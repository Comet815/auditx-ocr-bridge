#!/usr/bin/env python3
"""auditx-ocr-bridge 对抗测试探针

在仓库自带 34 例测试之外，专打四个疑点：
  P1  行号回映公式是否漏算前导区偏移（块文件里 preamble 占了 pre_end+1 行）
  P2  装饰器是否被切出函数体（Python 3.8+ FunctionDef.lineno 指向 def 行）
  P3  跨块假报过滤对中文 claim 是否完全失效
  P4  空前导区文件的块布局 + 杂项

纯标准库，Windows/Linux 通用。在仓库根目录运行：
  python ../bridge_probe.py   （或任意 cwd，自动定位仓库）
"""
from __future__ import annotations
import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "auditx-ocr-bridge"
sys.path.insert(0, str(ROOT))
import ocr_adapter as A  # noqa: E402


def section(title: str):
    print(f"\n{'─' * 8} {title} {'─' * 8}")


def chunk_fixture(tmp: Path):
    out = tmp / "chunks"
    r = subprocess.run([sys.executable, "chunk_big.py", "tests/fixture_big.py",
                        "--out", str(out), "--max-lines", "60", "--no-git"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    return out, manifest


def pre_end_of(src_text: str) -> int:
    tree = ast.parse(src_text)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.lineno - 1
    return len(src_text.splitlines())


# ───────────────────────────────────────────────────────────── P1 回映公式
def probe_remap(tmp: Path):
    section("P1 行号回映：前导区偏移")
    chunk_dir, _raw = chunk_fixture(tmp)
    manifest = A.load_chunk_manifest(chunk_dir / "manifest.json")
    fixture_src = (ROOT / "tests" / "fixture_big.py").read_text(encoding="utf-8")
    pe = pre_end_of(fixture_src)
    c1 = _raw["chunks"][0]
    s1 = c1["start_line"]
    chunk_text = (chunk_dir / c1["file"]).read_text(encoding="utf-8")
    chunk_lines = chunk_text.splitlines()

    print(f"fixture 前导区 pre_end = {pe} 行（docstring+import+常量）")
    print(f"块1 {c1['file']} 源区间 {c1['lines']}，块文件共 {len(chunk_lines)} 行")
    # 块文件里源第 s1 行（body 第 1 行）落在第几行？
    abs_line_of_body_start = chunk_lines.index(fixture_src.splitlines()[s1 - 1],
                                                pe) + 1 if pe else None
    print(f"按块文件绝对行号：body 第 1 行 = 块内第 {abs_line_of_body_start} 行"
          f"（= pre_end+2 = {pe + 2}？）")

    # 情形 a：模型按"块文件绝对行号"报缺陷（它看到的就是带前导的完整文件）
    #   若 body 第 1 行出缺陷，模型会报 start_line = pre_end + 2
    a = A.remap_lines({"path": c1["file"], "start_line": pe + 2}, manifest)
    print(f"\n[a] 假设模型报块文件绝对行号：body 首行缺陷 → 模型报 {pe + 2}")
    print(f"    适配器回映 → 源第 {a[0]} 行；真值应为源第 {s1} 行"
          f" → 偏移 {a[0] - s1:+d} 行 {'✗' if a[0] != s1 else '✓'}")

    # 情形 b：模型报的行号恰落在前导区里（比如抱怨某条 import）
    b = A.remap_lines({"path": c1["file"], "start_line": 3}, manifest)
    print(f"\n[b] 模型抱怨前导区第 3 行（块文件里就是源第 3 行的 docstring 内容）")
    print(f"    适配器回映 → 源第 {b[0]} 行（落进 body 区间 {c1['lines']} 里）✗")

    # 情形 c：仓库测试 B7 的断言 = 公式本身（循环）
    c = A.remap_lines({"path": c1["file"], "start_line": 3}, manifest)
    print(f"\n[c] 仓库 B7 断言「块内 3 行 → 源 {s1 + 2}」＝ 公式复读，非真值校验")
    print(f"    （块内第 3 行在块文件里其实是 {chunk_lines[2].strip()[:40]!r} —— "
          f"前导区内容，与源第 {s1 + 2} 行无关）")

    print(f"\n结论：公式成立当且仅当模型报的是「body 相对行号」。")
    print(f"若模型按块文件绝对行号报（最自然的行为），所有回映偏移 pre_end+1 = {pe + 1} 行。")
    print(f"裁决办法：一条已知向量 —— 在源文件埋 BAIT 行，看 OCR 报哪个数。")


# ───────────────────────────────────────────────────────────── P2 装饰器
def probe_decorators(tmp: Path):
    section("P2 装饰器边界（大类被按方法切开时）")
    body_a = "\n".join(f'        x{i} = "a" * 40 + str({i})' for i in range(12))
    body_b = "\n".join(f'        y{i} = "b" * 40 + str({i})' for i in range(12))
    src = tmp / "deco_src.py"
    src.write_text(
        'import functools\n'
        'CONST = 1\n'
        '\n'
        '@functools.lru_cache\n'          # 第一个顶层 def 的装饰器 → 会掉进前导区
        'def cached_fn(x):\n'
        '    return x * 2\n'
        '\n'
        'class Big:\n'
        '    def big_a(self):\n'
        + body_a + '\n'
        '        return x0\n'
        '\n'
        '    @property\n'                 # 方法装饰器 → 会被切出方法体
        '    def big_b(self):\n'
        + body_b + '\n'
        '        return y0\n',
        encoding="utf-8")
    out = tmp / "deco_chunks"
    r = subprocess.run([sys.executable, "chunk_big.py", str(src),
                        "--out", str(out), "--max-lines", "20", "--no-git"],
                       cwd=ROOT, capture_output=True, text=True)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    print(f"切成 {len(manifest['chunks'])} 块：")
    for c in manifest["chunks"]:
        text = (out / c["file"]).read_text(encoding="utf-8")
        pre = text.split("\n\nclass " if c["class"] else "\n\n", 1)[0]
        pre_tail = pre.splitlines()[-1].strip() if pre.splitlines() else ""
        tag = []
        if "@functools.lru_cache" in pre_tail:
            attach = "class 头" if c["class"] else "def"
            tag.append(f"前导区末尾的 @lru_cache 悬空 → 粘到本块 {attach} 上")
        if "big_b" in text and "@property" not in text:
            tag.append("@property 被切丢")
        flag = " ⚠ " + "；".join(tag) if tag else " ✓"
        print(f"  {c['file']:34} 源行 {c['lines']:8} {c['symbol']}{flag}")

    # 演示：方法块的 class 头前面是什么
    m_chunk = next(c for c in manifest["chunks"] if c["class"])
    text = (out / m_chunk["file"]).read_text(encoding="utf-8")
    i = text.index("class Big:")
    print(f"\n方法块 {m_chunk['file']} 里 class 头前的原文：")
    print("  " + text[max(0, i - 60):i + 12].replace("\n", "\\n"))
    print("\n结论：第一个顶层 def/class 的装饰器整行掉进前导区，被补进每一块；")
    print("      在方法块里它悬在 class 头上一行 → 装饰器语义错接（解析能过，模型看到的是假代码）。")
    print("      方法自己的装饰器（@property 等）则在按方法切分时被切出函数体，丢失。")


# ───────────────────────────────────────────────── P3 假报过滤的中文盲区
def probe_chinese_filter():
    section("P3 跨块假报过滤：中文 claim")
    symbols = A.collect_source_symbols(ROOT / "tests" / "fixture_big.py")
    cases = [
        ("`json` is never imported in this file", "英文句式"),
        ("`json` 未导入", "中文·未导入"),
        ("`json` 未定义", "中文·未定义"),
        ("`json` 缺少导入", "中文·缺少导入"),
        ("json 没有导入", "中文·无反引号"),
    ]
    for claim, label in cases:
        fake, why = A.is_chunk_artifact({"content": claim}, symbols)
        print(f"  {label:12} → {'丢弃' if fake else '保留'}  ({why or '不匹配任何判据'})")
    print("\n结论：_UNDEF_PAT 只认英文；中文语境的跨块假报会原样进账本。")
    print("      （方向是保守的——宁可漏杀不错杀，但这意味着中文报告下该过滤器基本空转）")


# ─────────────────────────────────────────────── P4 空前导区 + 杂项
def probe_misc(tmp: Path):
    section("P4 杂项")
    # 空前导区：文件第一行就是 def
    src = tmp / "nopre_src.py"
    src.write_text('def lone():\n    return 1\n' + 'def far():\n' +
                   '    return "x" * 300 + str(lone())\n', encoding="utf-8")
    out = tmp / "nopre_chunks"
    subprocess.run([sys.executable, "chunk_big.py", str(src), "--out", str(out),
                    "--max-lines", "10", "--no-git"],
                   cwd=ROOT, capture_output=True, text=True)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    text = (out / manifest["chunks"][0]["file"]).read_text(encoding="utf-8")
    print(f"  空前导区文件：块首 10 字符 = {text[:10]!r}  ← 两个空行开头")

    # OCR 输出后面跟了进度/日志行
    payload = json.dumps({"session_id": "s", "comments": [
        {"content": "bug", "path": "a.py", "start_line": 1}]},
        ensure_ascii=False)
    p = tmp / "ocr_dirty.json"
    p.write_text(payload + "\nScan complete in 12.3s\n", encoding="utf-8")
    try:
        A.load_ocr_json(p)
        print("  load_ocr_json：JSON 后有日志行 → 仍能解析 ✓")
    except Exception as ex:
        print(f"  load_ocr_json：JSON 后有日志行 → 崩溃 ✗（{type(ex).__name__}）")

    # 中文 category 归维度
    print(f"  map_dimension('安全') → {A.map_dimension('安全')}"
          f"（应为 security）")
    print(f"  map_dimension('性能') → {A.map_dimension('性能')}"
          f"（quality，恰好对，但纯属巧合）")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        probe_remap(tmp)
        probe_decorators(tmp)
        probe_chinese_filter()
        probe_misc(tmp)
    print("\n探针完毕。")
