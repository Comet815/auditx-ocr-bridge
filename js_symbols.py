#!/usr/bin/env python3
"""js_symbols — 从 JavaScript 源文件抽取符号表（给跨块假报过滤器用）

契约
    collect_js_symbols(path) -> set[str]

用途
    ocr_adapter 的假报过滤要回答一个问题：模型在某个块里说「X 未定义」，
    X 在**完整源文件**里到底有没有定义。有 → 跨块假报，丢弃；没有 → 真问题。
    Python 侧用 ast 抽符号表，本模块是 JS 侧的对应物，按文件后缀分派。

口径：宁可多收
    多收只会让过滤更保守（少丢几条假报），少收会把真假报放过去。
    所以除了契约必收项，本模块还收方法名、类字段名、普通标识符赋值目标。

必收（契约）
    - function / class 声明名（含 generator、export 包裹、函数表达式的名字）
    - import 绑定：默认导入、命名空间导入、命名导入（原名与别名都收）
    - require 变量：const x = require(...) 的绑定名（含解构）
    - const / let / var 声明名（含对象/数组解构、嵌套解构、默认值、剩余项）
    - 函数与箭头函数的参数（含解构参数、默认参数、剩余参数）
    - catch 参数
    - export 的名字（export { a, b as c } 的原名与别名、声明式 export 的名字）

用法
    from js_symbols import collect_js_symbols
    syms = collect_js_symbols("index.js")
    # 或命令行自检：python js_symbols.py index.js
"""
from __future__ import annotations

import sys
from pathlib import Path

import tree_sitter_javascript as _tsjs
from tree_sitter import Language, Parser

_LANG = Language(_tsjs.language())

# 语言内置全局：这些名字在文件里"必然可用"，不需要声明。
# 少了这张表会有系统性漏洞——任何提到 Number(...) / JSON.parse(...) / Object.keys(...)
# 的断言都满足不了"全部符号已定义"，跨块假报永远过滤不掉。
# 依据：ECMAScript 内建对象 + Node/CommonJS 的模块级全局。
JS_GLOBALS = frozenset({
    # ECMAScript 内建
    "Number", "String", "Boolean", "Object", "Array", "JSON", "Math", "Date",
    "RegExp", "Promise", "Map", "Set", "WeakMap", "WeakSet", "Symbol", "BigInt",
    "Proxy", "Reflect", "Intl", "ArrayBuffer", "SharedArrayBuffer", "DataView",
    "Uint8Array", "Int8Array", "Uint16Array", "Int16Array", "Uint32Array",
    "Int32Array", "Float32Array", "Float64Array", "BigInt64Array", "BigUint64Array",
    "Error", "TypeError", "RangeError", "SyntaxError", "ReferenceError",
    "EvalError", "URIError", "AggregateError",
    "parseInt", "parseFloat", "isNaN", "isFinite", "eval",
    "encodeURI", "encodeURIComponent", "decodeURI", "decodeURIComponent",
    "globalThis", "undefined", "NaN", "Infinity",
    # 定时器 / 微任务
    "setTimeout", "setInterval", "clearTimeout", "clearInterval", "queueMicrotask",
    # Node / CommonJS
    "console", "process", "require", "module", "exports", "__dirname", "__filename",
    "Buffer", "URL", "URLSearchParams", "TextEncoder", "TextDecoder",
    "structuredClone",
})


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _collect_pattern(node, src: bytes, out: set[str]):
    """把一个绑定模式（标识符/解构/默认值/剩余）里的名字全收进来"""
    if node is None:
        return
    t = node.type
    if t == "identifier":
        out.add(_text(node, src))
    elif t == "shorthand_property_identifier_pattern":
        out.add(_text(node, src))
    elif t == "object_pattern":
        for c in node.children:
            if c.type == "shorthand_property_identifier_pattern":
                out.add(_text(c, src))
            elif c.type == "pair_pattern":
                # 键也收：解构键是源文件里定义相邻的名字（口径：宁可多收，
                # 与 import/export 别名两头都收一致）
                key = c.child_by_field_name("key")
                if key is not None and key.type in (
                        "property_identifier", "identifier",
                        "shorthand_property_identifier_pattern"):
                    out.add(_text(key, src))
                _collect_pattern(c.child_by_field_name("value"), src, out)
            elif c.type == "object_assignment_pattern":
                # { d = 4 } 这种带默认值的简写绑定
                for g in c.children:
                    _collect_pattern(g, src, out)
            elif c.type == "rest_pattern":
                for g in c.children:
                    _collect_pattern(g, src, out)
    elif t == "array_pattern":
        for c in node.children:
            _collect_pattern(c, src, out)
    elif t == "assignment_pattern":
        _collect_pattern(node.child_by_field_name("left"), src, out)
    elif t == "rest_pattern":
        for c in node.children:
            _collect_pattern(c, src, out)
    # 其余（member_expression 等）不是绑定名，不收


def _collect_params(fn_node, src: bytes, out: set[str]):
    """函数类节点的参数：formal_parameters 或箭头函数的裸标识符参数"""
    params = fn_node.child_by_field_name("parameters")
    if params is not None:
        for c in params.children:
            if c.type in ("identifier", "object_pattern", "array_pattern",
                          "assignment_pattern", "rest_pattern"):
                _collect_pattern(c, src, out)
        return
    # 单参数无括号箭头函数：(x) => … 写成 x => …，参数是 parameter 域
    single = fn_node.child_by_field_name("parameter")
    if single is not None:
        _collect_pattern(single, src, out)


_FUNC_NODES = {
    "function_declaration", "generator_function_declaration",
    "function_expression", "generator_function_expression",
    "arrow_function", "method_definition",
}


def _walk(node, src: bytes, out: set[str]):
    t = node.type

    if t in ("function_declaration", "generator_function_declaration",
             "class_declaration", "function_expression",
             "generator_function_expression", "class_expression"):
        nm = node.child_by_field_name("name")
        if nm is not None:
            out.add(_text(nm, src))

    elif t == "method_definition":
        nm = node.child_by_field_name("name")
        if nm is not None:
            out.add(_text(nm, src).lstrip("#"))

    elif t == "field_definition":
        # 字段名挂在 property 域（与 chunk_js 踩过的同一个坑）
        prop = (node.child_by_field_name("property")
                or node.child_by_field_name("name"))
        if prop is not None:
            out.add(_text(prop, src).lstrip("#"))

    elif t == "variable_declarator":
        # const/let/var 的绑定名（含解构）；require 变量也是这种形态，
        # const { readFile } = require('fs') 的名字从解构模式里出
        _collect_pattern(node.child_by_field_name("name"), src, out)

    elif t == "import_statement":
        for c in node.children:
            if c.type != "import_clause":
                continue
            for g in c.children:
                if g.type == "identifier":
                    out.add(_text(g, src))                    # 默认导入
                elif g.type == "namespace_import":
                    for h in g.children:
                        if h.type == "identifier":
                            out.add(_text(h, src))            # * as ns
                elif g.type == "named_imports":
                    for sp in g.children:
                        if sp.type != "import_specifier":
                            continue
                        nm = sp.child_by_field_name("name")
                        al = sp.child_by_field_name("alias")
                        if nm is not None:
                            out.add(_text(nm, src))
                        if al is not None:
                            out.add(_text(al, src))

    elif t == "export_statement":
        for c in node.children:
            if c.type == "export_clause":
                for sp in c.children:
                    if sp.type != "export_specifier":
                        continue
                    nm = sp.child_by_field_name("name")
                    al = sp.child_by_field_name("alias")
                    if nm is not None:
                        out.add(_text(nm, src))
                    if al is not None:
                        out.add(_text(al, src))
        # 声明式 export（export function f…）的名字由子节点递归时收

    elif t == "catch_clause":
        _collect_pattern(node.child_by_field_name("parameter"), src, out)

    elif t == "assignment_expression":
        # 多收：普通标识符赋值目标（全局挂载、循环变量复用等）
        lhs = node.child_by_field_name("left")
        if lhs is not None and lhs.type == "identifier":
            out.add(_text(lhs, src))

    if t in _FUNC_NODES or t == "arrow_function":
        _collect_params(node, src, out)

    for c in node.children:
        _walk(c, src, out)


def collect_js_symbols(path) -> set[str]:
    """抽取 JS 源文件的符号表 → set[str]（契约函数）

    返回值包含语言内置全局（JS_GLOBALS）：问的是"这个名字在这个文件里可不可用"，
    内置对象永远可用，不该因为它们没被声明就被判成"未定义"。
    """
    src = Path(path).read_bytes()
    tree = Parser(_LANG).parse(src)
    out: set[str] = set()
    _walk(tree.root_node, src, out)
    out |= JS_GLOBALS
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    if len(sys.argv) != 2:
        print("用法: python js_symbols.py <file.js>", file=sys.stderr)
        sys.exit(2)
    for s in sorted(collect_js_symbols(sys.argv[1])):
        print(s)
