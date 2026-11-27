#!/usr/bin/env python3
"""chunk_rust — 把大 Rust 文件切成「每块都能独立解析」的分段

与 chunk_js.py 同一套语义、同一份 manifest v2 契约，Rust 特有的三处不同：

    1. **impl 块按成员拆时补头补尾**：`impl T {` … `}`，同 JS 的类块。
       合成头只写类型（`impl Store<T> {`），trait impl（`impl Tr for T`）
       的 trait 名不进合成头——语法层解析不受影响，归属看 manifest 的
       class 字段（记的也是类型原文）。
    2. **属性是独立兄弟节点**：`#[derive(...)]` 在 tree-sitter-rust 里
       是 attribute_item，与 JSDoc/装饰器同理，要并进被修饰项的起点；
       注释节点类型是 line_comment / block_comment，不是 comment。
    3. **前导区是 use + 顶层 const/static**，没有 package 子句（与 Go 相反）。
       const/static 本身也算可切单元：出现在文件前部时它们同时是前导区
       内容，出现在函数之间时也不会掉进合并间隙丢掉。

可切单元：fn / struct / enum / trait / type / mod / macro_rules /
const / static / impl。保证同前：逐块重解析自检、body 逐字、
body_start 在 build_chunk 里一次算清。manifest language="rust"。

用法
    python chunk_rust.py src/big.rs --out .chunks
    python chunk_rust.py src/big.rs --out .chunks --max-lines 240 --no-git

依赖：tree_sitter、tree_sitter_rust（0.24.x，abi3 wheel）

退出码：0 全部成功；1 有块解析失败；2 参数/文件错误
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import tree_sitter_rust as _tsrs
from tree_sitter import Language, Parser

CHUNKER_NAME = "chunk_rust"
CHUNKER_VERSION = "0.1.0"
MANIFEST_SCHEMA = "chunk-manifest/2"
LANGUAGE_NAME = "rust"
DEFAULT_MAX_LINES = 240

_LANG = Language(_tsrs.language())

COMMENT_TYPES = {"line_comment", "block_comment"}
ABSORB_TYPES = COMMENT_TYPES | {"attribute_item"}
# 整段成块的顶层单元（impl 单独处理，可能拆成员）
WHOLE_TYPES = {
    "function_item", "struct_item", "enum_item", "trait_item",
    "type_item", "mod_item", "macro_definition",
    "const_item", "static_item",
}
# impl 体内可拆成员
IMPL_MEMBER_TYPES = {"function_item", "const_item", "type_item"}
NEVER_UNITS = {"use_declaration"} | COMMENT_TYPES


def parse_rs(text: str):
    return Parser(_LANG).parse(text.encode("utf-8"))


def has_error(node) -> bool:
    # 迭代实现：递归版在深嵌套文件上会 RecursionError
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" or n.is_missing:
            return True
        stack.extend(n.children)
    return False


def node_lines(node) -> tuple[int, int]:
    s = node.start_point[0] + 1
    e = node.end_point[0] + 1 if node.end_point[1] > 0 else node.end_point[0]
    return s, max(s, e)


def node_text(node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8", "replace")


def name_of(node, src_bytes: bytes) -> str | None:
    n = node.child_by_field_name("name")
    return node_text(n, src_bytes) if n is not None else None


def extended_start(node, siblings: list, idx: int) -> int:
    """紧贴上方、行行相邻的注释 / 属性并进节点起点（同 chunk_js 的修法：
    行尾注释属于上一行代码，不并）"""
    start, _ = node_lines(node)
    j = idx - 1
    while j >= 0:
        sib = siblings[j]
        if sib.type not in ABSORB_TYPES:
            break
        sib_start, sib_end = node_lines(sib)
        if sib_end != start - 1:
            break
        if sib.type in COMMENT_TYPES and j >= 1:
            prev_end = node_lines(siblings[j - 1])[1]
            if sib_start <= prev_end:
                break
        start = sib_start
        j -= 1
    return start


def impl_items(impl_node, src_bytes: bytes):
    """impl 拆成员 → [(start, end, label, type_text)]；无声明列表返回 None"""
    t = impl_node.child_by_field_name("type")
    type_text = node_text(t, src_bytes) if t is not None else "?"
    body = next((c for c in impl_node.children
                 if c.type == "declaration_list"), None)
    if body is None:
        return None
    items = []
    kids = list(body.children)
    for i, m in enumerate(kids):
        if m.type in IMPL_MEMBER_TYPES:
            s = extended_start(m, kids, i)
            _, e = node_lines(m)
            items.append((s, e, name_of(m, src_bytes) or "member", type_text))
        elif m.type == "attribute_item":
            continue  # 由 extended_start 并进它修饰的成员
    return items


def collect_items(src_text: str, max_lines: int = DEFAULT_MAX_LINES):
    """→ (items, preamble_end)；语义同 chunk_js.collect_items"""
    src_bytes = src_text.encode("utf-8")
    tree = parse_rs(src_text)
    root = tree.root_node
    items: list[tuple[int, int, str, str | None]] = []
    kids = list(root.children)

    first_unit: int | None = None
    for i, node in enumerate(kids):
        t = node.type
        if t in NEVER_UNITS or t == "attribute_item":
            continue
        s = extended_start(node, kids, i)
        _, e = node_lines(node)
        n_before = len(items)
        if t == "impl_item":
            if e - s + 1 <= max_lines:
                tt = node.child_by_field_name("type")
                label = node_text(tt, src_bytes) if tt is not None else "impl"
                items.append((s, e, f"impl {label}", None))
            else:
                sub = impl_items(node, src_bytes)
                if sub:
                    items.extend(sub)
                else:
                    items.append((s, e, "impl", None))
        elif t in WHOLE_TYPES:
            label = name_of(node, src_bytes) or t.replace("_item", "") \
                .replace("_definition", "")
            items.append((s, e, label, None))
        # 其余顶层节点（macro_invocation 等）不单独成块
        if len(items) > n_before and first_unit is None:
            # 前导区基准 = 第一个产出单元的**顶层节点**起点。impl 被拆成员时，
            # 基准是 impl 行本身——若误取第一个成员的行号，`impl Big {` 会掉进
            # 前导区被补进每一块：成员块双 impl 头、尾块挂未闭合 impl，
            # 所有块解析失败（impl-first 文件，2026-11-27 回测发现，
            # 与 chunk_js/chunk_ts 的 class-first 同源）。
            first_unit = s

    if not items:
        # 退化兜底（同 chunk_js）：顶层语句各自成单元，防静默逃审；
        # use 仍排除（前导区材料）
        for i, node in enumerate(kids):
            if node.type in NEVER_UNITS or node.type == "attribute_item":
                continue
            s = extended_start(node, kids, i)
            _, e = node_lines(node)
            label = name_of(node, src_bytes) or node.type.replace("_item", "")
            items.append((s, e, label, None))
    if not items:
        return [], len(src_text.splitlines())
    preamble_end = (first_unit if first_unit is not None
                    else min(it[0] for it in items)) - 1
    return items, preamble_end


def merge_items(items, max_lines: int = DEFAULT_MAX_LINES):
    """同一来源（同 impl 或同为顶层）的相邻小块合并（同 chunk_js）"""
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
    """→ (块文本, body_start)；impl 成员块补 `impl T {` 头 + `}` 尾，
    body_start 在这里一次算清（同 chunk_js）"""
    out = list(lines[:pre_end])
    if out:
        out.append("")
    if cls:
        out.append(f"impl {cls} {{")
    body_start = len(out) + 1
    out.extend(lines[s - 1:e])
    if cls:
        out.append("}")
    return "\n".join(out) + "\n", body_start


def force_rm(p: Path):
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
    if not p.is_dir():
        return False
    if (p / "manifest.json").exists():
        return True
    return any(p.glob("chunk_*.rs")) or any(p.glob("chunk_*.py"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="按语法树切分大 Rust 文件，保证每块语法完整")
    ap.add_argument("source", help="要切的 Rust 源文件（.rs）")
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
    if src.suffix.lower() != ".rs":
        print(f"只支持 .rs 文件，收到: {src.suffix}", file=sys.stderr)
        return 2
    if out == src.parent:
        print("输出目录不能是源文件所在目录", file=sys.stderr)
        return 2
    if out in src.parents:
        print(f"输出目录是源文件的上级目录，拒绝清空: {out}", file=sys.stderr)
        return 2
    if out == Path(out.anchor):
        print(f"输出目录是盘符根目录，拒绝清空: {out}", file=sys.stderr)
        return 2
    if out.exists() and not looks_like_chunk_dir(out) and not args.force:
        print(f"目标目录已存在且不像分段输出目录（没有 manifest.json / chunk_*.rs）：\n  {out}\n"
              "拒绝清空。确认要覆盖请加 --force。", file=sys.stderr)
        return 2

    # 同其他分段器：带 BOM 的 .rs 读进来就剥掉，块体里不留 U+FEFF。
    try:
        src_text = src.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        print(f"源文件不是合法的 UTF-8 文本，无法切分: {src}", file=sys.stderr)
        return 2
    if has_error(parse_rs(src_text).root_node):
        print("源文件本身解析就有 ERROR 节点，先修语法再切", file=sys.stderr)
        return 2
    lines = src_text.splitlines()
    items, pre_end = collect_items(src_text, args.max_lines)
    merged = merge_items(items, args.max_lines)

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
        if has_error(parse_rs(text).root_node):
            bad.append({"idx": i, "symbol": label,
                        "error": "tree-sitter re-parse has ERROR node"})
            continue
        safe = "".join(ch if ch.isalnum() or ch in "_-" else "_"
                       for ch in label.split("+")[0])[:40] or "x"
        fn = out / f"chunk_{i:02d}_{safe}.rs"
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
            "body_start": body_start,
            "source": src.name,
        })

    if not args.no_git:
        _git(["add", "-A"], out)
        _git(["commit", "-qm", "chunks"], out, check=False)

    print(f"\n切成 {len(manifest)} 块（源文件 {len(lines)} 行，前导 {pre_end} 行）")
    for c in manifest:
        tag = f"[impl {c['class']}]" if c["class"] else ""
        print(f"  {c['file']:48} 源行 {c['lines']:12} {c['n_lines']:4} 行 {tag}")

    if bad:
        print("\n✗ 有块无法解析，已跳过：", file=sys.stderr)
        for b in bad:
            print(f"   #{b['idx']} {b['symbol']}: {b['error']}", file=sys.stderr)

    mp = out / "manifest.json"
    mp.write_text(json.dumps(
        {"schema": MANIFEST_SCHEMA,
         "source": src.name, "source_lines": len(lines),
         "language": LANGUAGE_NAME,
         "chunker": {"name": CHUNKER_NAME, "version": CHUNKER_VERSION},
         "preamble_lines": pre_end, "chunks": manifest,
         "failed": len(bad),
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
