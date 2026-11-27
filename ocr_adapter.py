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
import builtins
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

# OCR 的 category 是自由文本，按关键词归类。
# 中英混排是常态（模型常常用中文写报告），关键词表必须两种语言都收。
DIMENSION_RULES = [
    (("security", "injection", "xss", "vuln", "crypto", "auth",
      "安全", "注入", "漏洞", "加密", "鉴权", "越权", "敏感信息"), "security"),
    (("bug", "logic", "correctness", "error", "crash", "race", "concurr",
      "逻辑", "错误", "缺陷", "崩溃", "异常", "并发", "竞态", "空指针"), "logic"),
    (("performance", "perf", "memory", "slow",
      "性能", "内存", "泄漏", "耗时", "慢"), "quality"),
    (("maintain", "style", "readab", "naming", "duplicate", "complex",
      "可维护", "风格", "命名", "重复", "复杂", "整洁"), "quality"),
    (("doc", "comment", "test", "coverage",
      "文档", "注释", "测试", "覆盖"), "completeness"),
    (("api", "fit", "requirement", "spec",
      "接口", "需求", "规格", "适配", "兼容"), "fit"),
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


# 能保证 body_start 语义的语言。不在这张表里的一律拒绝回映——
# 宁可让调用方看到"未回映"，也不能给出一个没人验证过的行号。
SUPPORTED_LANGUAGES = {"python", "javascript"}


class ChunkManifest(dict):
    """{块文件名: (源起始行, 源结束行)} + 分段几何信息

    继承 dict 是为了向后兼容：老的调用方当普通映射用，照样能 `m[name]`。
    额外带真值字段：
      schema          分段契约版本（chunk-manifest/2 起有 language/chunker）
      language        源语言；不认识的语言拒绝回映，不猜
      chunker         {name, version}，复核方据此知道这批块是哪个实现切的
      preamble_lines  块文件里前导区占的行数（manifest 顶层字段）
      body_start      {块文件名: body 第一行在块文件里的行号}
    行号回映只认这些，不再靠"块内第 1 行 = body 第 1 行"这种假设。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.schema: str | None = None
        self.language: str | None = None
        self.chunker: dict = {}
        self.preamble_lines: int | None = None
        self.body_start: dict = {}

    def resolve(self, path: str) -> str | None:
        """块路径 → manifest 里的键。

        先精确匹配；再退化到 basename —— OCR 常把 path 报成
        "chunks/chunk_02_x.py" 这种带目录前缀的形式，而 manifest 里存的是
        裸文件名。块文件名在清单内唯一，所以 basename 兜底是安全的；
        不兜底就会静默 missed（F10 那类"坑 1 复发"）。
        """
        if not path:
            return None
        if path in self:
            return path
        base = Path(path).name
        return base if base in self else None


def load_chunk_manifest(path: Path) -> ChunkManifest:
    """分段清单 → ChunkManifest

    兼容两种格式：
      1. chunk_big.py 产出的 dict：{"chunks": [{"file":..., "lines": "128-251",
         "body_start": 27, ...}], "preamble_lines": 25, ...}
      2. 早期直接输出 list：[{"file":..., "lines": "128-251"}, ...]
        （取不到时用 start_line / end_line 字段）
    老清单没有 body_start 时仍可用，但回映会降级并告警（见 remap_detail）。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        entries = data.get("chunks") or data.get("manifest") or []
    elif isinstance(data, list):
        entries = data
    else:
        entries = []

    m = ChunkManifest()
    if isinstance(data, dict):
        m.schema = data.get("schema")
        lang = data.get("language")
        if isinstance(lang, str):
            m.language = lang.strip().lower()
        ch = data.get("chunker")
        if isinstance(ch, dict):
            m.chunker = ch
        pl = data.get("preamble_lines")
        if isinstance(pl, int) and pl >= 0:
            m.preamble_lines = pl

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
        bs = c.get("body_start")
        if isinstance(bs, int) and bs > 0:
            m.body_start[name] = bs
    return m


def remap_detail(comment: dict, manifest) -> tuple:
    """块内行号 → 源文件行号，返回 (start, end, path, status)。

    status：
      remapped      正常回映
      preamble      模型报的是前导区行号（前导区是逐字复制的，源行号相同）
      header        报在分隔空行 / 补出来的 class 头上 → 钳到 body 首行
      out-of-range  回映结果落在源文件区间之外（多半是模型幻觉行号）
      unmatched     manifest 里没有这个块路径（静默跳过的老毛病，现在会计数）
      unsupported-language  manifest 声明的语言不在白名单里 → 拒绝回映
      legacy        老清单缺 body_start，只能按旧假设回映 —— 不可靠，会告警
      no-manifest   没启用回映
    """
    path = comment.get("path") or ""
    s = comment.get("start_line")
    e = comment.get("end_line") or s
    if not manifest or not s:
        return s, e, path, "no-manifest"
    key = manifest.resolve(path) if hasattr(manifest, "resolve") else (
        path if path in manifest else None)
    if not key:
        return s, e, path, "unmatched"

    # 语言不认识就别回映。manifest 里有 language 但不在白名单里，
    # 说明那个 chunker 的 body_start 语义没人验过——猜出来的行号比没有更糟。
    lang = getattr(manifest, "language", None)
    if lang and lang not in SUPPORTED_LANGUAGES:
        return s, e, path, "unsupported-language"

    info = manifest[key]
    src_start, src_end = info
    body_start = getattr(manifest, "body_start", {}).get(key)
    pre = getattr(manifest, "preamble_lines", None)

    if body_start is None:
        # 老清单：没有几何信息，只能维持旧公式（等于假设 body 从块第 1 行开始）。
        # 这个假设已被实测证伪，所以标 legacy 让上层能告警。
        off = src_start - 1
        return s + off, (e + off) if e else None, path, "legacy"

    if s < body_start:
        if pre is not None and s <= pre:
            return s, min(e, pre) if e else s, path, "preamble"
        return src_start, src_start if not e else src_start, path, "header"

    off = src_start - body_start
    ns = s + off
    ne = (e + off) if e else None
    if ns < src_start or (ne is not None and ne > src_end):
        return ns, ne, path, "out-of-range"
    return ns, ne, path, "remapped"


def remap_lines(comment: dict, manifest) -> tuple:
    """把块内行号映射回源文件行号。返回 (start, end, 源文件名)。"""
    s, e, path, _status = remap_detail(comment, manifest)
    return s, e, path


def build_locator_remapped(c: dict, manifest=None,
                           source_name: str | None = None) -> str:
    if not manifest:
        return build_locator(c)
    s, e, path, status = remap_detail(c, manifest)
    # --source-name 只在真的命中了 manifest 时生效。对没命中的路径（同一次
    # 扫描里顺带扫的其他文件、或路径键对不上）一律保留原始文件名，
    # 否则会张冠李戴：文件名被改写、行号还留着，看起来完全合理。
    shown = source_name if (source_name and status not in
                            ("unmatched", "no-manifest", "unsupported-language")) else path
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
# 裸函数名：call() / buildReviews(answers) —— 英文报告里非常常见，不带反引号。
# 允许带参数：JS/Python 里大多数调用都有实参，只认空括号会漏掉大半。
# 前面不能是点或词字符，免得把 `os.system()` 里的 system 当成顶层符号。
_CALL_IDENT = re.compile(r"(?<![.\w])([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# 无引号时的兜底：从典型句式里抠符号名
_IDENT_PATTERNS = [
    re.compile(r"Missing\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:class|function|definition)", re.I),
    re.compile(r"Undefined\s+(?:function|helper|class|name)?\s*([A-Za-z_][A-Za-z0-9_]*)", re.I),
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+is\s+(?:not|never)\s+imported", re.I),
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+is\s+not\s+defined", re.I),
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+is\s+neither\s+imported", re.I),
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+is\s+(?:never|not)\s+defined", re.I),
    # 中文句式（OCR 在中文语境大量输出中文 claim，README 自己举的例子就是中文）
    re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:未|没有|缺少|缺失)(?:被)?(?:导入|定义|声明)"),
    re.compile(r"(?:缺少|未见|未发现)\s*([A-Za-z_][A-Za-z0-9_]*)\s*的?\s*(?:导入|定义|声明)"),
]
# 太常见的英文词，别当符号
_STOPWIDTH = {
    "the", "a", "an", "this", "that", "it", "is", "are", "was", "were", "be",
    "import", "imported", "definition", "class", "function", "helper", "name",
    "critical", "high", "medium", "low", "code", "file", "line", "module",
    # 连词/虚词：句式层会盯 "X is never defined" 前面那个词，不挡就会抽到 but/and
    "but", "and", "or", "so", "which", "where", "when", "here", "there",
    "also", "then", "same", "applies", "above", "below",
}
# 闸门：这段文字是不是在说"某符号未导入/未定义"。只认英文的话，
# 中文报告下这个过滤器完全空转（保守方向没错，但等于没过滤）。
_UNDEF_PAT = re.compile(
    r"(not imported|is never imported|NameError|undefined|"
    r"neither imported nor defined|missing .{0,40}(class|function|definition)|"
    # 英文里主语和"未定义"之间常夹几个词：is called but not defined /
    # never declared / was not imported。所以不要求 (is|are) 紧邻。
    r"\b(?:not|never)\s+(?:defined|declared|imported)\b|"
    r"未(?:被)?导入|没有(?:被)?导入|未被定义|未定义|没有定义|"
    r"缺少.{0,12}(?:导入|定义)|缺失.{0,12}(?:导入|定义))",
    re.I,
)


def _usable(name: str) -> bool:
    """能不能当"被点名的符号"：太短、太常见、是内置名，都不算"""
    if not name or len(name) <= 2:
        return False
    if name.lower() in _STOPWIDTH:
        return False
    # 内置名（NameError / ValueError …）不算"缺失的符号"
    return not hasattr(builtins, name)


def extract_identifiers(claim: str) -> list:
    """从 claim 里抽出被点名的符号

    顺序：反引号 → 句式 → 裸函数名。每层都要先清洗，清洗空了才往下走 ——
    否则 "…but is never defined…" 里的连词 `but` 会被句式层抓走，
    非空就挡住了后面的裸函数名兜底（盲测实锤踩过）。
    """
    picked = [x for x in (m.group(1) for m in _IDENT_IN_BACKTICK.finditer(claim))
              if _usable(x)]
    if not picked:
        from_pat = []
        for pat in _IDENT_PATTERNS:
            from_pat.extend(m.group(1) for m in pat.finditer(claim))
        picked = [x for x in from_pat if _usable(x)]
    if not picked:
        # 英文报告的常见写法：`call()`、`build_prompt()` 这种裸函数名。
        # 盲测实测：一条 "call() is invoked here but is never defined or
        # imported..." 的跨块假报，闸门命中了但这里抽不出符号，于是被放过。
        picked = [x for x in (m.group(1) for m in _CALL_IDENT.finditer(claim))
                  if _usable(x)]
    seen = set()
    return [x for x in picked if not (x in seen or seen.add(x))]


JS_SUFFIXES = {".js", ".mjs", ".cjs", ".jsx"}


def collect_source_symbols(src_path) -> set:
    """按扩展名分派符号抽取。

    语言不对就别硬抽：符号表错了，假报过滤器要么误杀真 bug、要么空转。
    2026-11-27 盲测那条 `call()` 漏网，根因就是"闸门对了、符号抽取不对"。
    不认识的后缀直接报错，不猜。
    """
    p = Path(src_path)
    suffix = p.suffix.lower()
    if suffix == ".py":
        return _collect_python_symbols(p)
    if suffix in JS_SUFFIXES:
        try:
            from js_symbols import collect_js_symbols  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                f"--source 指向 {p.name}，需要 JS 符号表抽取器 js_symbols.py，但它不在 PYTHONPATH 里。\n"
                "Python 的 ast 抽不出 JS 符号；没有它，跨块假报过滤会空转——"
                "宁可明确报错，也不假装过滤过。"
            ) from exc
        return collect_js_symbols(p)
    raise SystemExit(
        f"--source 不支持的语言：{p.name}（{suffix or '无扩展名'}）。"
        "目前支持 .py（内置）与 .js/.mjs/.cjs/.jsx（需 js_symbols.py）。"
    )


def _collect_python_symbols(src_path: Path) -> set:
    """Python：完整源文件里出现的所有顶层/类级定义名 + import 名"""
    src_path = Path(src_path)
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


def _format_locator(s, e, path, source_name=None, use_source_name=False) -> str:
    shown = source_name if (source_name and use_source_name) else path
    if s and e and e != s:
        return f"{shown}:{s}-{e}"
    if s:
        return f"{shown}:{s}"
    return shown


def convert(ocr_json: dict, source_file: str | None = None,
            manifest=None,
            source_name: str | None = None,
            source_symbols: set | None = None) -> dict:
    """返回 {findings: [...], stats: {...}}"""
    session_id = ocr_json.get("session_id")
    comments = ocr_json.get("comments") or []

    findings = []
    dropped_empty_claim = 0
    dropped_artifacts = []
    remap_stats = {
        "enabled": manifest is not None,
        "language": getattr(manifest, "language", None) if manifest else None,
        "chunker": getattr(manifest, "chunker", {}) if manifest else {},
        "remapped": 0,       # 正常回映
        "preamble": 0,       # 报在前导区（源行号相同）
        "header": 0,         # 报在空行/补出来的 class 头上
        "out_of_range": 0,   # 回映结果越出源文件区间
        "legacy": 0,         # 老清单缺 body_start
        "unsupported_language": 0,   # 语言不在白名单，拒绝回映
        "basename_matched": 0,   # OCR 报了带目录前缀的 path，靠 basename 命中
        "unmatched_paths": [],   # manifest 里找不到的块路径（去重）
    }
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

        if manifest:
            s, e, path, status = remap_detail(c, manifest)
            key = manifest.resolve(path) if hasattr(manifest, "resolve") else None
            if key and key != path:
                remap_stats["basename_matched"] += 1
            locator = _format_locator(
                s, e, path, source_name,
                status not in ("unmatched", "no-manifest", "unsupported-language"))
            if status == "remapped":
                remap_stats["remapped"] += 1
            elif status == "preamble":
                remap_stats["preamble"] += 1
            elif status == "header":
                remap_stats["header"] += 1
            elif status == "out-of-range":
                remap_stats["out_of_range"] += 1
            elif status == "legacy":
                remap_stats["legacy"] += 1
            elif status == "unsupported-language":
                remap_stats["unsupported_language"] += 1
            elif status == "unmatched":
                if c.get("path") not in remap_stats["unmatched_paths"]:
                    remap_stats["unmatched_paths"].append(c.get("path"))
        else:
            path = c.get("path") or ""
            status = "no-manifest"
            locator = build_locator(c)

        findings.append({
            "dimension": map_dimension(c.get("category")),
            "severity": map_severity(c.get("severity")),
            "claim": claim,
            "evidence": build_evidence(c, session_id),
            "locator": locator,
            "quote": (c.get("existing_code") or "").strip(),
            "suggestion": (c.get("suggestion_code") or "").strip(),
            # 溯源（不进账本的额外字段，供人工核查）
            "_source": {
                "tool": "open-code-review",
                "session_id": session_id,
                "file": source_file,
                # 每条 comment 自己的原始 path，改名/回映前的证据要留得住
                "ocr_path": c.get("path"),
                "ocr_severity": c.get("severity"),
                "ocr_category": c.get("category"),
                "chunk_line": c.get("start_line"),
                "remapped": status in ("remapped", "preamble", "header"),
                "remap_status": status,
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
            "remap": remap_stats,
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
    """读 OCR 的 JSON 输出，容忍前后夹带的进度/日志行。

    用 raw_decode 从第一个能解析成 dict 的 `{` 开始，解析完即止、不管尾部 ——
    日志在前（进度行）和日志在后（"Scan complete in 12.3s"）都能吃下。
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    dec = json.JSONDecoder()
    idx = raw.find("{")
    while idx >= 0:
        try:
            obj, _end = dec.raw_decode(raw, idx)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        idx = raw.find("{", idx + 1)
    raise SystemExit(f"没找到 JSON: {path}")


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
    ap.add_argument("-o", "--out", metavar="FILE",
                    help="把映射结果写成 JSON 文件（stdout 保留人类可读摘要）")
    ap.add_argument("--post", metavar="CASE_DIR", help="写进这个审查庭 case 目录")
    ap.add_argument("--allow-invalid", action="store_true",
                    help="--post 时校验不过的条目跳过并留痕，而不是整体拒绝入库")
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

    # 回映健康度：静默失败是上一版最大的坑（路径对不上就悄悄跳过，没人知道）
    rs = out["stats"]["remap"]
    if manifest:
        if rs["legacy"]:
            print("⚠️ 分段清单缺 body_start（旧版 chunk_big 产物）。\n"
                  "   回映会退化为旧公式，行号整体偏移「前导行数+1」。请用新版重新切分。",
                  file=sys.stderr)
        if rs["unmatched_paths"]:
            shown = ", ".join(str(p) for p in rs["unmatched_paths"][:5])
            print(f"⚠️ {len(rs['unmatched_paths'])} 个块路径不在分段清单里，行号未回映：{shown}",
                  file=sys.stderr)
        if rs["out_of_range"]:
            print(f"⚠️ {rs['out_of_range']} 条回映结果越出源文件区间（疑似模型幻觉行号）",
                  file=sys.stderr)

    # 校验
    problems = []
    bad_idx = set()
    for i, f in enumerate(out["findings"], 1):
        errs = validate(f)
        if errs:
            bad_idx.add(i)
            problems.extend(f"[{i}] {e}" for e in errs)

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

        # fail-closed：坏数据一条都不入库。
        # 之前是先全量入库、再事后报问题，留下"半批入库 + 异常中断"的账本噪音。
        if problems and not args.allow_invalid:
            print("✗ 校验未通过，按 fail-closed 拒绝入库（账本一个字都没写）：", file=sys.stderr)
            for p in problems:
                print("  " + p, file=sys.stderr)
            print("\n修好数据再来，或加 --allow-invalid 跳过坏条目后入库。", file=sys.stderr)
            out["validation_problems"] = problems
            out["posted"] = 0
            return 2

        brain = ReviewBrain(Path(args.post))
        posted_ids = {}
        skipped = []
        for i, f in enumerate(out["findings"], 1):
            if i in bad_idx:
                skipped.append({"locator": f.get("locator"), "reason": "校验未通过"})
                continue
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
            posted_ids[i] = rec.get("payload", {}).get("finding_id")
        out["posted"] = len(posted_ids)
        out["posted_ids"] = list(posted_ids.values())
        if skipped:
            out["skipped_invalid"] = skipped
        out["findings"] = [dict(f, _posted_id=posted_ids.get(i))
                           for i, f in enumerate(out["findings"], 1)]

    out["validation_problems"] = problems

    if args.out:
        op = Path(args.out)
        op.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"→ 已写入 {op}", file=sys.stderr)

    if args.json or args.post:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        st = out["stats"]
        print(f"OCR 会话: {st['ocr_summary'] and st['ocr_summary'].get('elapsed')}")
        print(f"comments {st['comments_total']} → 转换 {st['converted']}")
        print(f"严重度: {st['severity_histogram']}")
        print(f"维度:   {st['dimension_histogram']}")
        if manifest:
            print(f"回映: 正常 {rs['remapped']} · 前导区 {rs['preamble']} · "
                  f"类头 {rs['header']} · 越界 {rs['out_of_range']} · "
                  f"未匹配块 {len(rs['unmatched_paths'])}")
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
