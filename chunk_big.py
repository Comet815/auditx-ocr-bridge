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

# 分段契约版本。适配器按 schema 判能不能回映；chunker 名字/版本写进 manifest，
# 让复核方知道这批块是哪个实现切的、切的是不是同一版规则。
MANIFEST_SCHEMA = "chunk-manifest/2"
CHUNKER_NAME = "chunk_big"
CHUNKER_VERSION = "0.2.0"
LANGUAGE = "python"


def effective_start(node) -> int:
    """节点真正的起始行 —— 装饰器也算它的。

    Python 3.8+ 起 `FunctionDef.lineno` 指向 `def` 行，**不含装饰器**。
    直接拿 lineno 当边界会把第一个顶层函数的 `@decorator` 留在前导区，
    随前导区补进每一块——在别的块里它悬空，粘到下一个语法对象上。
    解析能过（语法自检抓不住），但模型看到的是被改写的代码。
    """
    decos = [d.lineno for d in getattr(node, "decorator_list", []) or []]
    return min([node.lineno] + decos)


def preamble_end(src_text: str) -> int:
    """import / 常量区结束行（第一个顶层 func/class 之前）"""
    tree = ast.parse(src_text)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return max(0, effective_start(node) - 1)
    return len(src_text.splitlines())


def collect_items(src_text: str, max_lines: int = DEFAULT_MAX_LINES):
    """→ [(start, end, label, class_name|None)]

    小函数 / 小类整段输出；大类的每个方法单独成为一项，并记住所属类名。
    """
    tree = ast.parse(src_text)
    items = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            items.append((effective_start(node), node.end_lineno, node.name, None))
        elif isinstance(node, ast.ClassDef):
            c_start = effective_start(node)
            size = node.end_lineno - c_start + 1
            if size <= max_lines:
                items.append((c_start, node.end_lineno, node.name, None))
            else:
                for m in node.body:
                    s = getattr(m, "lineno", None)
                    if not s:
                        continue
                    # 方法自己的装饰器（@property / @staticmethod …）也要跟着走，
                    # 否则被切出函数体，模型看到的是裸 def
                    s = effective_start(m)
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


def build_chunk(lines, pre_end, s, e, cls) -> tuple:
    """→ (块文本, body 在块文件里的起始行号)

    body_start 是行号回映的**唯一真值**。模型看到的是块文件，它报的行号是
    块文件的绝对行号——所以适配器必须知道 body 落在块文件的第几行。
    前导区、分隔空行、补出来的 `class X:` 头各占几行，在这里一次算清；
    在外面再推一遍就会推错（曾经就少算了一个常数，全库行号整体偏移）。
    """
    out = list(lines[:pre_end])
    if out:
        out.append("")                       # 前导区与 body 之间的空行
    if cls:
        out.append(f"class {cls}:")          # 方法块补类头，保持原缩进
    body_start = len(out) + 1
    out.extend(lines[s - 1:e])
    return "\n".join(out) + "\n", body_start


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


def looks_like_chunk_dir(p: Path) -> bool:
    """是分段器自己产出的目录吗（有 manifest 或 chunk_*.py）"""
    if not p.is_dir():
        return False
    if (p / "manifest.json").exists():
        return True
    return any(p.glob("chunk_*.py"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="按 AST 切分大 Python 文件，保证每块语法完整")
    ap.add_argument("source", help="要切的 Python 源文件")
    ap.add_argument("--out", required=True, help="输出目录（会被清空重建）")
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                    help=f"单块行数上限（默认 {DEFAULT_MAX_LINES}）")
    ap.add_argument("--no-git", action="store_true",
                    help="不初始化 git（OCR 的 review 模式需要 git，scan 模式不需要）")
    ap.add_argument("--force", action="store_true",
                    help="目标目录已存在且不像分段输出目录时，强制清空重建")
    args = ap.parse_args(argv)

    src = Path(args.source).resolve()
    out = Path(args.out).resolve()
    if not src.is_file():
        print(f"源文件不存在: {src}", file=sys.stderr)
        return 2
    if out == src.parent:
        print("输出目录不能是源文件所在目录", file=sys.stderr)
        return 2
    # --out 会被整体 rmtree，护栏必须比"不是源目录"更强
    if out in src.parents:
        print(f"输出目录是源文件的上级目录，拒绝清空: {out}", file=sys.stderr)
        return 2
    if out == Path(out.anchor):
        print(f"输出目录是盘符根目录，拒绝清空: {out}", file=sys.stderr)
        return 2
    if out.exists() and not looks_like_chunk_dir(out) and not args.force:
        print(f"目标目录已存在且不像分段输出目录（没有 manifest.json / chunk_*.py）：\n  {out}\n"
              "拒绝清空。确认要覆盖请加 --force。", file=sys.stderr)
        return 2

    try:
        src_text = src.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"源文件不是合法的 UTF-8 文本，无法切分: {src}", file=sys.stderr)
        return 2
    lines = src_text.splitlines()
    try:
        pre_end = preamble_end(src_text)
        merged = merge_items(collect_items(src_text, args.max_lines), args.max_lines)
    except SyntaxError as e:
        print(f"源文件语法错误，无法切分: {e}", file=sys.stderr)
        return 2

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
        text, body_start = build_chunk(lines, pre_end, s, e, cls)
        try:
            ast.parse(text)
        except SyntaxError as ex:
            bad.append({"idx": i, "symbol": label, "error": str(ex)})
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
            # 块文件里 body 第一行的行号（含前导区/空行/类头），回映用
            "body_start": body_start,
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
        for b in bad:
            print(f"   #{b['idx']} {b['symbol']}: {b['error']}", file=sys.stderr)

    mp = out / "manifest.json"
    mp.write_text(json.dumps(
        {"schema": MANIFEST_SCHEMA,
         "source": src.name, "source_lines": len(lines),
         "language": LANGUAGE,
         "chunker": {"name": CHUNKER_NAME, "version": CHUNKER_VERSION},
         "preamble_lines": pre_end, "chunks": manifest,
         "failed": len(bad),
         # 明细而不只是计数：README 说 manifest 会标出缺口，那就真标出来
         "failed_detail": bad},
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
