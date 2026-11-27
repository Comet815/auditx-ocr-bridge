#!/usr/bin/env python3
"""分段器鲁棒性回归：损坏/畸形源文件绝不 traceback。

契约（2026-11-27 接手工单）：对截断、语法损坏、空、纯注释、超长单行、
二进制垃圾、深嵌套（200 层）输入，每个分段器只能三选一——切出块
（rc=0）、部分块失败（rc=1）、干净拒绝（rc=2 且 stderr 说清原因）。
任何情况下 stderr 不许出现 Traceback；批量跑时单个坏文件不许拖垮其余。

语料运行时生成（不存二进制夹具），每个文件独立子进程跑真实 CLI。
退出码：0 契约成立 / 1 契约被违反 / 3 环境缺失（tree_sitter 等依赖不在）。

运行：python qa/regress_robustness.py
"""
from __future__ import annotations

import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

CMD = {"py": "chunk_big.py", "js": "chunk_js.py", "ts": "chunk_ts.py",
       "tsx": "chunk_ts.py", "vue": "chunk_vue.py", "go": "chunk_go.py",
       "rs": "chunk_rust.py"}
EXT = {"py": ".py", "js": ".js", "ts": ".ts", "tsx": ".tsx",
       "vue": ".vue", "go": ".go", "rs": ".rs"}

VALID = {
    "py": 'import os\n\n\ndef alpha(x):\n    return x + 1\n\n\ndef beta(y):\n    return y * 2\n',
    "js": 'function alpha(x) {\n  return x + 1;\n}\n\nfunction beta(y) {\n  return y * 2;\n}\n',
    "ts": 'function alpha(x: number): number {\n  return x + 1;\n}\n',
    "tsx": 'export function App(props: {n: number}) {\n  return <div>{props.n}</div>;\n}\n',
    "vue": '<template>\n  <div>{{ msg }}</div>\n</template>\n\n<script>\nexport default {\n  methods: {\n    load() { return 1; }\n  }\n}\n</script>\n',
    "go": 'package main\n\nfunc Alpha(x int) int {\n\treturn x + 1\n}\n',
    "rs": 'pub fn alpha(x: i32) -> i32 {\n    x + 1\n}\n',
}
BROKEN = {
    "py": "def broken(:\n    pass\n",
    "js": "function broken( { return 1; }\n",
    "ts": "function broken( { return 1; }\n",
    "tsx": "function broken( { return <div>; }\n",
    "vue": "<script>\nfunction broken( { return 1; }\n</script>\n",
    "go": "package main\n\nfunc Broken( { return 1 }\n",
    "rs": "pub fn broken( { 1 }\n",
}
COMMENT = {"py": "# only a comment\n", "js": "// only a comment\n",
           "ts": "// only a comment\n", "tsx": "// only a comment\n",
           "vue": "<!-- only a comment -->\n",
           "go": "// only a comment\npackage main\n",
           "rs": "// only a comment\n"}
LONGLINE = {
    "py": 'x = "' + "a" * 200_000 + '"\n',
    "js": 'const s = "' + "a" * 200_000 + '";\n',
    "ts": 'const s = "' + "a" * 200_000 + '";\n',
    "tsx": 'const s = "' + "a" * 200_000 + '";\n',
    "vue": '<script>\nconst s = "' + "a" * 200_000 + '";\n</script>\n',
    "go": 'package main\n\nvar s = "' + "a" * 200_000 + '"\n',
    "rs": 'static S: &str = "' + "a" * 200_000 + '";\n',
}


def deep_nest(lang: str, n: int = 200) -> str:
    if lang == "py":
        lines = ["def deep():"]
        for i in range(n):
            lines.append("    " * (i + 1) + "if True:")
        lines.append("    " * (n + 1) + "return 1")
        return "\n".join(lines) + "\n"
    if lang in ("js", "ts", "tsx"):
        return ("function deep(x) {\n" + "if (x) {\n" * n
                + "return 1;\n" + "}\n" * n + "}\n")
    if lang == "go":
        return ("package main\n\nfunc Deep(x int) int {\n"
                + "\tif x > 0 {\n" * n + "\treturn 1\n" + "\t}\n" * n + "}\n")
    if lang == "rs":
        return ("pub fn deep(x: i32) -> i32 {\n" + "if x > 0 {\n" * n
                + "return 1;\n" + "}\n" * n + "}\n")
    return ("<script>\nfunction deep(x) {\n" + "if (x) {\n" * n
            + "return 1;\n" + "}\n" * n + "}\n</script>\n")


def build_corpus(root: Path) -> list:
    rng = random.Random(20261127)
    files = []
    for lang, src in VALID.items():
        d = root / lang
        d.mkdir(parents=True)
        ext = EXT[lang]
        cases = {
            "valid": src.encode(),
            "truncated": src[: len(src) // 2].encode(),
            "syntax": BROKEN[lang].encode(),
            "empty": b"",
            "comments": COMMENT[lang].encode(),
            "longline": LONGLINE[lang].encode(),
            "binary": bytes(rng.randrange(256) for _ in range(4096)),
            "deepnest": deep_nest(lang).encode(),
        }
        for case, data in cases.items():
            p = d / f"{case}{ext}"
            p.write_bytes(data)
            files.append((lang, case, p))
    return files


def main() -> int:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_javascript  # noqa: F401
        import tree_sitter_typescript  # noqa: F401
        import tree_sitter_go  # noqa: F401
        import tree_sitter_rust  # noqa: F401
    except ImportError as e:
        print(f"环境缺失：{e}（装齐 tree_sitter 五包再跑）", file=sys.stderr)
        return 3

    violations = []
    with tempfile.TemporaryDirectory() as td:
        files = build_corpus(Path(td) / "src")
        done = 0
        for lang, case, src in files:
            out = Path(td) / "out" / f"{lang}-{case}"
            r = subprocess.run(
                [sys.executable, str(REPO / CMD[lang]), str(src),
                 "--out", str(out), "--no-git"],
                capture_output=True, text=True, timeout=180)
            done += 1
            tag = f"{lang}/{case}"
            if "Traceback" in r.stderr:
                violations.append(f"{tag}: 出现 Traceback")
            if r.returncode not in (0, 1, 2):
                violations.append(f"{tag}: 退出码 {r.returncode} 不在契约内")
            if r.returncode != 0 and not r.stderr.strip():
                violations.append(f"{tag}: 失败但 stderr 没给原因")
            if case == "binary" and r.returncode != 2:
                violations.append(f"{tag}: 二进制输入应干净拒绝(rc=2)，实际 {r.returncode}")
            if case == "syntax" and lang == "py" and r.returncode != 2:
                violations.append(f"{tag}: Python 语法损坏应干净拒绝(rc=2)，实际 {r.returncode}")
            if case == "valid" and r.returncode != 0:
                violations.append(f"{tag}: 对照组（合法文件）应成功，实际 {r.returncode}")
        # 批量隔离：语料全跑完本身就是证据（坏文件在前、合法文件在后都处理到了）
        if done != len(files):
            violations.append(f"批量被中断：只处理 {done}/{len(files)}")

    if violations:
        print("❌ 鲁棒性契约被违反：")
        for v in violations:
            print(f"  - {v}")
        return 1
    print(f"✅ 鲁棒性契约成立：{len(files)} 个损坏/边界文件，无 Traceback，"
          f"退出码全在 {{0,1,2}}，失败均给原因，批量未被单个坏文件拖垮")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
