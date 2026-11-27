#!/usr/bin/env python3
"""diff 模式回归：ocr review（git diff 审查）→ ocr_adapter 的链路复验。

背景：2026-11-19 实测 diff 模式可用（同一批缺陷，diff 审查比整文件 scan
定位更准），但此后适配器与分段器迭代多次，diff 模式再没验过。
本脚本把那次手工验证变成一条命令可复跑：

  1. 造一个临时 git 仓库，提交基线文件，再引入一处明显的坏改动；
  2. 跑 `ocr review --format json`（上游 open-code-review 的 diff 模式）；
  3. 把输出喂给 ocr_adapter.py，断言：转换成功、至少 1 条 finding、
     且至少 1 条 finding 的 locator 指向被改动的文件。

断言只针对链路（我们的适配器吃不吃 diff 模式的输出），不针对模型
观点质量——模型说什么随模型版本变，不是本仓代码的回归信号。

环境要求：ocr CLI 可运行且已配置 LLM（ocr config / 环境变量）。
缺环境时脚本以退出码 3 明确报「环境缺失」，不当作链路失败。

用法：
  python qa/regress_diff_mode.py [--ocr-cmd "bun x @alibaba-group/open-code-review"]

退出码：0 通过 · 1 链路断言失败 · 2 用法/内部错误 · 3 环境缺失
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAPTER = HERE.parent / "ocr_adapter.py"

BASE_FILE = '''\
def total(prices):
    """小计：价格求和"""
    return sum(prices)
'''

CHANGED_FILE = '''\
def total(prices):
    """小计：价格求和后加税"""
    return sum(prices) * tax_rate
'''


def split_cmd(raw: str) -> list[str]:
    """把 --ocr-cmd 切成分段，跨平台都不出错。

    两个坑都在这里（2026-11-27 实测，见 tests/test_diff_mode_script.py）：

    1. POSIX 模式下 `C:\\\\Users\\\\...` 的反斜杠被当转义符吃掉，
       整条路径压成 `C:UsersAdministrator...`。
       → Windows 上必须用非 POSIX 模式。
    2. 非 POSIX 模式**会把引号留在 token 里**：
       `'"C:\\\\Python\\\\python.exe" "stub.py"'` 切出来是
       `['"C:\\\\Python\\\\python.exe"', '"stub.py"']`，
       subprocess 拿着带引号的名字去找文件，必然找不到。
       → 切完还要把成对的首尾引号剥掉。
    """
    parts = shlex.split(raw, posix=(os.name != "nt"))
    out: list[str] = []
    for p in parts:
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        out.append(p)
    return out


def run(cmd, cwd=None, **kw):
    """跑一条命令。找不到可执行文件时返回 rc=127 而不是抛异常——
    「工具没装」是环境问题，该走退出码 3，不该变成 traceback。"""
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(
            cmd, 127, "", f"找不到可执行文件 {cmd[0]!r}：{e.strerror}")
    except OSError as e:
        return subprocess.CompletedProcess(
            cmd, 127, "", f"无法执行 {cmd[0]!r}：{e.strerror}")


def main() -> int:
    ap = argparse.ArgumentParser(description="diff 模式（ocr review）链路回归")
    ap.add_argument("--ocr-cmd", default=os.environ.get("OCR_CMD", "ocr"),
                    help="ocr CLI 命令（默认取 $OCR_CMD 或 ocr）")
    args = ap.parse_args()
    # 切分口径见 split_cmd：Windows 用非 POSIX 模式（反斜杠不被当转义），
    # 并剥掉切分后残留的成对引号（非 POSIX 模式会把引号留在 token 里）。
    ocr_cmd = split_cmd(args.ocr_cmd)

    # ── 环境检查：ocr 能跑吗 ────────────────────────────────────────────
    probe = run(ocr_cmd + ["version"])
    if probe.returncode != 0:
        print(f"环境缺失：ocr CLI 不可运行（{args.ocr_cmd!r}）："
              f"{(probe.stderr or probe.stdout).strip()[:200]}",
              file=sys.stderr)
        return 3
    print(f"ocr CLI 可用：{probe.stdout.strip().splitlines()[-1][:80]}")

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        for cmd in (["git", "init", "-q"],
                    ["git", "config", "user.email", "regress@example.com"],
                    ["git", "config", "user.name", "regress"]):
            r = run(cmd, cwd=repo)
            if r.returncode != 0:
                print(f"内部错误：git 初始化失败：{r.stderr[:200]}",
                      file=sys.stderr)
                return 2
        target = repo / "calc.py"
        target.write_text(BASE_FILE, encoding="utf-8")
        run(["git", "add", "."], cwd=repo)
        run(["git", "commit", "-qm", "base"], cwd=repo)
        target.write_text(CHANGED_FILE, encoding="utf-8")
        print("已造仓库：calc.py 引入未定义的 tax_rate（坏改动，未提交）")

        # ── 跑 diff 模式 ────────────────────────────────────────────────
        ocr_out = Path(td) / "ocr-review.json"
        r = run(ocr_cmd + ["review", "--format", "json"], cwd=repo)
        (Path(td) / "ocr-stdout.txt").write_text(r.stdout, encoding="utf-8")
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()
            print("环境缺失或上游失败：ocr review 未能完成"
                  f"（rc={r.returncode}）：{tail[-1][:200] if tail else ''}",
                  file=sys.stderr)
            print("若报错指向 LLM 未配置：在本机执行 ocr config 配置模型后重跑；"
                  "这不是适配器链路的失败。", file=sys.stderr)
            return 3
        ocr_out.write_text(r.stdout, encoding="utf-8")

        # ── 过适配器 ────────────────────────────────────────────────────
        mapped = Path(td) / "mapped.json"
        r = run([sys.executable, str(ADAPTER), str(ocr_out),
                 "--json", "-o", str(mapped)])
        if r.returncode not in (0, 2) or not mapped.exists():
            print(f"链路失败：适配器异常（rc={r.returncode}）："
                  f"{r.stderr.strip()[:300]}", file=sys.stderr)
            return 1
        data = json.loads(mapped.read_text(encoding="utf-8"))
        findings = data.get("findings") or []
        problems = data.get("validation_problems") or []
        if problems:
            print(f"链路失败：适配器校验未过：{problems[:3]}", file=sys.stderr)
            return 1
        if not findings:
            print("链路失败：diff 模式输出经适配器后 0 条 finding"
                  "（模型没报，或输出形状变了导致 comments 没被读到）",
                  file=sys.stderr)
            return 1
        hit = [f for f in findings if "calc.py" in (f.get("locator") or "")]
        if not hit:
            print("链路失败：有 finding 但没有一条指向被改动的 calc.py："
                  f"{[f.get('locator') for f in findings][:5]}",
                  file=sys.stderr)
            return 1
        print(f"✅ diff 模式链路通过：ocr review → 适配器 → "
              f"{len(findings)} 条 finding，其中 {len(hit)} 条指向 calc.py")
        return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
