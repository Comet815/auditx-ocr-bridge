#!/usr/bin/env python3
"""qa/regress_diff_mode.py 自身的回归（假 ocr，不碰真模型）。

为什么要有这个文件
------------------
2026-11-27 那个 diff 模式脚本在 Windows 上炸了两次：一次是 `ocr` 找不到时抛
traceback（rc=1，和「链路断言失败」同码），一次是 `--ocr-cmd` 传 Windows 路径
被 `shlex.split` 吃掉反斜杠。两条都只在「真的执行了那段代码」时才暴露，
而 CI 从来只跑 `regress_chunker.py` 的七步几何，`regress_diff_mode.py`
一次都没进过 CI —— 这就是它们能活下来的原因。

本文件用假 ocr 顶替上游 CLI：链路的一端（脚本的 machinery、退出码语义、
参数解析）变成可回归的；另一端（真实模型输出）仍归那头真跑管，不在这里冒充。

分工写清楚：
  - 这里锁的是**脚本**的行为：退出码 0/1/3 的语义、参数解析、缺工具时不崩。
  - 真实 ocr + 真模型跑通 diff 模式的那条证据在 docs/DIFF-MODE.md，本文件不替代它。

依赖：python + git（脚本要造临时 git 仓库）。缺 git 时明确 SKIP，不假装通过。
运行：python tests/test_diff_mode_script.py
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "qa" / "regress_diff_mode.py"

PASSES: list[str] = []
FAILS: list[str] = []
SKIPS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    (PASSES if cond else FAILS).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


def skip(name: str, why: str):
    SKIPS.append(name)
    print(f"  SKIP {name} — {why}")


# ── 假 ocr ────────────────────────────────────────────────────────────────
# 两个变体：正常报一条（指向 calc.py，脚本的改动文件）、空报（0 条 comment）。
STUB_OK = '''
import json, sys
print(json.dumps({"session_id": "stub", "comments": [{
    "path": "calc.py", "start_line": 3, "end_line": 3,
    "content": "tax_rate 未定义", "existing_code": "return sum(prices) * tax_rate",
    "severity": "high", "category": "bug"}]}, ensure_ascii=False))
sys.exit(0)
'''

# 真实 OCR 会在 JSON 前后夹进度行；这条顺带验 load_ocr_json 的容忍度。
STUB_NOISY = '''
import json, sys
print("[ocr] scanning 1 file ...")
print(json.dumps({"session_id": "stub-noisy", "comments": [{
    "path": "calc.py", "start_line": 3,
    "content": "tax_rate 未定义", "severity": "high", "category": "bug"}]},
    ensure_ascii=False))
print("Scan complete in 1.2s")
sys.exit(0)
'''

STUB_EMPTY = '''
import json, sys
print(json.dumps({"session_id": "stub-empty", "comments": []}))
sys.exit(0)
'''

# 下面两个专门让 review 失败、但 version 正常——否则脚本卡在环境探测那一步，
# 测不到「上游失败」这条分支（2026-11-27 自己踩过一次）。
STUB_FAIL = '''
import sys
if "version" in sys.argv:
    print("open-code-review v0.0.0-stub (fail-on-review)"); sys.exit(0)
sys.stderr.write("upstream exploded\\n")
sys.exit(1)
'''

STUB_NO_LLM = '''
import sys
if "version" in sys.argv:
    print("open-code-review v0.0.0-stub (no-llm)"); sys.exit(0)
sys.stderr.write("no valid LLM endpoint configured; one of OCR_LLM_URL/OCR_LLM_TOKEN\\n")
sys.exit(3)
'''


def write_stub(d: Path, name: str, body: str) -> Path:
    p = d / f"{name}.py"
    p.write_text(body, encoding="utf-8")
    return p


def as_cmd(py: str, stub: Path) -> str:
    """把「解释器 + 脚本」拼成 --ocr-cmd 能吃的串。

    Windows 用双引号包路径（含空格也不怕），shlex 非 POSIX 模式不会动反斜杠。
    """
    return f'"{py}" "{stub}"'


def run_script(ocr_cmd: str, timeout: int = 120):
    cmd = [sys.executable, str(SCRIPT), "--ocr-cmd", ocr_cmd]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main() -> int:
    if not SCRIPT.exists():
        print(f"内部错误：找不到 {SCRIPT}", file=sys.stderr)
        return 2
    if shutil.which("git") is None:
        print("SKIP 全部：本机没有 git（脚本要造临时 git 仓库），"
              "这不是通过，只是没验。")
        return 0

    py = sys.executable
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ok = write_stub(td, "stub_ok", STUB_OK)
        noisy = write_stub(td, "stub_noisy", STUB_NOISY)
        empty = write_stub(td, "stub_empty", STUB_EMPTY)
        fail = write_stub(td, "stub_fail", STUB_FAIL)
        nollm = write_stub(td, "stub_nollm", STUB_NO_LLM)

        # ── 1. 正常链路：0 退出，且确认它真的跑到了断言 ────────────────
        r = run_script(as_cmd(py, ok))
        check("1 假 ocr 报一条 → 脚本 rc=0", r.returncode == 0,
              f"rc={r.returncode} out={(r.stdout + r.stderr).strip()[:300]}")
        check("2 通过时打印「链路通过」而不是静默退出",
              "链路通过" in r.stdout, f"stdout={r.stdout.strip()[:200]}")
        check("3 通过时点名了改动文件 calc.py",
              "calc.py" in r.stdout, f"stdout={r.stdout.strip()[:200]}")

        # ── 2. 真实 OCR 的噪声形状（进度行夹 JSON）也要吃下 ────────────
        r = run_script(as_cmd(py, noisy))
        check("4 JSON 前后夹进度行 → 仍 rc=0", r.returncode == 0,
              f"rc={r.returncode} out={(r.stdout + r.stderr).strip()[:300]}")

        # ── 3. 模型没报：这是链路失败（rc=1），不是环境缺失 ────────────
        r = run_script(as_cmd(py, empty))
        check("5 0 条 finding → rc=1（链路断言失败）", r.returncode == 1,
              f"rc={r.returncode} out={(r.stdout + r.stderr).strip()[:300]}")
        check("6 0 条时说明「0 条 finding」而不是笼统报错",
              "0 条 finding" in (r.stderr + r.stdout),
              f"stderr={r.stderr.strip()[:200]}")

        # ── 4. 上游失败 / 未配模型：环境问题 → rc=3 ────────────────────
        r = run_script(as_cmd(py, fail))
        check("7 上游 review 失败 → 脚本 rc=3（环境缺失，不是链路失败）",
              r.returncode == 3, f"rc={r.returncode}")
        check("8 上游失败时区分「review 未完成」而不是笼统崩掉",
              "ocr review 未能完成" in (r.stderr + r.stdout),
              f"out={(r.stderr + r.stdout).strip()[:300]}")
        r = run_script(as_cmd(py, nollm))
        check("9 上游报 LLM 未配置 → 脚本 rc=3 并提示 ocr config",
              r.returncode == 3 and "ocr config" in (r.stderr + r.stdout),
              f"rc={r.returncode} out={(r.stdout + r.stderr).strip()[:300]}")

        # ── 5. 工具不存在：干净 rc=3，绝不 traceback ───────────────────
        # 这正是 2026-11-27 在 Windows 上炸掉的那条路径（rc=1 + traceback）。
        r = run_script("definitely-not-a-real-ocr-binary-xyz")
        out = r.stdout + r.stderr
        check("10 找不到 ocr → rc=3 而不是崩栈", r.returncode == 3,
              f"rc={r.returncode} out={out.strip()[:300]}")
        check("11 找不到 ocr 时不打印 Traceback",
              "Traceback (most recent call last)" not in out,
              f"out={out.strip()[:300]}")
        check("12 找不到 ocr 时明说「环境缺失」",
              "环境缺失" in out, f"out={out.strip()[:300]}")

        # ── 6. 命令切分：反斜杠 / 引号 / 空格（三个坑都在这条链上）──────
        # 6a 无引号两段：解释器 + 脚本，路径都不含空格
        r = run_script(f"{py} {ok}")
        check("13 无引号的两段命令 → rc=0", r.returncode == 0,
              f"rc={r.returncode} out={(r.stdout + r.stderr).strip()[:300]}")
        # 6b 带引号：Windows 非 POSIX 切分会把引号留在 token 里，
        #    不剥掉就变成「找一个名字里带引号的文件」（2026-11-27 第三个坑）
        r = run_script(as_cmd(py, ok))
        check("14 带引号的命令 → rc=0（引号必须被剥掉）", r.returncode == 0,
              f"rc={r.returncode} out={(r.stdout + r.stderr).strip()[:300]}")

        # ── 7. 路径里有空格也要能起来 ──────────────────────────────────
        spacey = td / "dir with space"
        spacey.mkdir()
        spaced = write_stub(spacey, "stub_space", STUB_OK)
        r = run_script(as_cmd(py, spaced))
        check("15 路径含空格 → rc=0", r.returncode == 0,
              f"rc={r.returncode} out={(r.stdout + r.stderr).strip()[:300]}")

    print(f"\n通过 {len(PASSES)} · 失败 {len(FAILS)}"
          + (f" · 跳过 {len(SKIPS)}" if SKIPS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
