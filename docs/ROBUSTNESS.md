# 分段器鲁棒性：损坏/畸形源文件的实测记录

日期：2026-11-27（接手工单）。基线 main 57f7e53。
可复跑脚本：`qa/regress_robustness.py`（语料运行时生成，56 个文件 =
7 个语言入口 × 8 类输入；断言：无 Traceback、退出码 ∈ {0,1,2}、
失败必给原因、批量不被单个坏文件中断）。

## 契约

坏文件只能三选一：切出块（rc=0）、部分块失败（rc=1）、干净拒绝
（rc=2 且 stderr 说清原因）。**绝不 traceback。**

## 修复前实测（先复现，逐项对源文件与 traceback 定性）

| 输入 | 结果 | 定性 |
|---|---|---|
| 二进制垃圾（随机字节）× 7 入口 | **全部 Traceback**（UnicodeDecodeError，rc=1） | 真 bug：读文件没兜解码失败 |
| 200 层嵌套 · Go / Rust | **Traceback**（RecursionError，死在 has_error 递归） | 真 bug：同一递归实现在 js/ts 里也有，只是阈值没撞到 |
| 200 层嵌套 · Python | **Traceback**（IndentationError） | CPython 自身缩进上限 100 层；崩点在 preamble_end 的裸 ast.parse |
| 语法损坏 · Python | **Traceback**（SyntaxError） | 真 bug：chunk_big 全程没有 SyntaxError 处理，其他语言是干净 rc=2 |
| 截断到一半 | js/ts/rs/vue 干净 rc=2 拒绝；go/py rc=0（切口恰落在完整单元上） | 行为差异，非崩溃，见「没修」 |
| 空文件 / 纯注释 | rc=0 零块（py/js/ts/rs）；go 空文件 rc=2（无 package 子句）；vue 无 script 块 rc=2 | 均干净，符合各自既有语义 |
| 超长单行（200 KB） | 全部正常 | 初测 rs/vue 的 rc=2 是**语料错误**（Rust const 缺类型标注、vue 没包 script 块），改对语料后 rc=0——分段器没错 |

## 修了什么（最小改动，语义不变）

1. **has_error 改迭代**（chunk_js / chunk_ts / chunk_go / chunk_rust
   四处同一实现；chunk_vue 复用 js/ts 的同名函数，一并受益）。
   显式栈遍历，布尔语义与递归版一致。修后 Go/Rust 200 层正常出块；
   额外探针：JS 1000 层嵌套 rc=0 出块。
2. **读文件兜 UnicodeDecodeError**（六个分段器入口各一处）：
   不是合法 UTF-8 → stderr 一句「源文件不是合法的 UTF-8 文本」+ rc=2。
3. **chunk_big 兜 SyntaxError**：main 里 preamble_end + collect_items
   包一层，语法损坏 → rc=2，与其他语言「先修语法再切」的立场对齐。

验证：修复后 56 个语料文件零违反；撤掉修复（git stash）脚本立刻红，
点名 11 个文件 19 条违反；全量门禁 117 + 116 + 13 + regress 七步
在修复前后都不变、未动任何断言。

## 没修（记录在案，等裁决或等数据）

- **损坏文件的语言间行为差异没有统一**：Go 的解析器能恢复就切出
  能切的块，js/ts/rs 是整文件 ERROR 就拒绝。哪边是产品立场，
  不是鲁棒性问题，是设计问题，本文不替它定。
- **Python 缩进超过 100 层的文件会被拒**（rc=2）：这是 CPython
  解析器自身的上限，不是分段器的限制，报错信息里带原文。
- **BOM / CRLF / 无尾换行变体不在本文范围**：PR #7 已覆盖
  （六语言 × 三变体），不重复造。
- **Windows 未验**：本轮脚本与修复只在 Linux 跑过。修复本身是
  纯 Python 逻辑（迭代遍历、异常兜底），不碰路径与控制台编码，
  但按 diff 模式的教训，这里只写「未验」，等 CI 四矩阵说话。
