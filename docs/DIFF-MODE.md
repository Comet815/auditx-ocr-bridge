# diff 模式（`ocr review`）回归记录

## 2026-11-19 的结论（历史）

2026-11-19 实测：open-code-review 的 diff 模式（`ocr review`，只审 git diff）
可用，且对同一批缺陷，diff 审查的定位比整文件 scan 更准——diff 自带上下文，
模型报的行号就是改动行附近，不需要先分段。这一结论此后被当作已知事实引用，
但之后适配器与分段器经历多次提交（映射表、回映、行号规整、输入加载层都动过），
diff 模式的输出再没有完整流经过一次适配器。**没有证据它失效，也没有证据它还有效。**

## 2026-11-27 复验（接手工单任务 2）

复验脚本：`qa/regress_diff_mode.py`（造临时 git 仓库 → 坏改动 →
`ocr review --format json` → 适配器 → 断言链路）。本次运行结果分三段：

**已证实的：**

- 上游 CLI 界面未变：当前版本（构建于 2026-11-26）的 `review` 子命令仍在，
  `--format json`、`--from/--to/--commit` 等用法与 11-19 时一致。
- 脚本 machinery 完整跑通：临时仓库、基线提交、坏改动（未定义的 `tax_rate`）
  都按预期生成；适配器侧不需要任何改动即可被脚本驱动。

**卡住的（环境，不是链路）：**

- `ocr review` 在本机停在 LLM 配置检查，原文：
  `no valid LLM endpoint configured; one of OCR_LLM_URL/OCR_LLM_TOKEN/OCR_LLM_MODEL, ~/.opencodereview/config.json, or ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_MODEL`
- 本机（云端 VM）没有配置任何 OCR 用的模型端点，脚本以退出码 3
  明确报「环境缺失」退出，与链路失败（退出码 1）分开。

**仍未证实的：**

- 一次真实的、带模型的 diff 审查输出完整流过适配器并产出指向改动文件的
  finding——这一步需要带模型配置的机器，本机做不了，不冒充已验。

## 在带模型的机器上复跑

```bash
ocr config   # 或设置 OCR_LLM_URL / OCR_LLM_TOKEN / OCR_LLM_MODEL
python qa/regress_diff_mode.py            # ocr 在 PATH 时
python qa/regress_diff_mode.py --ocr-cmd "bun x @alibaba-group/open-code-review"  # 无 node 时
```

退出码 0 = 链路通过（适配器吃下 diff 输出、至少 1 条 finding 指向被改文件）；
1 = 链路断言失败（这才是需要查适配器的信号）；3 = 环境缺失。

## 与 scan 模式的关系

diff 模式不经过分段器：diff 本身就是小块，行号是仓库文件的真实行号，
适配器在无 manifest 模式下消费即可。因此分段器历次改动（chunk-manifest/2、
body_start 几何）不影响 diff 模式；真正可能影响它的是适配器的输入加载与
字段映射，而这两处在 3a673c6 的改动（顶层数组拒绝、行号规整、comments
类型守卫）都针对 scan 模式的真实输出形状，diff 模式输出同为顶层对象 +
comments 数组，形状兼容——但这只是代码阅读结论，不替代上面那次真跑。
