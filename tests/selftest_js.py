#!/usr/bin/env python3
"""selftest_js — chunk_js.py 的自测（只用自造 JS，不依赖任何外部仓库）

跑法：python selftest_js.py [chunk_js.py 的路径]
断言口径：全部对**源文件真值**断言，不拿公式断言公式——
  - body 逐字相等：块文件 body 区间的每一行 == 源文件对应行
  - 回映落点：用唯一标记字符串在源文件里搜出真实行号，再看块文件里
    该标记的行号经 body_start 回映后是否落回这个真实行号
退出码 0 全过，1 有失败。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHUNK_JS = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "chunk_js.py"

FAILS: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASSES.append(name)
        print(f"  OK   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} — {detail}")


# ── 自造样本：行内容即真值，行号靠运行时搜索，不靠手数 ──────────────────────
FIXTURE_LINES = [
    "// 云函数风格样本（自造，无真实业务）",
    "import { createRequire } from 'module';",
    "const cloud = require('wx-server-sdk');",
    "const CONF = { env: 'test', retries: 3 };",
    "cloud.init({ env: CONF.env });",
    "",
    "/**",
    " * 登录校验。JSDoc 应随函数进块，不该留在前导区。",
    " */",
    "async function login(event, context) {",
    "  const openid = event.userInfo && event.userInfo.openId;",
    "  if (!openid) return { code: 401, marker: 'MARK_LOGIN' };",
    "  return { code: 0, openid };",
    "}",
    "",
    "class Store {",
    "  #cache = new Map();",
    "  constructor(db) { this.db = db; }",
    "  get size() { return this.#cache.size; }",
    "  async load(id) {",
    "    if (this.#cache.has(id)) return this.#cache.get(id);",
    "    const doc = await this.db.collection('t').doc(id).get();",
    "    this.#cache.set(id, doc.data);",
    "    return doc.data; // MARK_LOAD",
    "  }",
    "  async save(id, data) {",
    "    await this.db.collection('t').doc(id).set({ data });",
    "    this.#cache.set(id, data);",
    "    return true; // MARK_SAVE",
    "  }",
    "  clear() { this.#cache.clear(); }",
    "}",
    "",
    "const helper = (a, { b }) => a + b;",
    "",
    "exports.main = async (event, context) => {",
    "  const store = new Store(cloud.database());",
    "  return login(event, context); // MARK_MAIN",
    "};",
    "",
    "module.exports = { login, Store };",
]
FIXTURE = "\n".join(FIXTURE_LINES) + "\n"


def truth_line(marker: str) -> int:
    """标记在源文件里的真实行号（1 基）——真值来自搜索，不来自被测代码"""
    for i, ln in enumerate(FIXTURE_LINES, 1):
        if marker in ln:
            return i
    raise AssertionError(f"marker 不在样本里: {marker}")


def main() -> int:
    sys.path.insert(0, str(HERE))
    # 集成适配：本仓 chunk_*.py 在仓根，补一条路径（逻辑未改）
    sys.path.insert(0, str(CHUNK_JS.parent))
    import chunk_js  # noqa: E402  （同目录导入被测模块做重解析自检）

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "index.js"
        src.write_text(FIXTURE, encoding="utf-8")
        out = td / ".chunks"
        r = subprocess.run(
            [sys.executable, str(CHUNK_JS), str(src), "--out", str(out),
             "--max-lines", "12", "--no-git"],
            capture_output=True, text=True)
        check("S1 退出码 0", r.returncode == 0,
              f"rc={r.returncode} stderr={r.stderr.strip()[:200]}")
        if r.returncode != 0:
            return 1

        man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

        # ── 契约层 ──────────────────────────────────────────────────────
        check("S2 schema 字段", man.get("schema") == "chunk-manifest/2",
              str(man.get("schema")))
        check("S3 language 字段", man.get("language") == "javascript")
        check("S4 chunker 字段",
              man.get("chunker") == {"name": "chunk_js", "version": "0.1.0"},
              str(man.get("chunker")))
        check("S5 source_lines 对真值",
              man.get("source_lines") == len(FIXTURE_LINES))
        check("S6 failed=0", man.get("failed") == 0 and not man.get("failed_detail"),
              str(man.get("failed_detail")))
        # 前导区 = 第一个可切单元（login，含它的 JSDoc）之前：1..6 行
        jsdoc_start = truth_line("登录校验")
        check("S7 preamble_lines 对真值",
              man.get("preamble_lines") == jsdoc_start - 2,
              f"manifest={man.get('preamble_lines')} 真值={jsdoc_start - 2}")

        chunks = man["chunks"]
        check("S8 切出多块（小 max-lines 应拆类）", len(chunks) >= 4,
              f"实际 {len(chunks)} 块")
        store_chunks = [c for c in chunks if c.get("class") == "Store"]
        check("S9 大类被拆成方法块", len(store_chunks) >= 2,
              f"Store 块 {len(store_chunks)} 个")

        # ── 逐块真值层 ──────────────────────────────────────────────────
        for c in chunks:
            text = (out / c["file"]).read_text(encoding="utf-8")
            clines = text.splitlines()
            bs, s, e = c["body_start"], c["start_line"], c["end_line"]
            body = clines[bs - 1: bs - 1 + c["n_lines"]]
            check(f"S10 body 逐字相等 {c['file']}",
                  body == FIXTURE_LINES[s - 1:e],
                  f"块 body 首行 {body[:1]} vs 源 {FIXTURE_LINES[s - 1:s]}")
            check(f"S11 块可重解析 {c['file']}",
                  not chunk_js.has_error(chunk_js.parse_js(text).root_node))
            check(f"S12 前导区逐字打头 {c['file']}",
                  clines[:man["preamble_lines"]] ==
                  FIXTURE_LINES[:man["preamble_lines"]])
            if c.get("class"):
                check(f"S13 类头+收尾 {c['file']}",
                      clines[bs - 2] == f"class {c['class']} {{" and
                      clines[bs - 1 + c["n_lines"]] == "}",
                      f"头 {clines[bs - 2]!r} 尾 "
                      f"{clines[bs - 1 + c['n_lines']]!r}")

        # ── 回映落点（对源文件真值） ────────────────────────────────────
        for marker in ("MARK_LOGIN", "MARK_LOAD", "MARK_SAVE", "MARK_MAIN"):
            tline = truth_line(marker)
            owner = next(
                (c for c in chunks
                 if c["start_line"] <= tline <= c["end_line"]), None)
            check(f"S14 {marker} 有归属块", owner is not None)
            if owner is None:
                continue
            clines = (out / owner["file"]).read_text(
                encoding="utf-8").splitlines()
            chunk_line = next(
                i for i, ln in enumerate(clines, 1) if marker in ln)
            remapped = chunk_line - owner["body_start"] + owner["start_line"]
            check(f"S15 {marker} 回映偏移 0",
                  remapped == tline,
                  f"回映 {remapped} vs 真值 {tline}")

        # ── handler 不丢 ────────────────────────────────────────────────
        main_line = truth_line("exports.main")
        check("S16 exports.main 落在某块内",
              any(c["start_line"] <= main_line <= c["end_line"]
                  for c in chunks))

    # ── js_symbols：契约必收项逐类验（期望集是人工真值，写死在测试里） ──────
    import js_symbols  # noqa: E402

    SYM_FIXTURE = "\n".join([
        "import defaultThing, { named1, named2 as alias2 } from 'mod-a';",
        "import * as ns from 'mod-b';",
        "const fs = require('fs');",
        "const { readFile, writeFile: wf } = require('fs');",
        "const [first, , third = 3, ...restArr] = arr;",
        "let { a, b: { c }, d = 4, ...restObj } = obj;",
        "var plain = 1;",
        "function fn(p1, { dp }, [ap], pDef = 1, ...pRest) {",
        "  try { mayThrow(); } catch (err) { log(err); }",
        "  return p1;",
        "}",
        "const arrow = (q) => q;",
        "const inc = x => x + 1;",
        "class Klass { methodA(mParam) {} get prop() {} }",
        "export const exportedConst = 1;",
        "export { plain as plainOut, fn };",
        "export default function defaultFn() {}",
    ]) + "\n"
    EXPECTED = {
        # import 绑定（默认/命名空间/命名+别名）
        "defaultThing", "named1", "named2", "alias2", "ns",
        # require 变量（含解构、别名）
        "fs", "readFile", "writeFile", "wf",
        # const/let/var（含数组解构、默认值、剩余、嵌套对象解构）
        "first", "third", "restArr", "a", "c", "d", "restObj", "plain",
        # 函数声明 + 参数（含解构/默认/剩余参数）
        "fn", "p1", "dp", "ap", "pDef", "pRest",
        # catch 参数
        "err",
        # 箭头函数与参数（含无括号单参数）
        "arrow", "q", "inc", "x",
        # 类与方法（含 getter）
        "Klass", "methodA", "mParam", "prop",
        # export 的名字（声明式 + 列表式原名与别名）
        "exportedConst", "plainOut", "defaultFn",
    }
    with tempfile.TemporaryDirectory() as td2:
        sp = Path(td2) / "syms.js"
        sp.write_text(SYM_FIXTURE, encoding="utf-8")
        got = js_symbols.collect_js_symbols(sp)
    check("S17 符号表必收项全收", EXPECTED <= got,
          f"缺 {sorted(EXPECTED - got)}")
    check("S18 未定义名不误收",
          "mayThrow" not in got and "neverDefinedAnywhere" not in got)

    # 盲测同型回归：类方法名必须在符号表里（上一轮 call() 漏网的 JS 对应面）
    with tempfile.TemporaryDirectory() as td3:
        fp = Path(td3) / "index.js"
        fp.write_text(FIXTURE, encoding="utf-8")
        fgot = js_symbols.collect_js_symbols(fp)
    check("S19 样本符号含类方法名",
          {"login", "Store", "load", "save", "clear", "size", "helper"} <= fgot,
          f"实得 {sorted(fgot)}")

    # 退化兜底：只有 import+const 的文件不能切出 0 块静默逃审
    with tempfile.TemporaryDirectory() as td3b:
        dp = Path(td3b) / "plain.js"
        dp.write_text("import { a } from 'm';\nconst ONLY = 1;\n"
                      "console.log(ONLY, a);\n", encoding="utf-8")
        dout = Path(td3b) / ".chunks"
        r = subprocess.run(
            [sys.executable, str(CHUNK_JS), str(dp), "--out", str(dout),
             "--no-git"], capture_output=True, text=True)
        ok = r.returncode == 0 and len(json.loads(
            (dout / "manifest.json").read_text("utf-8"))["chunks"]) >= 1
        check("S20 无单元文件退化成块（不逃审）", ok,
              f"rc={r.returncode} stderr={r.stderr.strip()[:160]}")

    # ── TS 节：chunk_ts 切分 + TS 符号（期望对源文件真值） ─────────────────
    import chunk_ts  # noqa: E402

    TS_LINES = [
        "import { A } from 'm';",
        "export interface User { id: number; load(id: number): Promise<void>; }",
        "export type ID = string | number;",
        "export enum Color { Red, Green = 2 }",
        "const CONF: number = 1;",
        "export abstract class Base<T> {",
        "  private count: number = 0;",
        "  protected abstract load(id: ID): Promise<User>;",
        "  constructor(private db: object, readonly name: string) {}",
        "  async fetch(id: ID): Promise<T | null> {",
        "    const x = 1; // MARK_TS_FETCH",
        "    return null;",
        "  }",
        "  clear() { this.count = 0; }",
        "}",
        "export function make<T extends object>(x: T): T { return x; }",
    ]
    TS_FIXTURE = "\n".join(TS_LINES) + "\n"

    def ts_truth(marker: str) -> int:
        for i, ln in enumerate(TS_LINES, 1):
            if marker in ln:
                return i
        raise AssertionError(marker)

    with tempfile.TemporaryDirectory() as td4:
        td4 = Path(td4)
        tsrc = td4 / "smoke.ts"
        tsrc.write_text(TS_FIXTURE, encoding="utf-8")
        tout = td4 / ".chunks"
        r = subprocess.run(
            [sys.executable, str(CHUNK_JS.parent / "chunk_ts.py"), str(tsrc),
             "--out", str(tout), "--max-lines", "8", "--no-git"],
            capture_output=True, text=True)
        check("T1 chunk_ts 退出码 0", r.returncode == 0,
              f"rc={r.returncode} stderr={r.stderr.strip()[:200]}")
        if r.returncode == 0:
            tman = json.loads((tout / "manifest.json").read_text("utf-8"))
            check("T2 language=typescript", tman.get("language") == "typescript")
            check("T3 chunker=chunk_ts",
                  tman.get("chunker", {}).get("name") == "chunk_ts")
            check("T4 failed=0", tman.get("failed") == 0)
            for c in tman["chunks"]:
                text = (tout / c["file"]).read_text(encoding="utf-8")
                cl = text.splitlines()
                check(f"T5 body 逐字相等 {c['file']}",
                      cl[c["body_start"] - 1:
                         c["body_start"] - 1 + c["n_lines"]]
                      == TS_LINES[c["start_line"] - 1: c["end_line"]])
                check(f"T6 块可重解析 {c['file']}",
                      not chunk_ts.has_error(
                          chunk_ts.parse_ts(
                              text, chunk_ts.lang_for_suffix(".ts")
                          ).root_node))
            tline = ts_truth("MARK_TS_FETCH")
            owner = next((c for c in tman["chunks"]
                          if c["start_line"] <= tline <= c["end_line"]), None)
            check("T7 MARK_TS_FETCH 有归属块", owner is not None)
            if owner is not None:
                cl = (tout / owner["file"]).read_text(
                    encoding="utf-8").splitlines()
                cline = next(i for i, ln in enumerate(cl, 1)
                             if "MARK_TS_FETCH" in ln)
                check("T8 MARK_TS_FETCH 回映偏移 0",
                      cline - owner["body_start"] + owner["start_line"] == tline)
            iface_line = ts_truth("interface User")
            check("T9 interface 落在某块内",
                  any(c["start_line"] <= iface_line <= c["end_line"]
                      for c in tman["chunks"]))
        tsyms = js_symbols.collect_js_symbols(tsrc)
    check("T10 TS 符号必收项",
          {"A", "User", "ID", "Color", "Red", "Green", "Base", "count",
           "load", "db", "name", "fetch", "clear", "make", "CONF"} <= tsyms,
          f"缺 {sorted({'A', 'User', 'ID', 'Color', 'Red', 'Green', 'Base', 'count', 'load', 'db', 'name', 'fetch', 'clear', 'make', 'CONF'} - tsyms)}")

    # TSX：JSX 必须走 tsx 语法（typescript 语法下是 ERROR）
    with tempfile.TemporaryDirectory() as td5:
        td5 = Path(td5)
        xsrc = td5 / "app.tsx"
        xsrc.write_text(
            "import React from 'react';\n"
            "export const App = () => {\n"
            "  const el = <div className=\"x\">{1}</div>;\n"
            "  return el;\n"
            "};\n", encoding="utf-8")
        xout = td5 / ".chunks"
        r = subprocess.run(
            [sys.executable, str(CHUNK_JS.parent / "chunk_ts.py"), str(xsrc),
             "--out", str(xout), "--no-git"],
            capture_output=True, text=True)
        ok = r.returncode == 0 and json.loads(
            (xout / "manifest.json").read_text("utf-8"))["failed"] == 0
        check("T11 tsx 含 JSX 可切且全块可解析", ok,
              f"rc={r.returncode} stderr={r.stderr.strip()[:160]}")

    # ── Vue 节：chunk_vue 的 .vue 坐标（真值 = .vue 文件本身） ─────────────
    VUE_LINES = [
        "<template>",
        "  <view><text>{{ title }}</text></view>",
        "</template>",
        "<script>",
        "import { ref } from 'vue';",
        "const CONF = { pageSize: 20 }; // MARK_VUE_PRE",
        "export default {",
        "  methods: {",
        "    async load(id) {",
        "      return await db.get(id); // MARK_VUE_LOAD",
        "    }",
        "  }",
        "};",
        "function helper(a) { return a + 1; }",
        "</script>",
        "<script setup>",
        "const extra = 1;",
        "</script>",
    ]
    VUE_FIXTURE = "\n".join(VUE_LINES) + "\n"

    def vue_truth(marker: str) -> int:
        for i, ln in enumerate(VUE_LINES, 1):
            if marker in ln:
                return i
        raise AssertionError(marker)

    with tempfile.TemporaryDirectory() as td6:
        td6 = Path(td6)
        vsrc = td6 / "page.vue"
        vsrc.write_text(VUE_FIXTURE, encoding="utf-8")
        vout = td6 / ".chunks"
        r = subprocess.run(
            [sys.executable, str(CHUNK_JS.parent / "chunk_vue.py"), str(vsrc),
             "--out", str(vout), "--no-git"],
            capture_output=True, text=True)
        check("V1 chunk_vue 退出码 0", r.returncode == 0,
              f"rc={r.returncode} stderr={r.stderr.strip()[:200]}")
        if r.returncode == 0:
            vman = json.loads((vout / "manifest.json").read_text("utf-8"))
            check("V2 language=vue", vman.get("language") == "vue")
            check("V3 第二个 script 块有显式 warnings",
                  bool(vman.get("warnings")) and
                  "script setup" in vman["warnings"][0],
                  str(vman.get("warnings")))
            for c in vman["chunks"]:
                cl = (vout / c["file"]).read_text(
                    encoding="utf-8").splitlines()
                check(f"V4 body 是 .vue 逐字行 {c['file']}",
                      cl[c["body_start"] - 1:
                         c["body_start"] - 1 + c["n_lines"]]
                      == VUE_LINES[c["start_line"] - 1: c["end_line"]])
            vline = vue_truth("MARK_VUE_LOAD")
            owner = next((c for c in vman["chunks"]
                          if c["start_line"] <= vline <= c["end_line"]), None)
            check("V5 MARK_VUE_LOAD 有归属块", owner is not None)
            if owner is not None:
                cl = (vout / owner["file"]).read_text(
                    encoding="utf-8").splitlines()
                cline = next(i for i, ln in enumerate(cl, 1)
                             if "MARK_VUE_LOAD" in ln)
                check("V6 MARK_VUE_LOAD 回映 .vue 偏移 0",
                      cline - owner["body_start"] + owner["start_line"] == vline,
                      f"回映 {cline - owner['body_start'] + owner['start_line']}"
                      f" vs 真值 {vline}")
                # 前导区 1:1 对齐：块文件第 vue 真值行号行就是那行前导代码
                pre_line = vue_truth("MARK_VUE_PRE")
                check("V7 前导区与 .vue 行号 1:1 对齐",
                      "MARK_VUE_PRE" in cl[pre_line - 1],
                      f"块第 {pre_line} 行是 {cl[pre_line - 1][:40]!r}")
        vsyms = js_symbols.collect_js_symbols(vsrc)
    check("V8 .vue 符号抽 script 内容",
          {"CONF", "load", "helper", "ref"} <= vsyms,
          f"实得交集 {sorted({'CONF', 'load', 'helper', 'ref'} & vsyms)}")

    # 退化兜底（小凛在昆仑 App.vue 上逼出来的）：script 只有 import+const
    # 也必须切出块，不能 0 块静默逃审
    with tempfile.TemporaryDirectory() as td7:
        pp = Path(td7) / "plain.vue"
        pp.write_text(
            "<template><view /></template>\n"
            "<script setup>\n"
            "import { ref } from 'vue';\n"
            "const title = ref('x');\n"
            "</script>\n", encoding="utf-8")
        pout = Path(td7) / ".chunks"
        r = subprocess.run(
            [sys.executable, str(CHUNK_JS.parent / "chunk_vue.py"), str(pp),
             "--out", str(pout), "--no-git"],
            capture_output=True, text=True)
        ok = r.returncode == 0 and len(json.loads(
            (pout / "manifest.json").read_text("utf-8"))["chunks"]) >= 1
        check("V9 只有 import+const 的 .vue 也成块", ok,
              f"rc={r.returncode} stderr={r.stderr.strip()[:160]}")

    print(f"\n通过 {len(PASSES)} · 失败 {len(FAILS)}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
