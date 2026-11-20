#!/usr/bin/env python3
"""自检：分段器的三条保证 + 适配器的映射逻辑

跑法：python tests/test_bridge.py   （不依赖 pytest）
退出码 0 全部通过，1 有失败。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS = []
PASSES = []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print(f"  OK   {name}")
    else:
        FAILS.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# ── A. 分段器 ────────────────────────────────────────────────────────────
def test_chunker(tmp):
    print("\n[A] 分段器 chunk_big.py")
    out = tmp / "chunks"
    r = run([sys.executable, "chunk_big.py", "tests/fixture_big.py",
             "--out", str(out), "--max-lines", "60", "--no-git"])
    check("A1 退出码 0", r.returncode == 0, r.stderr[-400:])

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    chunks = manifest["chunks"]
    check("A2 至少切出 2 块", len(chunks) >= 2, f"实际 {len(chunks)}")

    # A3：每块都能独立解析
    bad = []
    for c in chunks:
        try:
            ast.parse((out / c["file"]).read_text(encoding="utf-8"))
        except SyntaxError as ex:
            bad.append(f"{c['file']}: {ex}")
    check("A3 每块都能 ast.parse", not bad, "; ".join(bad))

    # A4：每块都带前导区（import / 常量）
    missing = []
    for c in chunks:
        text = (out / c["file"]).read_text(encoding="utf-8")
        if "from __future__ import annotations" not in text:
            missing.append(c["file"])
        if "MAX_ITEMS = 100" not in text:
            missing.append(c["file"] + "(常量缺)")
    check("A4 每块都补了前导区", not missing, ", ".join(missing[:3]))

    # A5：方法块带 class 头
    method_chunks = [c for c in chunks if c["class"]]
    check("A5 存在方法块", bool(method_chunks), "fixture 应有大类被切开")
    no_head = [c["file"] for c in method_chunks
               if f"class {c['class']}:" not in
               (out / c["file"]).read_text(encoding="utf-8")]
    check("A5b 方法块都带 class 头", not no_head, ", ".join(no_head[:3]))

    # A6：行号区间覆盖且不越界
    src_lines = manifest["source_lines"]
    oob = [c for c in chunks if c["end_line"] > src_lines or c["start_line"] < 1]
    check("A6 行号不越界", not oob, str(oob[:2]))

    # A7：行号单调递增
    starts = [c["start_line"] for c in chunks]
    check("A7 行号单调递增", starts == sorted(starts), str(starts))

    return out, chunks


# ── B. 适配器映射 ────────────────────────────────────────────────────────
def test_adapter(tmp, chunk_dir, chunks):
    print("\n[B] 适配器 ocr_adapter.py")
    import ocr_adapter as A

    check("B1 severity 四档映射",
          [A.map_severity(x) for x in ("critical", "high", "medium", "low")]
          == ["BLOCKER", "MAJOR", "MINOR", "INFO"])
    check("B2 未知 severity 降级 INFO", A.map_severity("nonsense") == "INFO")
    check("B3 None 降级 INFO", A.map_severity(None) == "INFO")

    check("B4 security 归类",
          A.map_dimension("security") == "security", A.map_dimension("security"))
    check("B5 bug 归 logic",
          A.map_dimension("bug") == "logic", A.map_dimension("bug"))
    check("B6 未知 category 落 quality",
          A.map_dimension("zzz") == "quality")

    # 行号回映：manifest 把块文件名映射到源文件行区间
    manifest = A.load_chunk_manifest(chunk_dir / "manifest.json")
    c = chunks[0]
    start = c["start_line"]
    comment = {"path": c["file"], "start_line": 3, "end_line": 5}
    s, e, _p = A.remap_lines(comment, manifest)
    check("B7 行号映射回源文件", s == start + 3 - 1,
          f"块内 3 行 → 源 {s}，期望 {start + 2}")

    # 证据转换
    ev = A.build_evidence({"existing_code": "x = 1", }, "sid")
    check("B8 existing_code → quote",
          ev and ev[0]["kind"] == "quote" and ev[0]["content"] == "x = 1", str(ev))
    ev2 = A.build_evidence({"existing_code": ""}, "sid")
    check("B9 无原文降级 ref",
          ev2 and ev2[0]["kind"] == "ref", str(ev2))

    # 跨块假报过滤：符号全在源文件里 → 判为假报
    fixture = ROOT / "tests" / "fixture_big.py"
    symbols = A.collect_source_symbols(fixture)
    check("B10 收集到源文件符号", len(symbols) > 20, f"{len(symbols)} 个")
    check("B10b 认得出 import 的 json", "json" in symbols)
    check("B10c 认得出形参 path", "path" in symbols)

    # 适配器读的字段是 comment["content"]（OCR 正文），不是 claim
    fake = {"content": "`json` is never imported in this file"}
    is_fake, why = A.is_chunk_artifact(fake, symbols)
    check("B11 跨块假报被识别", is_fake, why)

    real = {"content": "`sqlite3` is never imported in this file"}
    is_fake2, why2 = A.is_chunk_artifact(real, symbols)
    check("B12 真缺陷不被误杀", not is_fake2, why2)

    # 去重
    deduped, dropped = A.dedupe_findings([
        {"locator": "a.py:7-9", "severity": "MINOR", "claim": "x"},
        {"locator": "a.py:7-9", "severity": "BLOCKER", "claim": "y"},
        {"locator": "a.py:20", "severity": "INFO", "claim": "z"},
    ])
    check("B13 去重后剩 2 条", len(deduped) == 2, f"{len(deduped)}")
    kept = [f for f in deduped if f["locator"] == "a.py:7-9"][0]
    check("B14 去重保留高严重度", kept["severity"] == "BLOCKER", kept["severity"])
    check("B15 丢弃计数", len(dropped) == 1, f"{dropped}")

    # 校验器能抓出坏数据
    probs = A.validate({"dimension": "logic", "severity": "NOPE", "claim": "",
                        "evidence": [], "locator": ""})
    check("B16 校验抓到严重度/证据/断言问题", len(probs) >= 3, str(probs))


# ── C. 纯转换端到端 ──────────────────────────────────────────────────────
def test_end_to_end(tmp, chunk_dir):
    print("\n[C] 纯转换端到端")
    c = json.loads((chunk_dir / "manifest.json").read_text(encoding="utf-8"))["chunks"][0]
    ocr = {
        "summary": {"elapsed": "1s"},
        "comments": [
            {"path": c["file"], "start_line": 3, "end_line": 3, "severity": "high",
             "category": "security",
             "existing_code": "p = Path(path)",
             "thinking": "should not appear",
             "content": "path traversal risk: `path` is used without validation"},
            {"path": c["file"], "start_line": 100, "end_line": 100, "severity": "low",
             "category": "bug",
             "existing_code": "return 1",
             "content": "`json` is not imported in this chunk"},
        ],
    }
    p = tmp / "ocr.json"
    p.write_text(json.dumps(ocr, ensure_ascii=False), encoding="utf-8")

    r = run([sys.executable, "ocr_adapter.py", str(p), "--json",
             "--chunk-manifest", str(chunk_dir / "manifest.json"),
             "--source", "tests/fixture_big.py",
             "--source-name", "tests/fixture_big.py"])
    check("C1 退出码 0", r.returncode == 0, r.stderr[-400:])
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError as ex:
        check("C2 输出是合法 JSON", False, str(ex))
        return
    check("C2 输出是合法 JSON", True)
    check("C3 有 findings", len(out.get("findings", [])) >= 1)

    blob = json.dumps(out, ensure_ascii=False)
    check("C4 thinking 未进账本", "should not appear" not in blob)
    # 只看 findings，不看 stats——丢弃明细里会保留原文，那是审计留痕，不该删
    findings_blob = json.dumps(out.get("findings", []), ensure_ascii=False)
    check("C5 跨块假报未进 findings",
          "not imported in this chunk" not in findings_blob, "B 组那条应被丢弃")
    check("C5b 丢弃有留痕",
          out.get("stats", {}).get("dropped_chunk_artifacts") == 1,
          str(out.get("stats", {}).get("dropped_chunk_artifacts")))
    check("C6 行号已回映到源文件",
          f"tests/fixture_big.py:{c['start_line'] + 2}" in blob,
          "locator 应指向源文件行号")
    check("C7 有校验结果字段", "validation_problems" in out)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        chunk_dir, chunks = test_chunker(tmp)
        test_adapter(tmp, chunk_dir, chunks)
        test_end_to_end(tmp, chunk_dir)

    print(f"\n{'=' * 50}")
    print(f"通过 {len(PASSES)} · 失败 {len(FAILS)}")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  - " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
