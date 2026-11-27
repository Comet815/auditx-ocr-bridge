#!/usr/bin/env python3
"""go_symbols — 从 Go 源文件抽取符号表（给跨块假报过滤器用）

契约（同 js_symbols）
    collect_go_symbols(path) -> set[str]

口径：宁可多收（理由同 js_symbols：多收只让过滤更保守，少收放过假报）。

收的范围
    - 函数名、方法名（receiver 方法与普通函数同等对待）
    - 类型名（type_spec，含分组声明里的每个 spec）
    - const / var 的 spec 名（含分组）
    - import 绑定：别名优先；无别名时按路径基名收（fmt、os 这类默认包名
      正是模型会引用的名字）；点导入与空白导入不产生可用名，跳过
    - 参数名（parameter_declaration 的 identifier 子节点，含 receiver；
      无名参数没有 identifier，天然不收）
    - struct 字段名、interface 方法名（method_elem）
    - 泛型类型参数名

不收：函数体内的局部变量。chunk_go 从不把一个函数体切开，局部名
永远和它的函数在同一块，不存在跨块误报面。
"""
from __future__ import annotations

import sys
from pathlib import Path

import tree_sitter_go as _tsgo
from tree_sitter import Language, Parser

_LANG = Language(_tsgo.language())


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _name(node, src: bytes, out: set[str]):
    nm = node.child_by_field_name("name") if node is not None else None
    if nm is not None:
        out.add(_text(nm, src))


def _walk(node, src: bytes, out: set[str]):
    t = node.type

    if t in ("function_declaration", "method_declaration",
             "type_spec", "const_spec", "var_spec", "method_elem",
             "type_parameter_declaration",
             "variadic_parameter_declaration"):
        _name(node, src, out)

    elif t == "field_declaration":
        # struct 字段：一行可声明多个名（a, b int），name 域可重复
        for nm in node.children_by_field_name("name"):
            out.add(_text(nm, src))

    elif t == "parameter_declaration":
        # 参数名就是 identifier 子节点（a, b int 有两个）；receiver 同理
        for c in node.children:
            if c.type == "identifier":
                out.add(_text(c, src))

    elif t == "import_spec":
        nm = node.child_by_field_name("name")
        # 别名的节点类型是 package_identifier（不是 identifier，第一版踩过）；
        # 点导入的 name 是 dot 节点，天然排除
        if nm is not None and nm.type in ("identifier", "package_identifier"):
            out.add(_text(nm, src))                    # 别名导入
        elif nm is None:
            p = node.child_by_field_name("path")
            if p is not None:
                raw = _text(p, src).strip("\"'`")
                base = raw.rstrip("/").rsplit("/", 1)[-1]
                if base:
                    out.add(base)                      # 默认包名 = 路径基名
        # "." 点导入与 "_" 空白导入不产生可引用名，跳过

    for c in node.children:
        _walk(c, src, out)


def collect_go_symbols(path) -> set[str]:
    """抽取 Go 源文件的符号表 → set[str]（契约函数）"""
    src = Path(path).read_bytes()
    tree = Parser(_LANG).parse(src)
    out: set[str] = set()
    _walk(tree.root_node, src, out)
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    if len(sys.argv) != 2:
        print("用法: python go_symbols.py <file.go>", file=sys.stderr)
        sys.exit(2)
    for s in sorted(collect_go_symbols(sys.argv[1])):
        print(s)
