#!/usr/bin/env python3
"""OCR → AuditX 适配器

把 alibaba/open-code-review 的 JSON 输出转成审查庭 review_brain_v2 能吃的
post_finding 参数，可选直接写进账本。

三条映射（对应《OCR 接入审查庭实测》里的结论）：
1. 枚举映射：critical/high/medium/low → BLOCKER/MAJOR/MINOR/INFO
   category → dimension(facts/logic/completeness/security/fit/quality)
2. 证据转换：existing_code → evidence:[{kind:"quote", content}]
3. 丢 thinking：模型思考链不进账本

用法：
    python ocr_adapter.py <ocr_output.json>                  # 打印映射结果（dry-run）
    python ocr_adapter.py <ocr_output.json> --json           # 输出 JSON
    python ocr_adapter.py <ocr_output.json> --post <case_dir> --seat S-OCR1   # 需自备引擎

依赖说明：
    只有 --post 需要审查庭引擎 review_brain_v2.py（私有组件，不在本仓库）。
    不带 --post 时本文件零第三方依赖、零本地库依赖，纯转换。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# ── 映射表 ──────────────────────────────────────────────────────────────

SEVERITY_MAP = {
    "critical": "BLOCKER",
    "high": "MAJOR",
    "medium": "MINOR",
    "low": "INFO",
    "info": "INFO",
}

# OCR 的 category 是自由文本，按关键词归类
DIMENSION_RULES = [
    (("security", "injection", "xss", "vuln", "crypto", "auth"), "security"),
    (("bug", "logic", "correctness", "error", "crash", "race", "concurr"), "logic"),
    (("performance", "perf", "memory", "slow"), "quality"),
    (("maintain", "style", "readab", "naming", "duplicate", "complex"), "quality"),
    (("doc", "comment", "test", "coverage"), "completeness"),
    (("api", "fit", "requirement", "spec"), "fit"),
]


def map_severity(raw: str | None) -> str:
    """未知/缺失的严重度按最保守的 INFO 处理。

    不能兜底成 MINOR：那会凭空抬高缺陷等级。上游给了看不懂的值时，
    正确的态度是"不认领"，而不是替它做决定。
    """
    return SEVERITY_MAP.get((raw or "").strip().lower(), "INFO")


def map_dimension(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    for keys, dim in DIMENSION_RULES:
        if any(k in s for k in keys):
            return dim
    return "quality"  # 兜底


def build_locator(c: dict) -> str:
    path = c.get("path") or "<unknown>"
    start = c.get("start_line")
    end = c.get("end_line")
    if start and end and end != start:
        return f"{path}:{start}-{end}"
    if start:
        return f"{path}:{start}"
    return path


def build_evidence(c: dict, session_id: str | None) -> list[dict]:
    """证据必须有内容。优先用逐字引用的 existing_code；
    没有的话退化成 ref（老实标注来源是 OCR 会话，不假装是逐字引用）。"""
    ev: list[dict] = []
    existing = (c.get("existing_code") or "").strip()
    if existing:
        ev.append({"kind": "quote", "content": existing})
    if not ev:
        sid = session_id or "unknown"
        snippet = (c.get("content") or "").strip()
        ev.append({"kind": "ref", "content": f"OCR session {sid}: {snippet[:200]}"})
    return ev


def load_chunk_manifest(path: Path) -> dict:
    """分段清单 → {块文件名: (源起始行, 源结束行)}

    兼容两种格式：
      1. chunk_big.py 产出的 dict：{"chunks": [{"file":..., "lines": "128-251"}], ...}
      2. 早期直接输出 list：[{"file":..., "lines": "128-251"}, ...]
        （取不到时用 start_line / end_line 字段）
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        entries = data.get("chunks") or data.get("manifest") or []
    elif isinstance(data, list):
        entries = data
    else:
        entries = []

    m = {}
    for c in entries:
        if not isinstance(c, dict):
            continue
        name = c.get("file") or c.get("path")
        if not name:
            continue
        a = b = None
        rng = str(c.get("lines") or "")
        if "-" in rng:
            left, _, right = rng.partition("-")
            try:
                a, b = int(left), int(right)
            except ValueError:
                a = b = None
        if a is None:
            try:
                a = int(c["start_line"])
                b = int(c.get("end_line", a))
            except (KeyError, TypeError, ValueError):
                continue
        m[name] = (a, b)
    return m


def remap_lines(comment: dict, manifest: dict) -> tuple:
    """把块内行号映射回源文件行号。返回 (start, end, 源文件名)。"""
    path = comment.get("path") or ""
    s = comment.get("start_line")
    e = comment.get("end_line") or s
    info = manifest.get(path)
    if not info or not s:
        return s, e, path
    src_start, _src_end = info
    # 块文件在第 1 行开始就是源文件的 src_start 行
    offset = src_start - 1
    return (s + offset, (e + offset) if e else None, path)


def build_locator_remapped(c: dict, manifest: dict | None,
                           source_name: str | None = None) -> str:
    if not manifest:
        return build_locator(c)
    s, e, path = remap_lines(c, manifest)
    shown = source_name or path
    if s and e and e != s:
        return f"{shown}:{s}-{e}"
    if s:
        return f"{shown}:{s}"
    return shown


# ── 跨块假报过滤 ────────────────────────────────────────────────────────
#
# 分段审查时，某块会引用定义在别块/前导里的符号，模型据此报
# "X is not imported / undefined / missing class"。这类是分段产物，不是真缺陷。
#
# 但**不能按关键词过滤**：真正的漏 import（如忘了 `import sqlite3`）用词一样，
# 会被误杀。正确判据是——**该符号在完整源文件里到底有没有定义**：
#   有定义（在别处）→ 跨块引用，丢弃
#   全文件都没有     → 真缺陷，保留

_IDENT_IN_BACKTICK = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)")
# 无引号时的兜底：从典型句式里抠符号名
_IDENT_PATTERNS = [
    re.compile(r"Missing\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:class|function|definition)", re.I),
    re.compile(r"Undefined\s+(?:function|helper|class|name)?\s*([A-Za-z_][A-Za-z0-9_]*)", re.I),
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+is\s+(?:not|never)\s+imported", re.I),
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+is\s+not\s+defined", re.I),
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+is\s+neither\s+imported", re.I),
]
# 太常见的英文词，别当符号
_STOPWIDTH = {
    "the", "a", "an", "this", "that", "it", "is", "are", "was", "were", "be",
    "import", "imported", "definition", "class", "function", "helper", "name",
    "critical", "high", "medium", "low", "code", "file", "line", "module",
}
_UNDEF_PAT = re.compile(
    r"(not imported|is never imported|NameError|undefined|"
    r"neither imported nor defined|missing .{0,40}(class|function|definition))",
    re.I,
)


def extract_identifiers(claim: str) -> list:
    """从 claim 里抽出被点名的符号（先去反引号，再走句式兜底）"""
    found = [m.group(1) for m in _IDENT_IN_BACKTICK.finditer(claim)]
    if not found:
        for pat in _IDENT_PATTERNS:
            found.extend(m.group(1) for m in pat.finditer(claim))
    import builtins

    out = []
    for x in found:
        if not x or len(x) <= 2:
            continue
        if x.lower() in _STOPWIDTH:
            continue
        # 内置名（NameError / ValueError …）不算"缺失的符号"
        if hasattr(builtins, x):
            continue
        out.append(x)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def collect_source_symbols(src_path: Path) -> set:
    """完整源文件里出现的所有顶层/类级定义名 + import 名"""
    text = src_path.read_text(encoding="utf-8", errors="replace")
    names = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # 解析不了就退化为正则
        for m in re.finditer(r"^\s*(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.M):
            names.add(m.group(1))
        for m in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", text, re.M):
            names.add(m.group(1))
        for m in re.finditer(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_, ]*)", text, re.M):
            names.update(x.strip() for x in m.group(1).split(","))
        for m in re.finditer(r"^\s*from\s+\S+\s+import\s+([A-Za-z_][A-Za-z0-9_, *]*)", text, re.M):
            names.update(x.strip() for x in m.group(1).split(",") if x.strip() != "*")
        return names

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            # 函数参数也算"已定义"，否则 citation 里提到形参会被判成缺失
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def is_chunk_artifact(comment: dict, source_symbols: set | None) -> tuple:
    """返回 (是否假报, 原因)。

    判据（保守，宁可漏杀不可错杀）：
      claim 里被点名的符号**全部**都能在完整源文件中找到 → 跨块引用假报
      只要有一个符号整份文件都没有 → 保留（可能是真缺陷，如忘了 import）
    因为"丢弃"是不可逆的破坏性操作，误杀真 bug 的代价 > 放过一条假报。
    """
    if not source_symbols:
        return False, ""
    claim = (comment.get("content") or "")
    if not _UNDEF_PAT.search(claim):
        return False, ""
    idents = extract_identifiers(claim)
    if not idents:
        return False, ""
    missing = [x for x in idents if x not in source_symbols]
    if missing:
        # 有符号在整份源文件里都找不到 → 可能是真的漏 import，保留
        return False, ""
    return True, "全部符号在完整源文件中已定义（跨块引用）"


_SEV_RANK = {"BLOCKER": 4, "MAJOR": 3, "MINOR": 2, "INFO": 1}


def dedupe_findings(findings: list) -> tuple:
    """同一条缺陷被模型报了多次时只留一条。

    判定：同一 (locator 的文件部分, 起始行) 视为同一处。
    保留策略：严重度更高者优先；同严重度取描述更长的那条（信息更全）。
    返回 (去重后列表, 被丢列表)。重复计数会直接影响 BLOCKER 门槛，必须去。
    """
    best: dict = {}
    dropped = []
    for f in findings:
        loc = f.get("locator") or ""
        path, _, tail = loc.partition(":")
        start = tail.split("-")[0].strip() if tail else ""
        key = (path, start)
        cur = best.get(key)
        if cur is None:
            best[key] = f
            continue
        # 比较
        cur_rank = _SEV_RANK.get(cur["severity"], 0)
        new_rank = _SEV_RANK.get(f["severity"], 0)
        if new_rank > cur_rank:
            dropped.append(cur)
            best[key] = f
        elif new_rank == cur_rank and len(f["claim"]) > len(cur["claim"]):
            dropped.append(cur)
            best[key] = f
        else:
            dropped.append(f)
    return list(best.values()), dropped


def convert(ocr_json: dict, source_file: str | None = None,
            manifest: dict | None = None,
            source_name: str | None = None,
            source_symbols: set | None = None) -> dict:
    """返回 {findings: [...], stats: {...}}"""
    session_id = ocr_json.get("session_id")
    comments = ocr_json.get("comments") or []

    findings = []
    dropped_empty_claim = 0
    dropped_artifacts = []
    for c in comments:
        claim = (c.get("content") or "").strip()
        if not claim:
            dropped_empty_claim += 1
            continue
        fp, why = is_chunk_artifact(c, source_symbols)
        if fp:
            dropped_artifacts.append({
                "path": c.get("path"),
                "line": c.get("start_line"),
                "reason": why,
                "claim": claim[:180],
            })
            continue
        findings.append({
            "dimension": map_dimension(c.get("category")),
            "severity": map_severity(c.get("severity")),
            "claim": claim,
            "evidence": build_evidence(c, session_id),
            "locator": build_locator_remapped(c, manifest, source_name),
            "quote": (c.get("existing_code") or "").strip(),
            "suggestion": (c.get("suggestion_code") or "").strip(),
            # 溯源（不进账本的额外字段，供人工核查）
            "_source": {
                "tool": "open-code-review",
                "session_id": session_id,
                "file": source_file,
                "ocr_severity": c.get("severity"),
                "ocr_category": c.get("category"),
                "chunk_line": c.get("start_line"),
                "remapped": bool(manifest and remap_lines(c, manifest)[0] != c.get("start_line")),
            },
        })

    before_dedupe = len(findings)
    findings, dup_dropped = dedupe_findings(findings)

    return {
        "findings": findings,
        "stats": {
            "deduped": before_dedupe - len(findings),
            "dedupe_detail": [{"locator": d.get("locator"), "claim": d.get("claim", "")[:120]}
                               for d in dup_dropped],
            "ocr_summary": ocr_json.get("summary"),
            "comments_total": len(comments),
            "converted": len(findings),
            "dropped_empty_claim": dropped_empty_claim,
            "dropped_chunk_artifacts": len(dropped_artifacts),
            "dropped_detail": dropped_artifacts,
            "severity_histogram": _hist(f["severity"] for f in findings),
            "dimension_histogram": _hist(f["dimension"] for f in findings),
        },
    }


def _hist(seq) -> dict:
    h: dict[str, int] = {}
    for x in seq:
        h[x] = h.get(x, 0) + 1
    return h


# ── 校验（不依赖 jsonschema，按 schema 的关键约束手动查） ─────────────────

def validate(f: dict) -> list[str]:
    errs = []
    if f["severity"] not in ("BLOCKER", "MAJOR", "MINOR", "INFO"):
        errs.append(f"severity 非法: {f['severity']}")
    if f["dimension"] not in ("facts", "logic", "completeness", "security", "fit", "quality"):
        errs.append(f"dimension 非法: {f['dimension']}")
    if not f.get("claim"):
        errs.append("claim 为空")
    ev = f.get("evidence") or []
    if not ev:
        errs.append("evidence 为空（审查庭会拒收）")
    for e in ev:
        if e.get("kind") not in ("quote", "calc", "ref", "source"):
            errs.append(f"evidence.kind 非法: {e.get('kind')}")
        if not e.get("content"):
            errs.append("evidence.content 为空")
    if not f.get("locator"):
        errs.append("locator 为空")
    return errs


# ── 读取 OCR 输出（前面可能混着进度行） ──────────────────────────────────

def load_ocr_json(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    idx = raw.rfind("\n{")
    if idx < 0:
        idx = raw.find("{")
    if idx < 0:
        raise SystemExit(f"没找到 JSON: {path}")
    return json.loads(raw[idx:].strip())


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，中文/emoji 会炸；强制 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="OCR → AuditX 适配器")
    ap.add_argument("ocr_json", help="OCR --format json 的输出文件")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出映射结果")
    ap.add_argument("--post", metavar="CASE_DIR", help="写进这个审查庭 case 目录")
    ap.add_argument("--seat", default="S-OCR1", help="席位 ID（默认 S-OCR1，须已 grant_seat）")
    ap.add_argument("--source-file", help="被审文件名（仅用于溯源记录）")
    ap.add_argument("--chunk-manifest", metavar="JSON",
                    help="分段清单（把块内行号映射回源文件行号）")
    ap.add_argument("--source-name", help="locator 里显示的源文件名（替代块文件名）")
    ap.add_argument("--source", metavar="FILE",
                    help="完整源文件路径（启用跨块假报过滤：符号在源文件中已定义则丢弃）")
    args = ap.parse_args(argv)

    manifest = None
    if args.chunk_manifest:
        manifest = load_chunk_manifest(Path(args.chunk_manifest))
        print(f"载入分段清单：{len(manifest)} 块", file=sys.stderr)

    data = load_ocr_json(Path(args.ocr_json))
    symbols = None
    if args.source:
        sp = Path(args.source)
        if not sp.exists():
            raise SystemExit(f"--source 指向的文件不存在: {sp}")
        symbols = collect_source_symbols(sp)
        print(f"载入源文件符号 {len(symbols)} 个（启用跨块假报过滤）", file=sys.stderr)

    out = convert(data, args.source_file, manifest, args.source_name, symbols)

    # 校验
    problems = []
    for i, f in enumerate(out["findings"], 1):
        for e in validate(f):
            problems.append(f"[{i}] {e}")

    if args.post:
        # 审查庭引擎是私有组件，不随本仓库分发。
        # 用 --post 时必须自行提供 review_brain_v2.py（放在 PATH 或 PYTHONPATH 里）。
        try:
            from review_brain_v2 import ReviewBrain  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                "--post 需要审查庭引擎 review_brain_v2.py，但它不在 PYTHONPATH 里。\n"
                "本仓库只含 OCR → 审查庭 的转换层，引擎本身不公开。\n"
                "去掉 --post 可用离线的纯转换模式（输出 JSON，稍后自己入库）。"
            ) from exc

        brain = ReviewBrain(Path(args.post))
        posted = []
        for f in out["findings"]:
            ev = [{"kind": e["kind"], "content": e["content"]} for e in f["evidence"]]
            rec = brain.post_finding(
                seat_id=args.seat,
                dimension=f["dimension"],
                severity=f["severity"],
                claim=f["claim"],
                evidence=ev,
                locator=f["locator"],
                quote=f["quote"],
                suggestion=f["suggestion"],
            )
            posted.append(rec)
        out["posted"] = len(posted)
        out["posted_ids"] = [p.get("payload", {}).get("finding_id") for p in posted]
        out["findings"] = [dict(f, _posted_id=pid) for f, pid in
                           zip(out["findings"], out["posted_ids"])]

    out["validation_problems"] = problems

    if args.json or args.post:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        st = out["stats"]
        print(f"OCR 会话: {st['ocr_summary'] and st['ocr_summary'].get('elapsed')}")
        print(f"comments {st['comments_total']} → 转换 {st['converted']}")
        print(f"严重度: {st['severity_histogram']}")
        print(f"维度:   {st['dimension_histogram']}")
        if problems:
            print("\n⚠️ 校验问题:")
            for p in problems:
                print("  " + p)
        else:
            print("\n✅ 全部字段通过审查庭校验")
        print("\n--- findings ---")
        for i, f in enumerate(out["findings"], 1):
            print(f"\n[{i}] {f['severity']:7} {f['dimension']:12} @ {f['locator']}")
            print(f"    结论: {f['claim'][:120]}")
            print(f"    证据: {f['evidence'][0]['kind']} :: {f['evidence'][0]['content'][:80]}")

    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())
