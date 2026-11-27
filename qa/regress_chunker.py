#!/usr/bin/env python3
"""regress_chunker.py — 分段器验收器

独立于被验收的 chunker：它只认 CLI 约定和 manifest 契约，不 import 对方的代码。
这样"切得对不对"由验收方判，而不是让分段器自己说自己对。

检的四件事（对应 JS 分段器立项的验收硬指标）：
  1. 每个被审文件都能切，退出码 0
  2. manifest 带 v2 字段（schema / language / chunker / 每块 body_start）
  3. 每块都能被语言解析器重新解析通过（python: ast；js: tree-sitter）
  4. body_start 指的那一行，逐字等于源文件 start_line 那一行（几何真值）

用法：
  python qa/regress_chunker.py --cmd "python chunk_js.py" --target <目录> --ext .js
  python qa/regress_chunker.py --cmd "python chunk_big.py" --target tests --ext .py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def _ts_parsers():
    """返回 [(名字, Parser)]；typescript 先，tsx 兜底（JSX 只在 tsx 语法里合法）"""
    from tree_sitter import Language, Parser
    import tree_sitter_typescript as tst
    return [("typescript", Parser(Language(tst.language_typescript()))),
            ("tsx", Parser(Language(tst.language_tsx())))]


def parse_ok(text: str, language: str) -> tuple[bool, str]:
    data = text.encode("utf-8")
    if language == "python":
        import ast
        try:
            ast.parse(text)
            return True, ""
        except SyntaxError as ex:
            return False, f"SyntaxError 行{ex.lineno}: {ex.msg}"

    if language == "javascript":
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_javascript as tsj
        except ImportError as ex:
            return True, f"跳过（缺 tree-sitter：{ex}）"
        tree = Parser(Language(tsj.language())).parse(data)
        return (not tree.root_node.has_error), ("tree-sitter 报语法错误"
                                                if tree.root_node.has_error else "")

    if language == "typescript":
        try:
            parsers = _ts_parsers()
        except ImportError as ex:
            return True, f"跳过（缺 tree_sitter_typescript：{ex}）"
        if not any(p.parse(data).root_node.has_error is False for _, p in parsers):
            return False, "typescript/tsx 两种语法都报错"
        return True, ""

    if language == "vue":
        # SFC 的块本身是 script 内容：JS 或 TS 都可能，任一能解析就算过
        candidates = []
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_javascript as tsj
            candidates.append(("javascript", Parser(Language(tsj.language()))))
        except ImportError:
            pass
        try:
            candidates += _ts_parsers()
        except ImportError:
            pass
        if not candidates:
            return True, "跳过（缺 tree-sitter）"
        if any(not p.parse(data).root_node.has_error for _, p in candidates):
            return True, ""
        return False, "JS/TS 语法都解析不了"

    return True, f"跳过（未知语言 {language}）"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="分段器验收器")
    ap.add_argument("--cmd", required=True, help='分段器命令，例："python chunk_js.py"')
    ap.add_argument("--target", required=True, help="被切文件所在目录")
    ap.add_argument("--ext", default=".js", help="只看这个扩展名（默认 .js）")
    ap.add_argument("--max-lines", default="240")
    ap.add_argument("--limit", type=int, default=0, help="只测前 N 个文件（0=全部）")
    args = ap.parse_args(argv)

    target = Path(args.target)
    files = sorted(p for p in target.rglob(f"*{args.ext}") if p.is_file())
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"没找到 {args.ext} 文件：{target}", file=sys.stderr)
        return 2

    cmd = args.cmd.split()
    n_files = n_chunks = 0
    fails: list[str] = []
    skipped_parse = 0

    with tempfile.TemporaryDirectory() as td:
        for f in files:
            out = Path(td) / f"chunks_{f.stem}"
            r = subprocess.run(
                cmd + [str(f), "--out", str(out), "--max-lines", args.max_lines,
                       "--no-git", "--force"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                fails.append(f"{f.name}: 切分退出码 {r.returncode} — {r.stderr.strip()[-160:]}")
                continue

            mp = out / "manifest.json"
            if not mp.exists():
                fails.append(f"{f.name}: 没产出 manifest.json")
                continue
            man = json.loads(mp.read_text(encoding="utf-8"))

            if man.get("schema") != "chunk-manifest/2":
                fails.append(f"{f.name}: schema={man.get('schema')!r}，期望 chunk-manifest/2")
            if not man.get("language"):
                fails.append(f"{f.name}: manifest 缺 language")
            if not (man.get("chunker") or {}).get("name"):
                fails.append(f"{f.name}: manifest 缺 chunker.name")

            src_lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            chunks = man.get("chunks") or []
            if not chunks:
                fails.append(f"{f.name}: 一块都没切出来")
                continue

            for c in chunks:
                n_chunks += 1
                name = c.get("file")
                bs = c.get("body_start")
                if not isinstance(bs, int) or bs < 1:
                    fails.append(f"{f.name}/{name}: 缺 body_start")
                    continue
                text = (out / name).read_text(encoding="utf-8", errors="replace")
                clines = text.splitlines()

                # 几何真值：块内 body 第一行 == 源文件 start_line 行
                sl = c.get("start_line")
                if 0 < bs <= len(clines) and 0 < sl <= len(src_lines):
                    if clines[bs - 1].strip() != src_lines[sl - 1].strip():
                        fails.append(
                            f"{f.name}/{name}: body_start={bs} 指的行与源 {sl} 行不符"
                            f"（块内 {clines[bs - 1].strip()[:40]!r} vs 源 {src_lines[sl - 1].strip()[:40]!r}）")
                else:
                    fails.append(f"{f.name}/{name}: body_start={bs} 或 start_line={sl} 越界")

                ok, why = parse_ok(text, man.get("language") or "")
                if not ok:
                    fails.append(f"{f.name}/{name}: 块解析失败 — {why}")
                elif why:
                    skipped_parse += 1

            n_files += 1

    print(f"\n文件 {n_files}/{len(files)} · 块 {n_chunks} · 解析跳过 {skipped_parse}")
    if fails:
        print(f"\n✗ {len(fails)} 个问题：")
        for x in fails[:30]:
            print("  - " + x)
        if len(fails) > 30:
            print(f"  … 其余 {len(fails) - 30} 条略")
        return 1
    print("✅ 全部通过：切分 / 契约字段 / 块可解析 / body_start 几何真值")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
