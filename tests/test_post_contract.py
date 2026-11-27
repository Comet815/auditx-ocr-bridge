#!/usr/bin/env python3
"""--post 字段契约测试（假引擎，走真实 CLI 子进程）。

背景：--post 把 finding 写进审查庭账本，引擎 review_brain_v2 是私有件、
不进公开仓，所以仓库 CI 一直验不到这条路。但「适配器到底给引擎传了
什么」是纯契约问题，不需要真引擎：本测试生成一个只记录参数的假
review_brain_v2 模块，用子进程跑真实 CLI（ocr_adapter.py --post），
断言记录到的 post_finding 参数。

与 test_bridge.py 里 F6 的分工：
  F6 是 in-process 版，它靠替换 A.validate 才能造出「校验不过」——
  因为走真实转换路径时，validate 查的每个字段都已被映射层规整
  （severity/dimension 只出合法枚举、claim 空的在 convert 就丢了、
  evidence 恒有内容、locator 有 <unknown> 兜底）。本文件 2026-11-27
  实测确认：构造不出能让 validate 在真实 CLI 下失败的 OCR 输入。
  所以这里锁的是 CLI 层真实存在的三条性质：
    P1 正常批次：post_finding 的字段名/类型/取值契约；
    P2 引擎缺失：--post 干净拒绝（SystemExit 文案），零入库；
    P3 结构垃圾（comments 非数组）：整批在入库前失败，零入库。
  validate 分支的 fail-closed 仍由 F6（in-process）覆盖，两边不重复。

运行：python tests/test_post_contract.py（无第三方依赖）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAPTER = HERE.parent / "ocr_adapter.py"

FAKE_ENGINE = '''\
import json, os
from pathlib import Path

class ReviewBrain:
    """只记录 post_finding 参数的假引擎，返回约定形状的回执"""
    def __init__(self, case_dir):
        self.case_dir = Path(case_dir)
        self.n = 0

    def post_finding(self, **kw):
        self.n += 1
        with open(os.environ["FAKE_BRAIN_RECORD"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(kw, ensure_ascii=False) + "\\n")
        return {"payload": {"finding_id": f"F-FAKE-{self.n:03d}"}}
'''

PASSES: list[str] = []
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    (PASSES if cond else FAILS).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))


def run_cli(ocr_path: Path, extra_args: list, engine_dir: Path | None,
            record: Path):
    env = dict(os.environ)
    env["FAKE_BRAIN_RECORD"] = str(record)
    if engine_dir is not None:
        env["PYTHONPATH"] = str(engine_dir)
    else:
        env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(ADAPTER), str(ocr_path)] + extra_args,
        capture_output=True, text=True, env=env)


def read_records(record: Path) -> list:
    if not record.exists():
        return []
    return [json.loads(ln) for ln in
            record.read_text(encoding="utf-8").splitlines() if ln.strip()]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        eng = td / "engine"
        eng.mkdir()
        (eng / "review_brain_v2.py").write_text(FAKE_ENGINE, encoding="utf-8")
        case = td / "case"
        record = td / "record.jsonl"

        # ── P1 正常批次的字段契约 ──────────────────────────────────────
        ocr = td / "ok.json"
        ocr.write_text(json.dumps({"session_id": "contract", "comments": [
            {"path": "a.py", "start_line": 10, "end_line": 10,
             "content": "此处有空指针风险", "existing_code": "x = obj.field",
             "suggestion_code": "x = obj.field if obj else None",
             "severity": "high", "category": "bug"},
            {"path": "b.py", "start_line": 3, "end_line": 4,
             "content": "缺少鉴权检查", "existing_code": "",
             "severity": "critical", "category": "安全"},
        ]}, ensure_ascii=False), encoding="utf-8")
        r = run_cli(ocr, ["--post", str(case), "--seat", "S-TEST"], eng, record)
        check("P1 --post 正常批次退出码 0", r.returncode == 0,
              f"rc={r.returncode} stderr={r.stderr.strip()[:200]}")
        recs = read_records(record)
        check("P2 两条 comment 恰好两次 post_finding", len(recs) == 2,
              f"实际 {len(recs)}")
        want_keys = {"seat_id", "dimension", "severity", "claim",
                     "evidence", "locator", "quote", "suggestion"}
        check("P3 字段名集合与契约一致（不多不少）",
              all(set(x) == want_keys for x in recs),
              f"实际 {[sorted(x) for x in recs]}")
        if len(recs) == 2:
            a, b = recs
            check("P4 seat_id 原样透传", a["seat_id"] == "S-TEST")
            check("P5 severity 映射进账本枚举",
                  a["severity"] == "MAJOR" and b["severity"] == "BLOCKER",
                  f"实际 {a['severity']}/{b['severity']}")
            check("P6 dimension 映射进账本枚举",
                  a["dimension"] == "logic" and b["dimension"] == "security",
                  f"实际 {a['dimension']}/{b['dimension']}")
            check("P7 evidence 是 [{kind, content}] 且 kind 合法",
                  all(isinstance(x["evidence"], list) and x["evidence"]
                      and all(set(e) == {"kind", "content"}
                              and e["kind"] in ("quote", "calc", "ref", "source")
                              for e in x["evidence"]) for x in recs))
            check("P8 有逐字证据时 kind=quote、无证据时降级 ref",
                  a["evidence"][0]["kind"] == "quote"
                  and b["evidence"][0]["kind"] == "ref")
            check("P9 locator/quote/claim/suggestion 均为 str",
                  all(isinstance(a[k], str) for k in
                      ("locator", "quote", "claim", "suggestion")))
            out = json.loads(r.stdout)
            check("P10 stdout 回执 posted=2 且带 finding_id",
                  out.get("posted") == 2
                  and out.get("posted_ids") == ["F-FAKE-001", "F-FAKE-002"],
                  f"实际 posted={out.get('posted')} ids={out.get('posted_ids')}")

        # ── P2 引擎缺失：干净拒绝、零入库 ─────────────────────────────
        record2 = td / "record2.jsonl"
        r = run_cli(ocr, ["--post", str(case), "--seat", "S-TEST"],
                    None, record2)
        check("P11 无引擎时 --post 非零退出且零入库",
              r.returncode != 0 and not record2.exists(),
              f"rc={r.returncode}")
        check("P12 无引擎时报错点名 review_brain_v2",
              "review_brain_v2" in (r.stderr + r.stdout))

        # ── P3 结构垃圾：整批在入库前失败 ─────────────────────────────
        bad = td / "bad.json"
        bad.write_text(json.dumps({"session_id": "x",
                                   "comments": {"0": {"content": "x"}}}),
                       encoding="utf-8")
        record3 = td / "record3.jsonl"
        r = run_cli(bad, ["--post", str(case), "--seat", "S-TEST"],
                    eng, record3)
        check("P13 comments 非数组时整批失败、零入库",
              r.returncode != 0 and not record3.exists(),
              f"rc={r.returncode}")

    print(f"\n通过 {len(PASSES)} · 失败 {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
