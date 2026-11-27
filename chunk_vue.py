#!/usr/bin/env python3
"""chunk_vue — 把 .vue 单文件组件的 <script> 切成「每块都能独立解析」的分段

坐标系（本工具的全部难点，只在一处处理）
    manifest 输出的是 **.vue 坐标**：start_line / end_line 指 .vue 文件的行，
    body 是 .vue 里逐字复制的那几行，V11 能拿 quote 回 .vue 比对。
    做法是构造一个「虚拟源」：script 内容之前垫上与被跳过的 .vue 行数
    等量的注释行，让块文件里的行号与 .vue 行号处处对齐——前导区是这样，
    body 的 body_start 也自然落在 .vue 坐标上。平移只在这一处发生，
    collect / merge / build 全部复用 chunk_js / chunk_ts，不在分支里各加。

v1 范围（显式，不静默）
    - 只切**第一个** <script> 块。第二个块（常见是 <script setup> 与普通
      <script> 并存）不切，但会在 stdout 与 manifest 的 warnings 里写明
      它的行号区间——少审一半代码这种事不能悄悄发生。
    - <script> 标签须独占一行（格式化过的 SFC 都是如此）；标签同行带代码
      时明确报错退出，不猜偏移。
    - script 的 lang 属性决定语法：ts → typescript，tsx → tsx，
      缺省/js → javascript。切分与符号规则随之分派到 chunk_ts / chunk_js。

manifest：schema chunk-manifest/2，language="vue"，body_start 语义不变。

用法
    python chunk_vue.py src/page.vue --out .chunks
    python chunk_vue.py src/page.vue --out .chunks --max-lines 240 --no-git

退出码：0 全部成功；1 有块解析失败；2 参数/文件/结构错误
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import chunk_js
import chunk_ts

CHUNKER_NAME = "chunk_vue"
CHUNKER_VERSION = "0.1.0"
MANIFEST_SCHEMA = "chunk-manifest/2"
LANGUAGE_NAME = "vue"
DEFAULT_MAX_LINES = chunk_js.DEFAULT_MAX_LINES

_SCRIPT_OPEN = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(r"</script\s*>", re.IGNORECASE)
_LANG_ATTR = re.compile(r"""\blang\s*=\s*["']?([A-Za-z0-9_-]+)["']?""")


class ScriptBlock:
    def __init__(self, attrs: str, tag_line: int, content: str,
                 first_line: int, end_line: int):
        self.attrs = attrs
        self.tag_line = tag_line          # <script> 标签所在行（.vue，1 基）
        self.content = content            # script 原文（不含标签）
        self.first_line = first_line      # content 第一行对应的 .vue 行号
        self.end_line = end_line          # content 最后一行对应的 .vue 行号
        m = _LANG_ATTR.search(attrs)
        self.lang = (m.group(1).lower() if m else "javascript")
        self.setup = bool(re.search(r"\bsetup\b", attrs))

    @property
    def kind(self) -> str:
        return "script setup" if self.setup else "script"


def extract_script_blocks(text: str) -> list[ScriptBlock]:
    """抽出全部 <script> 块（含行号几何）；标签同行带代码 → ValueError"""
    blocks: list[ScriptBlock] = []
    pos = 0
    while True:
        mo = _SCRIPT_OPEN.search(text, pos)
        if mo is None:
            break
        mc = _SCRIPT_CLOSE.search(text, mo.end())
        if mc is None:
            raise ValueError("<script> 没有配对的 </script>")
        tag_line = text.count("\n", 0, mo.start()) + 1
        # 标签行在标签之后必须只剩空白，否则行几何没有干净定义
        line_end = text.find("\n", mo.end())
        rest = text[mo.end(): line_end if line_end != -1 else len(text)]
        if rest.strip():
            raise ValueError(
                f"<script> 标签同行带代码（第 {tag_line} 行），v1 不处理这种布局")
        content = text[mo.end(): mc.start()]
        if content.startswith("\n"):
            content = content[1:]
            first_line = tag_line + 1
        elif content.strip() == "":
            content = ""
            first_line = tag_line + 1
        else:
            raise ValueError(
                f"<script> 内容与标签同行（第 {tag_line} 行），v1 不处理这种布局")
        n_lines = content.count("\n") + (1 if content else 0)
        blocks.append(ScriptBlock(
            attrs=mo.group(1), tag_line=tag_line, content=content,
            first_line=first_line, end_line=first_line + n_lines - 1))
        pos = mc.end()
    return blocks


def _machinery(block: ScriptBlock):
    """按 lang 选切分器的一组函数 → (collect, parse_check, chunk_ext)"""
    if block.lang in ("ts", "typescript"):
        lang = chunk_ts.lang_for_suffix(".ts")
        return (lambda t, m: chunk_ts.collect_items(t, m, lang),
                lambda t: chunk_ts.has_error(chunk_ts.parse_ts(t, lang).root_node),
                ".ts")
    if block.lang == "tsx":
        lang = chunk_ts.lang_for_suffix(".tsx")
        return (lambda t, m: chunk_ts.collect_items(t, m, lang),
                lambda t: chunk_ts.has_error(chunk_ts.parse_ts(t, lang).root_node),
                ".tsx")
    return (chunk_js.collect_items,
            lambda t: chunk_js.has_error(chunk_js.parse_js(t).root_node),
            ".js")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="切分 .vue 文件的 <script>，manifest 输出 .vue 坐标")
    ap.add_argument("source", help="要切的 .vue 文件")
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
    if src.suffix.lower() != ".vue":
        print(f"只支持 .vue 文件，收到: {src.suffix}", file=sys.stderr)
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
    if out.exists() and not chunk_js.looks_like_chunk_dir(out) and not args.force:
        print(f"目标目录已存在且不像分段输出目录（没有 manifest.json / chunk_*）：\n  {out}\n"
              "拒绝清空。确认要覆盖请加 --force。", file=sys.stderr)
        return 2

    vue_text = src.read_text(encoding="utf-8")
    vue_lines = vue_text.splitlines()
    try:
        blocks = extract_script_blocks(vue_text)
    except ValueError as ex:
        print(str(ex), file=sys.stderr)
        return 2
    if not blocks:
        print("没有找到 <script> 块，无可切分", file=sys.stderr)
        return 2
    block = blocks[0]

    warnings: list[str] = []
    for extra in blocks[1:]:
        msg = (f"第二个 <{extra.kind}> 块（第 {extra.tag_line}–"
               f"{extra.end_line + 1} 行）v1 未切分，未进入审查")
        warnings.append(msg)
        print(f"⚠ {msg}", file=sys.stderr)

    collect, parse_check, chunk_ext = _machinery(block)
    script_lines = block.content.splitlines()
    if parse_check(block.content):
        print("script 内容本身解析就有 ERROR 节点，先修语法再切",
              file=sys.stderr)
        return 2
    items, pre_end = collect(block.content, args.max_lines)

    # ── 唯一的坐标平移点 ────────────────────────────────────────────────
    # 虚拟源 = 等量填充注释行 + script 行：虚拟行号 == .vue 行号。
    # items 与 pre_end 在这里一次性平移，下游 build/merge 不感知 .vue。
    off = block.first_line - 1
    pad = [f"// <vue> 前置内容（template 等）占位，第 {i + 1} 行"
           for i in range(off)]
    vlines = pad + script_lines
    vitems = [(s + off, e + off, label, cls) for (s, e, label, cls) in items]
    vpre_end = pre_end + off
    merged = chunk_js.merge_items(vitems, args.max_lines)

    chunk_js.force_rm(out)
    out.mkdir(parents=True)

    if not args.no_git:
        try:
            chunk_js._git(["init", "-q"], out)
            chunk_js._git(["config", "user.email", "chunker@local"], out)
            chunk_js._git(["config", "user.name", "chunker"], out)
        except (OSError, chunk_js.subprocess.CalledProcessError) as exc:
            print(f"! git 初始化失败（加 --no-git 可跳过）: {exc}",
                  file=sys.stderr)
            args.no_git = True

    manifest = []
    bad = []
    for i, (s, e, label, cls) in enumerate(merged, 1):
        text, body_start = chunk_js.build_chunk(vlines, vpre_end, s, e, cls)
        if parse_check(text):
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
        chunk_js._git(["add", "-A"], out)
        chunk_js._git(["commit", "-qm", "chunks"], out, check=False)

    print(f"\n切成 {len(manifest)} 块（.vue {len(vue_lines)} 行，"
          f"script 第 {block.first_line}–{block.end_line} 行，"
          f"lang={block.lang}，前导 {vpre_end} 行）")
    for c in manifest:
        tag = f"[{c['class']}]" if c["class"] else ""
        print(f"  {c['file']:48} 源行 {c['lines']:12} {c['n_lines']:4} 行 {tag}")

    if bad:
        print("\n✗ 有块无法解析，已跳过：", file=sys.stderr)
        for b in bad:
            print(f"   #{b['idx']} {b['symbol']}: {b['error']}", file=sys.stderr)

    mp = out / "manifest.json"
    doc = {"schema": MANIFEST_SCHEMA,
           "source": src.name, "source_lines": len(vue_lines),
           "language": LANGUAGE_NAME,
           "chunker": {"name": CHUNKER_NAME, "version": CHUNKER_VERSION},
           "preamble_lines": vpre_end, "chunks": manifest,
           "failed": len(bad),
           "failed_detail": bad}
    if warnings:
        doc["warnings"] = warnings
    mp.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"\nmanifest → {mp}")
    return 0 if not bad else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
