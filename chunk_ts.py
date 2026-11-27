#!/usr/bin/env python3
"""chunk_ts — 把大 TypeScript 文件切成「每块都能独立解析」的分段

与 chunk_js.py 同一套语义、同一份 manifest v2 契约，只换语法与节点表：
    - .ts / .mts / .cts 用 typescript 语法，.tsx 用 tsx 语法
      （JSX 在 typescript 语法下直接 ERROR，这条是实测出来的）
    - TS 特有顶层声明（interface / type / enum / abstract class / ambient）
      也算可切单元，不会被当成前导区或掉进合并间隙丢掉
    - 类成员多认 public_field_definition 与 abstract_method_signature /
      method_signature（接口与抽象类里没有函数体的方法签名）

三条保证与硬语义同 chunk_js.py：逐块重解析自检、前导区逐字复制、
类块补 `class X {` 头 + 收尾 `}`、body 逐字、body_start 在 build_chunk
里一次算清。manifest 的 language 字段写 "typescript"。

用法
    python chunk_ts.py src/big.ts --out .chunks
    python chunk_ts.py src/app.tsx --out .chunks --max-lines 240
    python chunk_ts.py src/big.ts --out .chunks --no-git

依赖：tree_sitter、tree_sitter_typescript（0.23.x，abi3 wheel）

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

import tree_sitter_typescript as _tsts
from tree_sitter import Language, Parser

CHUNKER_NAME = "chunk_ts"
CHUNKER_VERSION = "0.1.0"
MANIFEST_SCHEMA = "chunk-manifest/2"
LANGUAGE_NAME = "typescript"
DEFAULT_MAX_LINES = 240

_LANG_TS = Language(_tsts.language_typescript())
_LANG_TSX = Language(_tsts.language_tsx())

FUNC_VALUE_TYPES = {
    "arrow_function", "function_expression", "function",
    "generator_function", "generator_function_expression",
}
FUNC_DECL_TYPES = {"function_declaration", "generator_function_declaration"}
CLASS_TYPES = {"class_declaration", "abstract_class_declaration"}
# TS 特有顶层声明：整段成块（接口就算超长也不拆成员——拆接口要伪造
# 头尾且成员无体，收益低；与「超长单函数整段走」保持同一处理）
TS_WHOLE_TYPES = {
    "interface_declaration", "type_alias_declaration",
    "enum_declaration", "ambient_declaration",
}
# 类体内可拆成员（比 JS 多 public_field_definition 与两种方法签名）
MEMBER_TYPES = {
    "method_definition", "public_field_definition", "field_definition",
    "method_signature", "abstract_method_signature", "static_block",
}


def lang_for_suffix(suffix: str):
    return _LANG_TSX if suffix.lower() == ".tsx" else _LANG_TS


def parse_ts(text: str, lang):
    return Parser(lang).parse(text.encode("utf-8"))


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
    """紧贴上方、行行相邻的注释 / 装饰器并进节点起点（同 chunk_js）

    ⚠ 行尾注释不算前导注释：`const A = 1;  // note` 下一行就是函数时，
    那条注释属于上一条语句。并进来的话那一行会被吞进本块 body、其他块的前导区
    跟着丢它，preamble_lines 少算 —— 别的块缺这个常量，模型就会报「A 未定义」。
    """
    start, _ = node_lines(node)
    j = idx - 1
    while j >= 0:
        sib = siblings[j]
        if sib.type not in ("comment", "decorator"):
            break
        sib_start, sib_end = node_lines(sib)
        if sib_end != start - 1:
            break
        if j - 1 >= 0:
            _, prev_end = node_lines(siblings[j - 1])
            if sib_start <= prev_end:      # 与上一条语句同行 → 行尾注释，别并
                break
        start = sib_start
        j -= 1
    return start


def declarator_label(decl_node, src_bytes: bytes) -> str:
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
    """大类拆成员 → [(start, end, label, class_name)]；无类体返回 None"""
    cls_name = name_of(cls_node, src_bytes) or "Anonymous"
    body = cls_node.child_by_field_name("body")
    if body is None:
        return None
    items = []
    kids = list(body.children)
    for i, m in enumerate(kids):
        if m.type in MEMBER_TYPES:
            s = extended_start(m, kids, i)
            _, e = node_lines(m)
            if m.type in ("public_field_definition", "field_definition"):
                prop = (m.child_by_field_name("property")
                        or m.child_by_field_name("name"))
                label = (node_text(prop, src_bytes).lstrip("#")
                         if prop is not None else "field")
            elif m.type == "static_block":
                label = "static"
            else:
                label = name_of(m, src_bytes) or "method"
            items.append((s, e, label, cls_name))
        elif m.type == "decorator":
            continue
    return items


def collect_items(src_text: str, max_lines: int = DEFAULT_MAX_LINES, lang=None):
    """→ (items, preamble_end)；语义同 chunk_js.collect_items"""
    if lang is None:
        lang = _LANG_TS
    src_bytes = src_text.encode("utf-8")
    tree = parse_ts(src_text, lang)
    root = tree.root_node
    items: list[tuple[int, int, str, str | None]] = []
    kids = list(root.children)

    def class_or_split(node, whole_label: str, ext_start: int):
        s0, e0 = node_lines(node)
        if e0 - s0 + 1 <= max_lines:
            items.append((ext_start, e0, whole_label, None))
        else:
            sub = class_items(node, src_bytes, max_lines)
            if sub:
                items.extend(sub)
            else:
                items.append((ext_start, e0, whole_label, None))

    first_unit: int | None = None
    for i, node in enumerate(kids):
        t = node.type
        s = extended_start(node, kids, i)
        _, e = node_lines(node)
        n_before = len(items)
        if t in FUNC_DECL_TYPES:
            items.append((s, e, name_of(node, src_bytes) or "func", None))
        elif t in CLASS_TYPES:
            class_or_split(node, name_of(node, src_bytes) or "class", s)
        elif t in TS_WHOLE_TYPES:
            label = name_of(node, src_bytes) or t.replace("_declaration", "")
            items.append((s, e, label, None))
        elif t == "export_statement":
            inner = next(
                (c for c in node.children
                 if c.type in FUNC_DECL_TYPES or c.type in CLASS_TYPES
                 or c.type in TS_WHOLE_TYPES),
                None)
            if inner is not None and inner.type in CLASS_TYPES:
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
                    (s, e, name_of(inner, src_bytes)
                     or inner.type.replace("_declaration", ""), None))
            else:
                # export const f = () => … —— 拿声明者名字当标签
                lex = next((c for c in node.children
                            if c.type in ("lexical_declaration",
                                          "variable_declaration")), None)
                if lex is not None and is_function_valued_decl(lex):
                    items.append((s, e, declarator_label(lex, src_bytes), None))
                else:
                    items.append((s, e, "export", None))
        elif t in ("lexical_declaration", "variable_declaration"):
            if is_function_valued_decl(node):
                items.append((s, e, declarator_label(node, src_bytes), None))
        elif t == "expression_statement":
            assign = next(
                (c for c in node.children if c.type == "assignment_expression"),
                None)
            if assign is not None:
                lhs = assign.child_by_field_name("left")
                if lhs is not None and lhs.type == "member_expression":
                    items.append((s, e, member_label(lhs, src_bytes), None))
        if len(items) > n_before and first_unit is None:
            # 前导区基准 = 第一个产出单元的**顶层节点**起点。被拆成方法的类，
            # 基准是 class 行本身——若误取第一个方法的行号，`class X {` 会
            # 掉进前导区被补进每一块：方法块出现双 class 头、顶层块挂着未
            # 闭合的 class，所有块解析失败。class-first 文件
            # （2026-11-27 外部验收回测发现，regress_chunker 抓的）。
            first_unit = s

    if not items:
        # 没有可切单元时不能返回空——空 = 静默逃过审查。
        # 退化：顶层语句各自当单元，交给 merge_items 合并（同 chunk_js）。
        for node in kids:
            s0, e0 = node_lines(node)
            items.append((s0, e0, "top", None))
    if not items:
        return [], len(src_text.splitlines())
    preamble_end = (first_unit if first_unit is not None
                    else min(it[0] for it in items)) - 1
    return items, preamble_end


def merge_items(items, max_lines: int = DEFAULT_MAX_LINES):
    """同一来源的相邻小块合并到 max_lines（同 chunk_js）"""
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
    """→ (块文本, body_start)；body_start 在这里一次算清（同 chunk_js）"""
    out = list(lines[:pre_end])
    if out:
        out.append("")
    if cls:
        out.append(f"class {cls} {{")
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
    return (any(p.glob("chunk_*.ts")) or any(p.glob("chunk_*.tsx"))
            or any(p.glob("chunk_*.js")) or any(p.glob("chunk_*.py")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="按语法树切分大 TypeScript 文件，保证每块语法完整")
    ap.add_argument("source", help="要切的 TypeScript 源文件（.ts/.tsx/.mts/.cts）")
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
    suffix = src.suffix.lower()
    if suffix not in (".ts", ".tsx", ".mts", ".cts"):
        print(f"只支持 TypeScript 文件（.ts/.tsx/.mts/.cts），收到: {suffix}",
              file=sys.stderr)
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
        print(f"目标目录已存在且不像分段输出目录（没有 manifest.json / chunk_*.ts）：\n  {out}\n"
              "拒绝清空。确认要覆盖请加 --force。", file=sys.stderr)
        return 2

    lang = lang_for_suffix(suffix)
    chunk_ext = ".tsx" if suffix == ".tsx" else ".ts"
    # 同 chunk_js：带 BOM 的源文件读进来就剥掉，别让 U+FEFF 混进块体。
    try:
        src_text = src.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        print(f"源文件不是合法的 UTF-8 文本，无法切分: {src}", file=sys.stderr)
        return 2
    if has_error(parse_ts(src_text, lang).root_node):
        print("源文件本身解析就有 ERROR 节点（.tsx 请确认后缀，JSX 需 tsx 语法）",
              file=sys.stderr)
        return 2
    lines = src_text.splitlines()
    items, pre_end = collect_items(src_text, args.max_lines, lang)
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
        if has_error(parse_ts(text, lang).root_node):
            bad.append({"idx": i, "symbol": label,
                        "error": "tree-sitter re-parse has ERROR node"})
            continue
        safe = "".join(ch if ch.isalnum() or ch in "_-" else "_"
                       for ch in label.split("+")[0])[:40] or "x"
        fn = out / f"chunk_{i:02d}_{safe}{chunk_ext}"
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
