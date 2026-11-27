# 性能基线 · 分段器在真实大文件上的耗时

测于 2026-11-27，接手工单任务 1。目的只有计量，不做优化结论：
先有基线，以后任何「改快了/改慢了」才有对照。

## 环境

| 项 | 值 |
|---|---|
| 机器 | 云端 VM，AMD EPYC 9D25（共享 vCPU，nproc 4），内存 7.9 GB |
| Python | 3.12.16（python-build-standalone） |
| tree_sitter | 0.27.0；javascript 0.25.0 / typescript 0.23.2 / go 0.25.0 / rust 0.24.2 |
| 仓库版本 | 3a673c6（main） |
| 参数 | 各分段器默认参数（--max-lines 240），--no-git |

## 结果（每文件跑 3 次，耗时取中位）

| 语言 | 文件 | 行数 | 块数 | 失败块 | 耗时中位 |
|---|---|---:|---:|---:|---:|
| Python | review_brain_v2.py（私有引擎文件，仅本地计量，文件本身不进本仓） | 1478 | 7 | 0 | 119 ms |
| JavaScript | 昆仑云函数 learn/index.js（客户代码，仅统计量） | 625 | 3 | 0 | 108 ms |
| Go | golang/go `src/strings/strings.go`（master） | 1304 | 7 | 0 | 129 ms |
| Go | gin-gonic/gin `context.go`（master） | 1543 | 7 | 0 | 128 ms |
| Rust | BurntSushi/ripgrep `crates/core/flags/defs.rs`（master） | 8153 | 37 | 0 | 306 ms |

块数与此前验收记录一致（strings.go 7 块、defs.rs 37 块），可互相印证。

内存量级：子进程峰值 RSS 以 `resource.getrusage(RUSAGE_CHILDREN)` 量，
解释器与语法加载的基线约 22 MB；最大的 defs.rs 峰值约 35 MB。
即这一量级的文件上，内存是几十 MB 级别，不构成约束。

## 怎么读这张表

- 耗时是**进程墙钟**，含解释器启动与语法加载（固定成本约 100 ms 量级）。
  所以 625 行的 JS 与 1304 行的 Go 耗时接近，不是 Go 特别快，
  是固定成本在小文件上占主导；行数涨到 8153 行（defs.rs）时，
  解析与切分的边际成本才显出来（306 ms，仍远低于 1 秒）。
- 结论只许引用到「当前实现下，万行级单文件切分是亚秒级、内存几十 MB」。
  不许引申到并发、批量仓库级吞吐——没有测过。

## 复现

```bash
# 开源样本（行数应与上表一致，master 漂移时以行数为准对不上就换 tag）
curl -sL -o strings.go https://raw.githubusercontent.com/golang/go/master/src/strings/strings.go
curl -sL -o context.go https://raw.githubusercontent.com/gin-gonic/gin/master/context.go
curl -sL -o defs.rs https://raw.githubusercontent.com/BurntSushi/ripgrep/master/crates/core/flags/defs.rs

python chunk_go.py strings.go --out out-strings --no-git
python chunk_go.py context.go --out out-context --no-git
python chunk_rust.py defs.rs --out out-defs --no-git
# 块数看各 out 目录下 manifest.json 的 chunks 长度与 failed 字段；
# 耗时用任意墙钟工具量三次取中位（如 bash time、python -m timeit 的子进程法）。
```
