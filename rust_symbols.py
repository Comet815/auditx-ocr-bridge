#!/usr/bin/env python3
"""rust_symbols — 从 Rust 源文件抽取符号表（给跨块假报过滤器用）

契约（同 js_symbols）
    collect_rust_symbols(path) -> set[str]

口径：宁可多收（理由同 js_symbols：多收只让过滤更保守，少收放过假报）。

收的范围
    - fn 名（含 impl / trait 里的成员函数与 trait 方法签名）
    - struct / enum / trait / type / mod / const / static 的名字
    - enum 变体名、struct 字段名
    - use 引入的名字（路径各段与别名都收，口径同 import 别名两头收）
    - 函数参数名（parameter 的 pattern，含解构）与 let 绑定名
    - 泛型类型参数名、macro_rules 名
    - self（方法里永远可用）
"""
from __future__ import annotations

import sys
from pathlib import Path

import tree_sitter_rust as _tsrs
from tree_sitter import Language, Parser

_LANG = Language(_tsrs.language())

# std prelude + 常用宏：任何 Rust 文件里都可直接用，不需要 use。
# 理由同 js_symbols.JS_GLOBALS / go_symbols.GO_BUILTINS：
# 过滤器的判据是「全部符号已定义」，缺了这张表，提到 Option / Vec / String /
# println 的跨块假报在 Rust 侧永远过滤不掉。
RUST_PRELUDE = frozenset({
    # prelude 里的类型与枚举
    "Option", "Some", "None", "Result", "Ok", "Err",
    "Vec", "String", "Box", "Rc", "Arc", "Cell", "RefCell", "Cow",
    "HashMap", "HashSet", "BTreeMap", "BTreeSet", "VecDeque", "BinaryHeap",
    "Iterator", "IntoIterator", "Extend", "ExactSizeIterator",
    "Clone", "Copy", "Debug", "Display", "Default", "PartialEq", "Eq",
    "PartialOrd", "Ord", "Hash", "Send", "Sync", "Sized", "Unpin", "Drop",
    "Fn", "FnMut", "FnOnce", "From", "Into", "TryFrom", "TryInto",
    "AsRef", "AsMut", "ToString", "ToOwned",
    # 常用宏（按裸名收，模型引用时通常不带 ! ）
    "panic", "print", "println", "eprint", "eprintln", "format", "vec",
    "assert", "assert_eq", "assert_ne", "debug_assert", "debug_assert_eq",
    "todo", "unimplemented", "unreachable", "write", "writeln", "matches", "dbg",
    # 字面零值
    "true", "false",
})

_PATTERN_TYPES = {
    "tuple_pattern", "tuple_struct_pattern", "struct_pattern",
    "reference_pattern", "mut_pattern", "or_pattern", "slice_pattern",
    "captured_pattern", "array_pattern",
}


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _collect_pattern(node, src: bytes, out: set[str]):
    """绑定模式里的名字（标识符/元组/结构解构/引用/或模式）"""
    if node is None:
        return
    t = node.type
    if t == "identifier":
        out.add(_text(node, src))
    elif t == "field_pattern":
        nm = node.child_by_field_name("name")
        if nm is not None:
            out.add(_text(nm, src))
        _collect_pattern(node.child_by_field_name("pattern"), src, out)
    elif t in _PATTERN_TYPES:
        for c in node.children:
            _collect_pattern(c, src, out)
    # 其余（字面量、范围、通配）不是绑定名


def _collect_params(fn_node, src: bytes, out: set[str]):
    params = fn_node.child_by_field_name("parameters")
    if params is None:
        return
    for c in params.children:
        if c.type == "parameter":
            _collect_pattern(c.child_by_field_name("pattern"), src, out)
        elif c.type == "self_parameter":
            out.add("self")


def _walk(node, src: bytes, out: set[str]):
    t = node.type

    if t in ("function_item", "function_signature_item",
             "struct_item", "enum_item", "trait_item", "type_item",
             "mod_item", "const_item", "static_item", "macro_definition",
             "enum_variant"):
        nm = node.child_by_field_name("name")
        if nm is not None:
            out.add(_text(nm, src))
        if t in ("function_item", "function_signature_item"):
            _collect_params(node, src, out)

    elif t == "field_declaration":
        nm = node.child_by_field_name("name")
        if nm is not None:
            out.add(_text(nm, src))

    elif t == "use_declaration":
        # 引入名：路径各段标识符与别名全收（宁可多收）；crate/self/super
        # 是路径关键字不是绑定名，排除
        stack = list(node.children)
        while stack:
            c = stack.pop()
            if c.type == "identifier":
                out.add(_text(c, src))
            elif c.type == "use_as_clause":
                al = c.child_by_field_name("alias")
                if al is not None:
                    out.add(_text(al, src))
                stack.extend(c.children)
            elif c.type in ("scoped_identifier", "scoped_use_list",
                            "use_list"):
                stack.extend(c.children)

    elif t == "let_declaration":
        _collect_pattern(node.child_by_field_name("pattern"), src, out)

    elif t in ("type_parameter", "constrained_type_parameter"):
        nm = node.child_by_field_name("name")
        if nm is not None:
            out.add(_text(nm, src))

    for c in node.children:
        _walk(c, src, out)


def collect_rust_symbols(path) -> set[str]:
    """抽取 Rust 源文件的符号表 → set[str]（契约函数）

    返回值包含 **prelude**（std 预导入的名字）与常用宏：它们在任何 Rust 文件里
    都可直接使用，不需要 `use`。少了这张表，任何提到 Option / Vec / String /
    println 的断言在 Rust 侧永远满足不了「全部符号已定义」，跨块假报过滤不掉。
    """
    src = Path(path).read_bytes()
    tree = Parser(_LANG).parse(src)
    out: set[str] = set()
    _walk(tree.root_node, src, out)
    out |= RUST_PRELUDE
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    if len(sys.argv) != 2:
        print("用法: python rust_symbols.py <file.rs>", file=sys.stderr)
        sys.exit(2)
    for s in sorted(collect_rust_symbols(sys.argv[1])):
        print(s)
