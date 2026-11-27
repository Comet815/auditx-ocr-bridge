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
    #
    # 注意这里不再断言公式本身（旧版 B7/C6 就是这么写的：拿实现当真值，
    # 循环论证，所以前导区偏移的 bug 一直没被拦住）。现在拿**源文件真值**断言：
    # 块文件里 body_start 那一行，必须逐字等于源文件 start_line 那一行。
    manifest = A.load_chunk_manifest(chunk_dir / "manifest.json")
    c = chunks[0]
    body_start = c["body_start"]
    chunk_lines = (chunk_dir / c["file"]).read_text(encoding="utf-8").splitlines()

    def chunk_line(n):
        return chunk_lines[n - 1].strip() if n - 1 < len(chunk_lines) else "<越界>"

    def src_line(n):
        return (ROOT / "tests" / "fixture_big.py").read_text(
            encoding="utf-8").splitlines()[n - 1].strip()

    check("B7a body_start 指向的块内行 == 源 start_line 行",
          chunk_line(body_start) == src_line(c["start_line"]),
          f"块内第 {body_start} 行 {chunk_line(body_start)!r} vs 源第 {c['start_line']} 行 "
          f"{src_line(c['start_line'])!r}")

    # 真值：block 内 body_start+k 行，就是源文件 start_line+k 行
    comment = {"path": c["file"], "start_line": body_start + 2,
               "end_line": body_start + 4}
    s, e, _p = A.remap_lines(comment, manifest)
    check("B7b 行号映射回源文件（真值）", s == c["start_line"] + 2,
          f"块内第 {body_start + 2} 行 → 源 {s}，期望 {c['start_line'] + 2}")
    check("B7c 结束行同样回映", e == c["start_line"] + 4, f"→ {e}")

    # 模型报前导区行号时，前导区是逐字复制的，源行号应当原样返回
    s2, _e2, _p2 = A.remap_lines({"path": c["file"], "start_line": 2}, manifest)
    check("B7d 前导区行号原样返回", s2 == 2, f"→ 源 {s2}")

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
    body_line = c["body_start"] + 1        # 块文件里 body 的第 2 行
    ocr = {
        "summary": {"elapsed": "1s"},
        "comments": [
            {"path": c["file"], "start_line": body_line, "end_line": body_line,
             "severity": "high", "category": "security",
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
    check("C6 行号已回映到源文件（真值）",
          f"tests/fixture_big.py:{c['start_line'] + 1}" in blob,
          f"块内 body 第 2 行应回映到源 {c['start_line'] + 1}")
    check("C7 有校验结果字段", "validation_problems" in out)


# ── D. 已修 bug 的回归（F1/F2/F3/F4/F5/F7/F9/F10/F12）───────────────────

def write_and_chunk(tmp, name, text, max_lines):
    src = tmp / f"{name}_src.py"
    src.write_text(text, encoding="utf-8")
    out = tmp / f"{name}_chunks"
    r = run([sys.executable, "chunk_big.py", str(src), "--out", str(out),
             "--max-lines", str(max_lines), "--no-git"])
    assert r.returncode == 0, r.stderr
    return out, json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def chunk_text(out, c):
    return (out / c["file"]).read_text(encoding="utf-8")


def deco_source():
    lines = ["import functools", "CONST = 1", "",
             "@functools.lru_cache",            # 第一个顶层函数的装饰器
             "def cached_fn(x):", "    return x * 2", "", "",
             "class Big:", "    def big_a(self):"]
    lines += [f'        x{i} = "a" * 40 + str({i})' for i in range(10)]
    lines += ["        return x0", "", "    @property", "    def big_b(self):"]
    lines += [f'        y{i} = "b" * 40 + str({i})' for i in range(10)]
    lines += ["        return y0"]
    return "\n".join(lines) + "\n"


def test_decorators(tmp):
    """F2：装饰器不该掉进前导区，也不该被切丢"""
    print("\n[D] 装饰器边界（F2）")
    out, dm = write_and_chunk(tmp, "deco", deco_source(), 20)
    chunks = dm["chunks"]

    lru = [c for c in chunks if "@functools.lru_cache" in chunk_text(out, c)]
    check("F2a 装饰器只出现在它自己的块里", len(lru) == 1,
          f"实际出现在 {len(lru)} 块：{[c['file'] for c in lru]}")
    if lru:
        body = chunk_text(out, lru[0]).splitlines()
        i = next(k for k, l in enumerate(body) if "@functools.lru_cache" in l)
        nxt = next((l for l in body[i + 1:] if l.strip()), "")
        check("F2b 装饰器紧跟它装饰的函数（没有悬空）",
              nxt.strip() == "def cached_fn(x):", f"下一行是 {nxt.strip()!r}")

    others = [c for c in chunks if c not in lru]
    leaked = [c["file"] for c in others
              if "@functools.lru_cache" in chunk_text(out, c)]
    check("F2c 别的块不再被前导区污染装饰器", not leaked, ", ".join(leaked))

    mb = [c for c in chunks if c["symbol"].startswith("big_b")]
    check("F2d 方法装饰器没被切丢",
          bool(mb) and "@property" in chunk_text(out, mb[0]),
          "big_b 块里应有 @property")


REMARK_SRC = (
    '"""doc"""\n'
    "import json\n"
    "\n"
    "A = 1\n"
    "B = 2\n"
    "\n"
    "def one():\n"
    "    return A\n"
    "\n"
    "def two():\n"
    "    return B\n"
)


def test_remap_edges(tmp):
    """F1/F10/F12：回映必须按 body_start 算，且失败要能看见"""
    print("\n[E] 行号回映边界（F1/F10/F12）")
    import ocr_adapter as A

    out, dm = write_and_chunk(tmp, "remark", REMARK_SRC, 3)
    m = A.load_chunk_manifest(out / "manifest.json")
    c = dm["chunks"][0]
    bs = c["body_start"]

    s, _e, _p, st = A.remap_detail({"path": c["file"], "start_line": bs + 1}, m)
    check("F1a body 第 2 行 → 源 start_line+1",
          (s, st) == (c["start_line"] + 1, "remapped"), f"{s} / {st}")

    s2, _e2, _p2, st2 = A.remap_detail({"path": c["file"], "start_line": 2}, m)
    check("F1b 前导区行号原样返回", (s2, st2) == (2, "preamble"), f"{s2} / {st2}")

    s3, _e3, _p3, st3 = A.remap_detail({"path": c["file"], "start_line": bs - 1}, m)
    check("F1c 分隔空行/类头钳到 body 首行",
          (s3, st3) == (c["start_line"], "header"), f"{s3} / {st3}")

    _s4, _e4, _p4, st4 = A.remap_detail({"path": "other.py", "start_line": 5}, m)
    check("F10 未匹配路径被标记而非静默", st4 == "unmatched", st4)

    loc = A.build_locator_remapped({"path": "other.py", "start_line": 5}, m,
                                   source_name="src/big.py")
    check("F12 --source-name 不覆写未匹配路径", loc.startswith("other.py:"), loc)

    legacy = A.ChunkManifest({c["file"]: (c["start_line"], c["end_line"])})
    _s5, _e5, _p5, st5 = A.remap_detail({"path": c["file"], "start_line": bs + 1}, legacy)
    check("F1d 老清单降级为 legacy（会告警，不假装正确）", st5 == "legacy", st5)

    # 统计要能看见未匹配
    conv = A.convert({"comments": [
        {"path": "other.py", "start_line": 3, "content": "随便一条", "category": "bug"},
        {"path": c["file"], "start_line": bs + 1, "content": "另一条", "category": "bug"},
    ]}, manifest=m)
    rs = conv["stats"]["remap"]
    check("F10b 未匹配路径进了 stats", rs["unmatched_paths"] == ["other.py"],
          str(rs["unmatched_paths"]))
    check("F10c 回映计数正确", rs["remapped"] == 1, str(rs["remapped"]))


def test_chinese():
    """F3/F5：中文 claim 与中文 category"""
    print("\n[F] 中文语境（F3/F5）")
    import ocr_adapter as A
    symbols = A.collect_source_symbols(ROOT / "tests" / "fixture_big.py")

    for claim in ("`json` 未导入", "`json` 未定义", "`json` 缺少导入",
                  "json 没有导入", "`json` 未导入了"):
        fake, why = A.is_chunk_artifact({"content": claim}, symbols)
        check(f"F3 中文假报被过滤：{claim}", fake, why)

    fake2, _ = A.is_chunk_artifact({"content": "`sqlite3` 未导入"}, symbols)
    check("F3b 中文真缺陷不被误杀", not fake2)

    check("F5 安全→security", A.map_dimension("安全") == "security",
          A.map_dimension("安全"))
    check("F5b 逻辑→logic", A.map_dimension("逻辑错误") == "logic",
          A.map_dimension("逻辑错误"))
    check("F5c 文档→completeness", A.map_dimension("文档缺失") == "completeness",
          A.map_dimension("文档缺失"))


def test_dirty_json(tmp):
    """F4：OCR 输出后面跟着日志行"""
    print("\n[G] 脏 JSON（F4）")
    import ocr_adapter as A
    p = tmp / "dirty.json"
    p.write_text('{"session_id": "s1", "comments": []}\nScan complete in 12.3s\n',
                 encoding="utf-8")
    check("F4 尾部日志不再让 JSON 崩", A.load_ocr_json(p).get("session_id") == "s1")

    p2 = tmp / "dirty2.json"
    p2.write_text('[ocr] starting {weird\n{"session_id": "s2", "comments": []}\n',
                  encoding="utf-8")
    check("F4b 日志里带花括号也能找到真 JSON",
          A.load_ocr_json(p2).get("session_id") == "s2")


def test_guards(tmp):
    """F7/F9：输出目录护栏 + 空前导区"""
    print("\n[H] 护栏（F7/F9）")
    src = tmp / "guard_src.py"
    src.write_text("def f():\n    return 1\n", encoding="utf-8")

    out = tmp / "danger"
    out.mkdir()
    (out / "keep.txt").write_text("重要文件", encoding="utf-8")
    r = run([sys.executable, "chunk_big.py", str(src), "--out", str(out), "--no-git"])
    check("F7 拒绝清空不像分段输出的目录",
          r.returncode == 2 and (out / "keep.txt").exists(), f"exit={r.returncode}")
    r2 = run([sys.executable, "chunk_big.py", str(src), "--out", str(out),
              "--no-git", "--force"])
    check("F7b --force 可以覆盖", r2.returncode == 0, r2.stderr[-200:])

    out2, dm = write_and_chunk(tmp, "nopre", "def lone():\n    return 1\n", 100)
    text = chunk_text(out2, dm["chunks"][0])
    check("F9 空前导区不以空行开头", not text.startswith("\n"), repr(text[:12]))


FAKE_ENGINE = '''\
import json
from pathlib import Path


class ReviewBrain:
    def __init__(self, case_dir):
        self.case_dir = Path(case_dir)
        self.case_dir.mkdir(parents=True, exist_ok=True)
        self.log = self.case_dir / "posted.jsonl"

    def post_finding(self, **kw):
        with self.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\\n")
        n = len(self.log.read_text(encoding="utf-8").splitlines())
        return {"payload": {"finding_id": f"F-{n:03d}"}}
'''


def test_post_failclosed(tmp):
    """F6：校验不过时不能半批入库"""
    print("\n[I] --post 的 fail-closed（F6）")
    import contextlib
    import io
    import ocr_adapter as A

    eng = tmp / "engine"
    eng.mkdir()
    (eng / "review_brain_v2.py").write_text(FAKE_ENGINE, encoding="utf-8")
    sys.path.insert(0, str(eng))
    try:
        ocr = tmp / "post.json"
        ocr.write_text(json.dumps({"comments": [
            {"path": "a.py", "start_line": 1, "content": "第一条问题",
             "category": "bug", "severity": "high"},
            {"path": "b.py", "start_line": 2, "content": "第二条问题",
             "category": "bug", "severity": "high"},
        ]}, ensure_ascii=False), encoding="utf-8")

        calls = {"n": 0}
        orig_validate = A.validate

        def fake_validate(f):
            calls["n"] += 1
            return ["模拟校验失败"] if calls["n"] == 1 else []

        A.validate = fake_validate
        try:
            case = tmp / "case_strict"
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = A.main([str(ocr), "--post", str(case), "--seat", "S-OCR1"])
            check("F6 校验不过 → 拒绝入库，账本零写入",
                  rc == 2 and not (case / "posted.jsonl").exists(),
                  f"rc={rc}")

            case2 = tmp / "case_lenient"
            calls["n"] = 0
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc2 = A.main([str(ocr), "--post", str(case2), "--seat", "S-OCR1",
                              "--allow-invalid"])
            n = (case2 / "posted.jsonl")
            posted = len(n.read_text(encoding="utf-8").splitlines()) if n.exists() else 0
            check("F6b --allow-invalid 只丢坏条目、其余照常入库",
                  rc2 == 2 and posted == 1, f"rc={rc2} posted={posted}")
        finally:
            A.validate = orig_validate
    finally:
        sys.path.remove(str(eng))


def test_cli_out(tmp):
    """F11：README 里写了 -o，就得真的有"""
    print("\n[J] -o 输出（F11）")
    ocr = tmp / "mini.json"
    ocr.write_text(json.dumps({"comments": [
        {"path": "a.py", "start_line": 1, "content": "问题", "category": "bug",
         "severity": "low"},
    ]}, ensure_ascii=False), encoding="utf-8")
    dest = tmp / "mapped.json"
    r = run([sys.executable, "ocr_adapter.py", str(ocr), "-o", str(dest)])
    check("F11 -o 能写文件", dest.exists(), r.stderr[-300:])
    check("F11b -o 写的是合法 JSON",
          dest.exists() and "findings" in json.loads(dest.read_text(encoding="utf-8")))


def test_blindtest_regression(tmp):
    """盲测实锤：英文裸函数名的跨块假报曾被放过"""
    print("\n[K] 盲测回归（2026-11-27 缪斯盲测那条真实假报）")
    import ocr_adapter as A

    src = tmp / "blind_src.py"
    src.write_text(
        "import json\n\n\n"
        "def strip_fences(t):\n    return t\n\n\n"
        "def build_prompt(a, b):\n    return str(a) + str(b)\n\n\n"
        "def call(x):\n    return json.dumps(x)\n\n\n"
        "def main():\n    return call(build_prompt(1, 2))\n",
        encoding="utf-8")
    symbols = A.collect_source_symbols(src)

    claim = ("call() is invoked here but is never defined or imported in this file; "
             "the same applies to build_prompt() and strip_fences(). "
             "As written, this file would raise NameError at runtime.")
    fake, why = A.is_chunk_artifact({"content": claim}, symbols)
    check("K1 英文裸函数名的跨块假报被过滤", fake, why)
    idents = A.extract_identifiers(claim)
    check("K2 抽到的是符号而不是连词",
          idents == ["call", "build_prompt", "strip_fences"], str(idents))

    claim2 = "run_job() is invoked here but is never defined or imported in this file."
    fake2, _ = A.is_chunk_artifact({"content": claim2}, symbols)
    check("K3 真缺陷不被误杀", not fake2)

    # K4/K5：JS 端到端实测抓到的那条（2026-11-27，昆仑 learn/index.js）
    # 同一句里混着"未定义"和别的名字，只要有一个名字在文件里找不到就保留
    js_claim = ("`buildReviews(answers)` is called but not defined in this file/chunk, "
                "and it relies on `answers` rather than the sanitized `ids`.")
    js_syms = {"buildReviews", "answers", "ids", "Number"}
    fake3, why3 = A.is_chunk_artifact({"content": js_claim}, js_syms)
    check("K4 带参数的裸函数名 + 'but not defined' 也能识别", fake3, why3)

    fake4, _ = A.is_chunk_artifact({"content": js_claim}, {"answers", "ids"})
    check("K5 缺一个名字就保留（不误杀）", not fake4)


def test_manifest_v2(tmp):
    """L：分段契约 v2（language / chunker / 未知语言拒绝回映）"""
    print("\n[L] 分段契约 v2")
    import ocr_adapter as A

    out, dm = write_and_chunk(tmp, "v2", REMARK_SRC, 3)
    check("L1 manifest 带 schema", dm.get("schema") == "chunk-manifest/2",
          str(dm.get("schema")))
    check("L2 manifest 带 language", dm.get("language") == "python",
          str(dm.get("language")))
    ch = dm.get("chunker") or {}
    check("L3 manifest 带 chunker 名与版本",
          bool(ch.get("name")) and bool(ch.get("version")), str(ch))

    m = A.load_chunk_manifest(out / "manifest.json")
    check("L4 适配器读到 language / chunker",
          m.language == "python" and m.chunker.get("name") == "chunk_big",
          f"{m.language} / {m.chunker}")

    c = dm["chunks"][0]
    conv = A.convert({"comments": [
        {"path": c["file"], "start_line": c["body_start"] + 1,
         "content": "一条断言", "category": "bug"}]}, manifest=m)
    rs = conv["stats"]["remap"]
    check("L5 stats 记录语言", rs["language"] == "python", str(rs["language"]))

    # 已支持的语言共用同一套 body_start 语义，都能正常回映
    for lang in ("javascript", "typescript", "vue", "go", "rust"):
        ml = A.load_chunk_manifest(out / "manifest.json")
        ml.language = lang
        _s, _e, _p, stl = A.remap_detail(
            {"path": c["file"], "start_line": c["body_start"] + 1}, ml)
        check(f"L8 {lang} 允许回映", stl == "remapped", stl)

    bad = A.load_chunk_manifest(out / "manifest.json")
    bad.language = "cobol"    # 真正不认识的（rust 已支持，别再拿它当反例）
    _s, _e, _p, st = A.remap_detail(
        {"path": c["file"], "start_line": c["body_start"] + 1}, bad)
    check("L6 未知语言拒绝回映", st == "unsupported-language", st)
    loc = A.build_locator_remapped(
        {"path": c["file"], "start_line": c["body_start"] + 1},
        bad, source_name="src/x.cob")
    check("L7 拒绝回映时不改写文件名", not loc.startswith("src/x.cob"), loc)


def test_symbol_dispatch(tmp):
    """M：符号表按语言分派"""
    print("\n[M] 符号表分派")
    import ocr_adapter as A

    check("M1 .py 走内置 ast",
          len(A.collect_source_symbols(ROOT / "tests" / "fixture_big.py")) > 20)

    js = tmp / "x.js"
    js.write_text("function f(){ return Number('1') }\n", encoding="utf-8")

    # 缺 js_symbols 时必须明确报错，不能空转。这里用 sys.modules 置 None 模拟缺失
    # （置 None 会让 import 抛 ImportError），比依赖"文件不存在"更可控。
    saved = sys.modules.get("js_symbols")
    sys.modules["js_symbols"] = None
    msg, ok = "", False
    try:
        A.collect_source_symbols(js)
    except SystemExit as e:
        msg = str(e)
        ok = "js_symbols.py" in msg
    finally:
        if saved is None:
            sys.modules.pop("js_symbols", None)
        else:
            sys.modules["js_symbols"] = saved
    check("M2 .js 缺 js_symbols 时明确报错而不是空转", ok, msg[:60])

    # 装得上就走 JS 符号表，且内置全局算"已定义"
    try:
        import js_symbols  # noqa: F401
        have_js = True
    except ImportError:
        have_js = False
    if have_js:
        syms = A.collect_source_symbols(js)
        check("M4 .js 走 tree-sitter 抽符号", "f" in syms, str(sorted(syms)[:5]))
        check("M5 JS 内置全局算已定义", "Number" in syms and "JSON" in syms)
    else:
        print("  SKIP M4/M5（未安装 tree-sitter）")

    txt = tmp / "x.txt"
    txt.write_text("hi\n", encoding="utf-8")
    ok2 = False
    try:
        A.collect_source_symbols(txt)
    except SystemExit:
        ok2 = True
    check("M3 不支持的后缀明确报错", ok2)

    # Go / Rust 分派（各自独立模块，缺了就明确报错，不空转）
    go = tmp / "x.go"
    go.write_text("package p\n\nfunc Load(a int) int { return a }\n", encoding="utf-8")
    try:
        gsyms = A.collect_source_symbols(go)
        check("M6 .go 走 go_symbols", "Load" in gsyms, str(sorted(gsyms)[:6]))
    except SystemExit as e:
        check("M6 .go 走 go_symbols", False, str(e)[:60])

    rs = tmp / "x.rs"
    rs.write_text("fn make(n: usize) -> usize { n }\n", encoding="utf-8")
    try:
        rsyms = A.collect_source_symbols(rs)
        check("M7 .rs 走 rust_symbols", "make" in rsyms, str(sorted(rsyms)[:6]))
    except SystemExit as e:
        check("M7 .rs 走 rust_symbols", False, str(e)[:60])

    # Go/Rust 的内置名表：缺了它，提到 len/Option 的跨块假报永远过滤不掉
    # （与 js_symbols.JS_GLOBALS 同一个道理，由缪斯在收工报告里点名）
    go2 = tmp / "builtin.go"
    go2.write_text("package p\n\nfunc F(a []int) int { return len(a) }\n",
                   encoding="utf-8")
    try:
        gs = A.collect_source_symbols(go2)
        check("M8 Go 内置名算已定义（len/string/nil）",
              {"len", "string", "nil"} <= gs, str(sorted(gs)[:8]))
        fake_go, _ = A.is_chunk_artifact(
            {"content": "`len` is not defined in this chunk"}, gs)
        check("M8b 提到内置名的跨块假报能被过滤", fake_go)
    except SystemExit as e:
        check("M8 Go 内置名算已定义（len/string/nil）", False, str(e)[:60])

    rs2 = tmp / "prelude.rs"
    rs2.write_text("pub fn f(v: Option<u8>) -> Vec<u8> { Vec::new() }\n",
                   encoding="utf-8")
    try:
        rr = A.collect_source_symbols(rs2)
        check("M9 Rust prelude 算已定义（Option/Vec/String）",
              {"Option", "Vec", "String"} <= rr, str(sorted(rr)[:8]))
    except SystemExit as e:
        check("M9 Rust prelude 算已定义（Option/Vec/String）", False, str(e)[:60])


def test_js_trailing_comment(tmp):
    """N：行尾注释不能被当成下一个单元的前导注释（2026-11-27 修）"""
    print("\n[N] JS 行尾注释")
    import json as _json
    import subprocess

    try:
        import tree_sitter_javascript  # noqa: F401
    except ImportError:
        print("  SKIP N（未安装 tree-sitter）")
        return

    src = tmp / "tail_comment.js"
    src.write_text(
        "const MAX = 3;  // 行尾注释紧贴下一个函数\n"
        "function alpha(a) {\n"
        "  return a + MAX;\n"
        "}\n"
        "\n"
        "function beta() {\n"
        "  return alpha(1);\n"
        "}\n", encoding="utf-8")
    out = tmp / "tail_chunks"
    r = subprocess.run([sys.executable, "chunk_js.py", str(src),
                        "--out", str(out), "--max-lines", "40", "--no-git",
                        "--force"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    check("N1 切分成功", r.returncode == 0, r.stderr[-200:])
    if r.returncode != 0:
        return
    man = _json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    check("N2 行尾注释没把上一条语句吞进 body（preamble 应为 1）",
          man["preamble_lines"] == 1, str(man["preamble_lines"]))
    check("N3 首块从函数行开始", man["chunks"][0]["start_line"] == 2,
          str(man["chunks"][0]["start_line"]))
    text = (out / man["chunks"][0]["file"]).read_text(encoding="utf-8")
    check("N4 前导区里还留着那条 const",
          "const MAX = 3" in text.split("function alpha")[0])


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        chunk_dir, chunks = test_chunker(tmp)
        test_adapter(tmp, chunk_dir, chunks)
        test_end_to_end(tmp, chunk_dir)
        test_decorators(tmp)
        test_remap_edges(tmp)
        test_chinese()
        test_dirty_json(tmp)
        test_guards(tmp)
        test_post_failclosed(tmp)
        test_cli_out(tmp)
        test_blindtest_regression(tmp)
        test_manifest_v2(tmp)
        test_symbol_dispatch(tmp)
        test_js_trailing_comment(tmp)

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
