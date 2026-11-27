#!/usr/bin/env python3
"""chunk_js — 把大 JavaScript 文件切成「每块都能独立解析」的分段

为什么需要它
    与 chunk_big.py（Python 版）同一个理由、同一套语义，换到 JS：
    代码审查模型对超大单文件会超时或丢上下文；按行硬切会切出缺 import、
    缺 class 头的块，模型把「未定义名字」当真缺陷报，全是假阳性。
    本工具按 tree-sitter 语法树切，保证每块语法完整。

三条保证（与 Python 版对齐）
    1. 每块都能被 tree-sitter 重新解析通过（切完逐块自检，不过就报错退出）
    2. 每块都带原文件的 import / require / 顶层常量前导区（逐字复制）
    3. 大类按方法切时补 `class X {` 头 **和** 收尾 `}`（JS 只补头会语法错误）

硬语义（manifest v2 契约，冻结）
    - body 是源文件 start_line..end_line 的逐字复制，不改写
    - body_start 是 body 第一行在**块文件**里的行号（1 基），回映的唯一真值，
      在 build_chunk 里一次算清，不在别处重推
    - manifest 顶层带 schema / language / chunker 字段，见 write_manifest

附带产物：`manifest.json`（schema: chunk-manifest/2），配合 ocr_adapter.py
的 --chunk-manifest 可把行号映射回源文件。

用法
    python chunk_js.py src/big.js --out .chunks
    python chunk_js.py src/big.js --out .chunks --max-lines 240
    python chunk_js.py src/big.js --out .chunks --no-git

依赖：tree_sitter、tree_sitter_javascript（pip 安装，都有 wheel）

退出码：0 全部成功；1 有块解析失败（已写出的块仍然可用，manifest 会标出缺口）；
        2 参数/文件错误
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

import tree_sitter_javascript as _tsjs
from tree_sitter import Language, Parser

CHUNKER_NAME = "chunk_js"
CHUNKER_VERSION = "0.1.0"
MANIFEST_SCHEMA = "chunk-manifest/2"
LANGUAGE_NAME = "javascript"
DEFAULT_MAX_LINES = 240

_LANG = Language(_tsjs.language())

# 赋值右侧是这些节点时，声明/赋值语句算「可切单元」
FUNC_VALUE_TYPES = {
    "arrow_function", "function_expression", "function",
    "generator_function", "generator_function_expression",
}
FUNC_DECL_TYPES = {"function_declaration", "generator_function_declaration"}


def _parser() -> Parser:
    return Parser(_LANG)


def parse_js(text: str):
    return _parser().parse(text.encode("utf-8"))


def has_error(node) -> bool:
    """子树里有没有 ERROR / 缺失节点"""
    if node.type == "ERROR" or node.is_missing:
        return True
    return any(has_error(c) for c in node.children)


def node_lines(node) -> tuple[int, int]:
    """节点占的源行区间（1 基，含两端）"""
    s = node.start_point[0] + 1
    e = node.end_point[0] + 1 if node.end_point[1] > 0 else node.end_point[0]
    return s, max(s, e)


def node_text(node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8", "replace")


def name_of(node, src_bytes: bytes) -> str | None:
    n = node.child_by_field_name("name")
    return node_text(n, src_bytes) if n is not None else None


def extended_start(node, siblings: list, idx: int) -> int:
    """把紧贴在节点上方、行行相邻的注释 / 装饰器并进节点起点。

    JSDoc 和装饰器是节点的一部分语义：留在外面会被前导区或上一块带走，
    模型看到的就是被改写的代码（Python 版 effective_start 修的是同一件事）。
    只并「相邻」的兄弟节点，不跨空行、不越过前一个兄弟的结尾。
    """
    start, _ = node_lines(node)
    j = idx - 1
    while j >= 0:
        sib = siblings[j]
        if sib.type not in ("comment", "decorator"):
            break
        _, sib_end = node_lines(sib)
        if sib_end != start - 1:
            break
        start, _ = node_lines(sib)
        j -= 1
    return start


def declarator_label(decl_node, src_bytes: bytes) -> str:
    """声明语句的标签：第一个声明者的名字（解构取原文片段）"""
    for child in decl_node.children:
        if child.type == "variable_declarator":
            nm = child.child_by_field_name("name")
            if nm is None:
                return "decl"
            if nm.type == "identifier":
                return node_text(nm, src_bytes)
            return node_text(nm, src_bytes)[:20] or "decl"
    return "decl"


def is_function_valued_decl(decl_node) -> bool:
    for child in decl_node.children:
        if child.type == "variable_declarator":
            v = child.child_by_field_name("value")
            if v is not None and v.type in FUNC_VALUE_TYPES:
                return True
    return False


def member_label(member_expr, src_bytes: bytes) -> str:
    prop = member_expr.child_by_field_name("property")
    if prop is not None:
        return node_text(prop, src_bytes)
    return "member"


def class_items(cls_node, src_bytes: bytes, max_lines: int):
    """大类拆方法 → [(start, end, label, class_name)]；小类返回 None（整段走）"""
    cls_name = name_of(cls_node, src_bytes) or "Anonymous"
    body = cls_node.child_by_field_name("body")
    if body is None:
        return None
    items = []
    kids = list(body.children)
    for i, m in enumerate(kids):
        if m.type in ("method_definition", "field_definition", "static_block"):
            s = extended_start(m, kids, i)
            _, e = node_lines(m)
            if m.type == "method_definition":
                label = name_of(m, src_bytes) or "method"
            elif m.type == "field_definition":
                # 字段名挂在 property 域下（name 域是 None），私有字段去掉 #
                prop = (m.child_by_field_name("property")
                        or m.child_by_field_name("name"))
                label = (node_text(prop, src_bytes).lstrip("#")
                         if prop is not None else "field")
            else:
                label = "static"
            items.append((s, e, label, cls_name))
        elif m.type == "decorator":
            continue  # 装饰器由 extended_start 并进它修饰的方法
    return items


def collect_items(src_text: str, max_lines: int = DEFAULT_MAX_LINES):
    """→ (items, preamble_end)

    items: [(start, end, label, class_name|None)]，行号 1 基、含两端，
    body 区间逐字对应源文件。preamble_end 是第一个可切单元之前的行数
    （import / require / 顶层常量区），与 Python 版 preamble_end 同义。
    """
    src_bytes = src_text.encode("utf-8")
    tree = parse_js(src_text)
    root = tree.root_node
    items: list[tuple[int, int, str, str | None]] = []
    kids = list(root.children)

    def class_or_split(node, whole_label: str, ext_start: int):
        """类（或 export 包着的类）：小则整段，大则拆方法"""
        s0, e0 = node_lines(node)
        if e0 - s0 + 1 <= max_lines:
            items.append((ext_start, e0, whole_label, None))
        else:
            sub = class_items(node, src_bytes, max_lines)
            if sub:
                items.extend(sub)
            else:
                items.append((ext_start, e0, whole_label, None))

    for i, node in enumerate(kids):
        t = node.type
        s = extended_start(node, kids, i)
        _, e = node_lines(node)
        if t in FUNC_DECL_TYPES:
            items.append((s, e, name_of(node, src_bytes) or "func", None))
        elif t == "class_declaration":
            class_or_split(node, name_of(node, src_bytes) or "class", s)
        elif t == "export_statement":
            inner = next(
                (c for c in node.children
                 if c.type in FUNC_DECL_TYPES or c.type == "class_declaration"),
                None)
            if inner is not None and inner.type == "class_declaration":
                if e - s + 1 <= max_lines:
                    items.append(
                        (s, e, name_of(inner, src_bytes) or "class", None))
                else:
                    sub = class_items(inner, src_bytes, max_lines)
                    if sub:
                        items.extend(sub)
                    else:
                        items.append(
                            (s, e, name_of(inner, src_bytes) or "class", None))
            elif inner is not None:
                items.append(
                    (s, e, name_of(inner, src_bytes) or "func", None))
            else:
                # export { a, b } / export default expr —— 整句成块，别丢
                items.append((s, e, "export", None))
        elif t in ("lexical_declaration", "variable_declaration"):
            if is_function_valued_decl(node):
                items.append((s, e, declarator_label(node, src_bytes), None))
            # 非函数值的声明不单独成块：第一个单元之前归前导区，
            # 单元之间的会被合并区间逐字带上（与 Python 版一致）
        elif t == "expression_statement":
            assign = next(
                (c for c in node.children if c.type == "assignment_expression"),
                None)
            if assign is not None:
                lhs = assign.child_by_field_name("left")
                if lhs is not None and lhs.type == "member_expression":
                    # exports.main = … / X.prototype.m = … / obj.m = …
                    items.append((s, e, member_label(lhs, src_bytes), None))
        # 其余顶层语句（import、普通调用、纯数据声明）不单独成块

    if not items:
        return [], len(src_text.splitlines())
    preamble_end = min(it[0] for it in items) - 1
    return items, preamble_end


def merge_items(items, max_lines: int = DEFAULT_MAX_LINES):
    """把同一来源（同类或同为顶层）的相邻小块合并到 max_lines（与 Python 版同逻辑）"""
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

    body_start 是行号回映的**唯一真值**：前导区、分隔空行、补出来的
    `class X {` 头各占几行，在这里一次算清，别在外面重推。
    JS 与 Python 的差别只在类块：头之外还要在 body 之后补收尾 `}`，
    尾行在 body 之后，不影响 body_start。
    """
    out = list(lines[:pre_end])
    if out:
        out.append("")                       # 前导区与 body 之间的空行
    if cls:
        out.append(f"class {cls} {{")        # 方法块补类头（保持 body 原缩进）
    body_start = len(out) + 1
    out.extend(lines[s - 1:e])
    if cls:
        out.append("}")                      # 收尾：只补头不补尾是语法错误
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
    """是分段器自己产出的目录吗（有 manifest 或 chunk_*.js）"""
    if not p.is_dir():
        return False
    if (p / "manifest.json").exists():
        return True
    return any(p.glob("chunk_*.js")) or any(p.glob("chunk_*.py"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="按语法树切分大 JavaScript 文件，保证每块语法完整")
    ap.add_argument("source", help="要切的 JavaScript 源文件（.js/.mjs/.cjs/.jsx）")
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
    if src.suffix.lower() not in (".js", ".mjs", ".cjs", ".jsx"):
        print(f"v1 只支持 JavaScript 文件（.js/.mjs/.cjs/.jsx），收到: {src.suffix}",
              file=sys.stderr)
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
        print(f"目标目录已存在且不像分段输出目录（没有 manifest.json / chunk_*.js）：\n  {out}\n"
              "拒绝清空。确认要覆盖请加 --force。", file=sys.stderr)
        return 2

    src_text = src.read_text(encoding="utf-8")
    if has_error(parse_js(src_text).root_node):
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
        if has_error(parse_js(text).root_node):
            bad.append({"idx": i, "symbol": label,
                        "error": "tree-sitter re-parse has ERROR node"})
            continue
        safe = "".join(ch if ch.isalnum() or ch in "_-" else "_"
                       for ch in label.split("+")[0])[:40] or "x"
        fn = out / f"chunk_{i:02d}_{safe}.js"
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
         "language": LANGUAGE_NAME,
         "chunker": {"name": CHUNKER_NAME, "version": CHUNKER_VERSION},
         "preamble_lines": pre_end, "chunks": manifest,
         "failed": len(bad),
         # 明细而不只是计数：manifest 要标出缺口，那就真标出来
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
