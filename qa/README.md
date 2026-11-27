# QA 记录

本目录保留 2026-11-27 那次外部质检的原始材料与复核工具。

| 文件 | 用途 |
|---|---|
| `质检报告-2026-11-27.md` | 原始报告，**未改动**，保留原文以便逐条对照 |
| `check_remap.py` | 行号回映裁决器：拿 `mapped.json` / 账本记录，对源文件核真值 |
| `bridge_probe.py` | 四组对抗探针：回映公式 / 装饰器 / 中文过滤 / 杂项 |

## 复跑

```powershell
python qa/bridge_probe.py                       # 需在仓库根目录运行
python qa/check_remap.py mapped.json --source src/big.py
```

> `bridge_probe.py` 里那些「结论：…」是报告作者写死的说明文字，不是运行时判断。
> 修完之后实测值会变（中文 claim 全部丢弃、装饰器不再悬空、块首不再有空行），
> 但静态文案不会跟着变——看**实测那一行**，别读结论段。

## 各条处理结果

| # | 结论 | 处理 |
|---|---|---|
| F1 | **实锤**（报告说"待裁决"，已用真实模型裁决） | manifest 新增 `body_start`，回映按它算；另修报告漏掉的**类头也占一行** |
| F2 | 已证实 → 已修 | `effective_start()`：区间起点取 `lineno` 与装饰器行的最小值 |
| F3 | 已证实 → 已修 | 闸门补中文句式 |
| F4 | 已证实 → 已修 | `raw_decode`，尾部日志不再崩 |
| F5 | 已证实 → 已修 | 维度规则补中文关键词 |
| F6 | 静态审查成立 → 已修 | `--post` fail-closed；`--allow-invalid` 只跳过坏条目并留痕 |
| F7 | 静态审查成立 → 已修 | 目标目录不像分段产物则拒绝清空；另拒盘符根与源文件上级目录；`--force` 放行 |
| F8 | 已证实 → 已修 | README 测试节重写；B7/C6 改为对**源文件真值**断言 |
| F9 | 已证实 → 已修 | 空前导区不再产生块首空行；manifest 增加 `failed_detail` |
| F10 | 静态审查成立 → 已修 | `stats.remap.unmatched_paths` + 告警；另加 basename 兜底（见下） |
| F11 | 已证实 → 已修 | `-o/--out` 真实现 |
| F12 | 已证实 → 已修 | `--source-name` 仅在命中 manifest 时生效；`_source` 增 `ocr_path` |

### F1 的裁决依据

报告给的办法是用 `check_remap.py` 对已有数据跑。但手上没有当时的 `mapped.json`，
所以直接做了真实模型实验（`deepseek-chat`，源文件前导 12 行）：

```
os.system(cmd)   源文件第 20 行   块文件第 16 行
模型报 → 16
旧公式 → 18 + 16 - 1 = 33   （错 13 行 = 前导 12 + 1）
修好后 → 20                  ✓
```

模型报的是**它看到的块文件的绝对行号**，不是 body 相对行号。假设被证伪，公式已改。

### 报告之外的一个真坑

真实 `ocr scan --path .chunks` 输出的 `path` 是 `chunks/chunk_02_x.py`，
而 manifest 里存的是裸文件名 `chunk_02_x.py`。原逻辑下这会导致**全部** finding
静默不匹配（F10 的加强版，实际必撞）。现已加 basename 兜底，并计入
`stats.remap.basename_matched`。
