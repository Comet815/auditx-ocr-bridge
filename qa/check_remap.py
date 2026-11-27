#!/usr/bin/env python3
"""check_remap — F1 裁决器：locator 行号 ↔ quote 真实行号

不需要 OCR 原始输出。只要两样东西：
  1. 任意含 finding 的 JSON/JSONL —— 适配器 mapped.json、账本 case 记录
     （含 payload 嵌套）、verifier 导出物均可。脚本递归找「有 locator
     且有 quote（或 evidence 里 kind=quote）的 dict」。
  2. 被审的源文件（可多个，按 basename 匹配）。

对每条 finding：quote 在源文件里定位真实行号（多行确认优先），
偏移 = locator 行号 − 真实行号。按源文件分组输出偏移分布并给裁决提示。

判读：
  · 未分段文件（直接 ocr scan）偏移应恒为 0 —— 控制组；
  · 分段文件偏移恒为 0          → 公式成立（OCR 报 body 相对行号）；
  · 分段文件偏移恒为 pre_end+1  → F1 实锤，按质检报告 F1 修法改 remap_lines；
  · 恒为其他常数                → 另有系统性原因（双重偏移/source-name 错位），
                                  逐条看 detail 再定性。

用法：
  python check_remap.py mapped.json --source src/big.py
  python check_remap.py cases/ --source-dir src/
"""
from __future__ import annotations
import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def walk_findings(obj, out):
    """递归收集 finding 形状的 dict：有 locator 且有 quote/quote 型 evidence"""
    if isinstance(obj, dict):
        loc = obj.get("locator")
        if isinstance(loc, str) and loc:
            q = obj.get("quote")
            if not (isinstance(q, str) and q.strip()):
                ev = obj.get("evidence")
                if isinstance(ev, list):
                    q = "\n".join(
                        e.get("content") for e in ev
                        if isinstance(e, dict) and e.get("kind") == "quote"
                        and isinstance(e.get("content"), str))
            if isinstance(q, str) and q.strip():
                out.append({"locator": loc, "quote": q})
            return  # 命中即停，不再深入 finding 内部（_source 等）
        for v in obj.values():
            walk_findings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_findings(v, out)


def load_findings(path: Path) -> list:
    text = path.read_text(encoding="utf-8", errors="replace")
    found: list = []
    if path.suffix == ".jsonl":
        for line in text.splitlines():
            if line.strip():
                try:
                    walk_findings(json.loads(line), found)
                except json.JSONDecodeError:
                    pass
        return found
    try:
        walk_findings(json.loads(text), found)
    except json.JSONDecodeError:  # 容错：.json 里其实存了 JSONL
        for line in text.splitlines():
            if line.strip():
                try:
                    walk_findings(json.loads(line), found)
                except json.JSONDecodeError:
                    pass
    return found


def parse_locator(loc: str):
    path, _, tail = loc.rpartition(":")
    m = re.match(r"\s*(\d+)", tail)
    if not path or not m:
        return None, None
    return path.strip(), int(m.group(1))


def locate_quote(src_lines: list, quote: str) -> list:
    """quote 在源文件中的候选行号；多行逐行确认优先，退化为首行匹配"""
    norm = [l.rstrip() for l in src_lines]
    qlines = [l.strip() for l in quote.splitlines() if l.strip()]
    if not qlines:
        return []
    first = qlines[0]
    cands = [i + 1 for i, l in enumerate(norm) if l.strip() == first]
    confirmed = []
    for ln in cands:
        ok = True
        for k, ql in enumerate(qlines[1:], 1):
            j = ln - 1 + k
            if j >= len(norm) or norm[j].strip() != ql:
                ok = False
                break
        if ok:
            confirmed.append(ln)
    return confirmed or cands


def pre_end_of(path: Path):
    """分段器口径的前导区行数（首个顶层 def/class 之前）"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.lineno - 1
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F1 裁决器：locator vs quote 真实行号")
    ap.add_argument("findings", nargs="+",
                    help="mapped.json / 账本 JSONL / 目录（递归扫 .json/.jsonl）")
    ap.add_argument("--source", action="append", default=[],
                    help="源文件（可多次）")
    ap.add_argument("--source-dir", help="源文件目录（按 basename 匹配）")
    args = ap.parse_args(argv)

    srcmap: dict = {}
    for s in args.source:
        p = Path(s)
        srcmap[p.name] = p
    if args.source_dir:
        for p in sorted(Path(args.source_dir).rglob("*.py")):
            srcmap.setdefault(p.name, p)

    files = []
    for f in args.findings:
        p = Path(f)
        if p.is_dir():
            files.extend(q for q in sorted(p.rglob("*"))
                         if q.suffix in (".json", ".jsonl"))
        else:
            files.append(p)

    findings = []
    for f in files:
        findings.extend(load_findings(f))
    if not findings:
        print("没找到任何 finding（需要 locator + quote/evidence）", file=sys.stderr)
        return 1
    print(f"载入 {len(findings)} 条 finding（来自 {len(files)} 个文件）\n")

    # 分组核对
    g = defaultdict(lambda: {"hits": [], "amb": 0, "noloc": 0, "nosrc": 0,
                             "total": 0, "src": None})
    for fd in findings:
        path, ln = parse_locator(fd["locator"])
        gkey = path or "<无法解析 locator>"
        grp = g[gkey]
        grp["total"] += 1
        if not path:
            continue
        src = srcmap.get(Path(path).name)
        if src is None:
            grp["nosrc"] += 1
            continue
        grp["src"] = src
        cands = locate_quote(
            src.read_text(encoding="utf-8", errors="replace").splitlines(),
            fd["quote"])
        if not cands:
            grp["noloc"] += 1
        elif len(cands) == 1:
            grp["hits"].append((fd["locator"], ln, cands[0]))
        else:
            grp["amb"] += 1

    # 输出
    verdicts = []
    for name, d in sorted(g.items()):
        print(f"◆ {name}  （finding {d['total']} 条：定位 {len(d['hits'])} · "
              f"多候选 {d['amb']} · 无法定位 {d['noloc']} · 无源文件 {d['nosrc']}）")
        for loc, ln, true in d["hits"][:20]:
            print(f"    {loc:44} locator行 {ln:>4} ↔ quote真值 {true:>4}"
                  f" → 偏移 {ln - true:+d}")
        if len(d["hits"]) > 20:
            print(f"    … 其余 {len(d['hits']) - 20} 条略")
        if d["hits"]:
            hist = Counter(ln - true for _, ln, true in d["hits"])
            if len(hist) == 1:
                c = next(iter(hist))
                if c == 0:
                    v = "偏移恒为 0：locator 与 quote 真值一致 ✓"
                else:
                    pe = pre_end_of(d["src"]) if d["src"] else None
                    if pe is not None and c == pe + 1:
                        v = f"偏移恒为 {c:+d} = pre_end({pe})+1 → F1 实锤 ✗"
                    else:
                        v = (f"偏移恒为 {c:+d}（pre_end+1="
                             f"{(pe + 1) if pe is not None else '?'}）→ 非前导区假设，另有原因")
            else:
                v = f"偏移分散 {dict(hist)} → 逐条人工核对"
            print(f"    ▶ 裁决：{v}")
            verdicts.append((name, v))
        print()

    print("─── 汇总 ───")
    for name, v in verdicts:
        print(f"  {name}: {v}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
