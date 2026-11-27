#!/usr/bin/env python3
"""输入编码/换行变体的验收：六种语言 × {UTF-8 BOM, CRLF, 无尾换行}。

为什么要有这个文件
------------------
2026-11-27 抽查发现：整个仓库对 BOM / CRLF / 无尾换行**零覆盖**。
实测六种语言跑下来，只有 Python + BOM 会挂（`ast.parse()` 吃 str 时行首
U+FEFF 直接 SyntaxError），其余五种走 tree-sitter，本来就容得下。
也就是说：这条路径一直是「碰巧能过」，没有任何东西拦着它将来坏掉。

做法
----
不在仓库里存二进制夹具（CRLF 会被 .gitattributes 规范化、BOM 又没法肉眼 review），
改成**运行时从现有夹具生成变体**，再用 `qa/regress_chunker.py`（实现无关的验收工具）
跑几何真值。生成 → 验收 → 删临时目录，仓库里不留痕迹。

bases 说明：每种语言的文件名跟着夹具走（chunk_vue 靠 .vue 后缀认语言等），
所以变体文件沿用同一个后缀，只换内容字节。

运行：python tests/test_input_encodings.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURES = HERE / "fixtures"
REGRESS = ROOT / "qa" / "regress_chunker.py"

# (语言标签, --cmd 字符串, 夹具相对 fixtures/ 的路径)
# 用裸 "python" 而不是 sys.executable：regress_chunker 是 `args.cmd.split()`
# 切命令，解释器路径里一旦有空格（如 Program Files）就会被切断。
CASES = [
    ("python",     "python chunk_big.py",  "python/sample.py"),
    ("javascript", "python chunk_js.py",   "js/sample.js"),
    ("typescript", "python chunk_ts.py",   "ts/sample.ts"),
    ("vue",        "python chunk_vue.py",  "vue/sample.vue"),
    ("go",         "python chunk_go.py",   "go/sample.go"),
    ("rust",       "python chunk_rust.py", "rust/sample.rs"),
]

VARIANTS = ("bom", "crlf", "notail")

PASSES: list[str] = []
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    (PASSES if cond else FAILS).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


def render(text: str, variant: str) -> bytes:
    """把一份源文本渲染成目标字节序列。"""
    if variant == "crlf":
        body = text.replace("\r\n", "\n").replace("\n", "\r\n")
        return body.encode("utf-8")
    if variant == "notail":
        return text.rstrip("\r\n").encode("utf-8")
    if variant == "bom":
        return b"\xef\xbb\xbf" + text.replace("\r\n", "\n").encode("utf-8")
    raise ValueError(variant)


def run_regress(cmd_str: str, target: Path, ext: str):
    full = [sys.executable, str(REGRESS), "--cmd", cmd_str,
            "--target", str(target), "--ext", ext, "--max-lines", "20"]
    return subprocess.run(full, cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def main() -> int:
    if not REGRESS.exists():
        print(f"内部错误：找不到 {REGRESS}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for label, cmd, rel in CASES:
            src = FIXTURES / rel
            if not src.exists():
                check(f"{label}: 夹具存在", False, f"缺 {rel}")
                continue
            text = src.read_text(encoding="utf-8-sig")
            ext = src.suffix
            for variant in VARIANTS:
                work = td / f"{label}_{variant}"
                work.mkdir(parents=True, exist_ok=True)
                target = work / src.name
                target.write_bytes(render(text, variant))
                r = run_regress(cmd, work, ext)
                ok = r.returncode == 0
                check(f"{label} / {variant}", ok,
                      (r.stdout + r.stderr).strip().splitlines()[-1][:220]
                      if (r.stdout + r.stderr).strip() else "")

    print(f"\n通过 {len(PASSES)} · 失败 {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
