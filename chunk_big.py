#!/usr/bin/env python3
"""chunk_big — 把大 Python 文件切成「每块都能独立解析」的分段

为什么需要它
    代码审查模型（OCR 等）对超大单文件会超时或丢上下文。直接按行硬切，
    切出来的块往往缺 import、缺 class 头，模型会把「未定义名字」当成真缺陷
    报上来——全是假阳性。本工具按 AST 切，保证每块语法完整。

三条保证
    1. 每块都能 `ast.parse` 通过（切完逐块自检，不过就报错退出）
    2. 每块都带原文件的 import / 常量前导区
    3. 大类按方法切时补 `class X:` 头，保持原缩进

附带产物：`manifest.json` 记录每块对应的原始行号区间，
配合 ocr_adapter.py 的 --chunk-manifest 可把行号映射回源文件。

用法
    python chunk_big.py src/big.py --out .chunks
    python chunk_big.py src/big.py --out .chunks --max-lines 240
    python chunk_big.py src/big.py --out .chunks --no-git

退出码：0 全部成功；1 有块解析失败（已写出的块仍然可用，manifest 会标出缺口）
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

DEFAULT_MAX_LINES = 240


def preamble_end(src_text: str) -> int:
    """import / 常量区结束行（第一个顶层 func/class 之前）"""
    tree = ast.parse(src_text)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return max(0, node.lineno - 1)
    return len(src_text.splitlines())


def collect_items(src_text: str, max_lines: int = DEFAULT_MAX_LINES):
    """→ [(start, end, label, class_name|None)]

    小函数 / 小类整段输出；大类的每个方法单独成为一项，并记住所属类名。
    """
    tree = ast.parse(src_text)
    items = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            items.append((node.lineno, node.end_lineno, node.name, None))
        elif isinstance(node, ast.ClassDef):
            size = node.end_lineno - node.lineno + 1
            if size <= max_lines:
                items.append((node.lineno, node.end_lineno, node.name, None))
            else:
                for m in node.body:
                    s = getattr(m, "lineno", None)
                    if not s:
                        continue
                    e = getattr(m, "end_lineno", s)
                    name = getattr(m, "name", "<stmt>")
                    items.append((s, e, name, node.name))
    return items


def merge_items(items, max_lines: int = DEFAULT_MAX_LINES):
    """把同一来源（同类或同为顶层）的相邻小块合并到 max_lines"""
    out = []
    cur = None
    for s, e, label, cls in items:
        if cur and cur[3] == cls and (e - cur[0] + 1) <= max_lines:
            cur[1] = e
            cur[2] += "+" + label
        else:
            if cur:
                out.append(tuple(cur))
            cur = [s, e, label, cls]
    if cur:
        out.append(tuple(cur))
    return out


def build_chunk(lines, pre_end, s, e, cls) -> str:
    pre = "\n".join(lines[:pre_end])
    body = "\n".join(lines[s - 1:e])
    if cls:
        # 方法块：补 class 头，保持原缩进
        return f"{pre}\n\nclass {cls}:\n{body}\n"
    return f"{pre}\n\n{body}\n"


def force_rm(p: Path):
    """跨平台删除目录（Windows 上只读文件需先改属性）"""
    def onerr(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    if p.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=lambda f, path, _e: onerr(f, path, _e))
        else:
            shutil.rmtree(p, onerror=onerr)


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="按 AST 切分大 Python 文件，保证每块语法完整")
    ap.add_argument("source", help="要切的 Python 源文件")
    ap.add_argument("--out", required=True, help="输出目录（会被清空重建）")
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                    help=f"单块行数上限（默认 {DEFAULT_MAX_LINES}）")
    ap.add_argument("--no-git", action="store_true",
                    help="不初始化 git（OCR 的 review 模式需要 git，scan 模式不需要）")
    args = ap.parse_args(argv)

    src = Path(args.source).resolve()
    out = Path(args.out).resolve()
    if not src.is_file():
        print(f"源文件不存在: {src}", file=sys.stderr)
        return 2
    if out == src.parent:
        print("输出目录不能是源文件所在目录", file=sys.stderr)
        return 2

    src_text = src.read_text(encoding="utf-8")
    lines = src_text.splitlines()
    pre_end = preamble_end(src_text)
    merged = merge_items(collect_items(src_text, args.max_lines), args.max_lines)

    force_rm(out)
    out.mkdir(parents=True)

    if not args.no_git:
        try:
            _git(["init", "-q"], out)
            _git(["config", "user.email", "chunker@local"], out)
            _git(["config", "user.name", "chunker"], out)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"! git 初始化失败（加 --no-git 可跳过）: {exc}", file=sys.stderr)
            args.no_git = True

    manifest = []
    bad = []
    for i, (s, e, label, cls) in enumerate(merged, 1):
        text = build_chunk(lines, pre_end, s, e, cls)
        try:
            ast.parse(text)
        except SyntaxError as ex:
            bad.append((i, label, str(ex)))
            continue
        safe = "".join(ch if ch.isalnum() or ch in "_-" else "_"
                       for ch in label.split("+")[0])[:40] or "x"
        fn = out / f"chunk_{i:02d}_{safe}.py"
        fn.write_text(text, encoding="utf-8")
        manifest.append({
            "idx": i,
            "file": fn.name,
            "lines": f"{s}-{e}",
            "start_line": s,
            "end_line": e,
            "n_lines": e - s + 1,
            "symbol": label,
            "class": cls,
            "source": src.name,
        })

    if not args.no_git:
        _git(["add", "-A"], out)
        _git(["commit", "-qm", "chunks"], out, check=False)

    print(f"\n切成 {len(manifest)} 块（源文件 {len(lines)} 行，前导 {pre_end} 行）")
    for c in manifest:
        tag = f"[{c['class']}]" if c["class"] else ""
        print(f"  {c['file']:48} 源行 {c['lines']:12} {c['n_lines']:4} 行 {tag}")

    if bad:
        print("\n✗ 有块无法解析，已跳过：", file=sys.stderr)
        for i, label, err in bad:
            print(f"   #{i} {label}: {err}", file=sys.stderr)

    mp = out / "manifest.json"
    mp.write_text(json.dumps(
        {"source": src.name, "source_lines": len(lines),
         "preamble_lines": pre_end, "chunks": manifest, "failed": len(bad)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nmanifest → {mp}")
    return 0 if not bad else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
