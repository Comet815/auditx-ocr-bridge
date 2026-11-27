# diff 模式（`ocr review`）回归记录

## 2026-11-19 的结论（历史）

2026-11-19 实测：open-code-review 的 diff 模式（`ocr review`，只审 git diff）
可用，且对同一批缺陷，diff 审查的定位比整文件 scan 更准——diff 自带上下文，
模型报的行号就是改动行附近，不需要先分段。这一结论此后被当作已知事实引用，
但之后适配器与分段器经历多次提交（映射表、回映、行号规整、输入加载层都动过），
diff 模式的输出再没有完整流经过一次适配器。**没有证据它失效，也没有证据它还有效。**

## 2026-11-27 复验（接手工单任务 2）：链路已通，真跑已补

这次复验由两人分工完成：脚本与 CLI 界面验证在无模型端点的云端 VM 上做，
带模型的真跑在配了端点的本机做。**结论是链路有效**，下面分开写证据。

### 已证实（带真实模型的一次完整真跑）

```
$ python qa/regress_diff_mode.py --ocr-cmd "C:\Users\Administrator\AppData\Roaming\npm\ocr.cmd"
ocr CLI 可用：https://github.com/alibaba/open-code-review
已造仓库：calc.py 引入未定义的 tax_rate（坏改动，未提交）
✅ diff 模式链路通过：ocr review → 适配器 → 1 条 finding，其中 1 条指向 calc.py
exit 0
```

| 项 | 值 |
|---|---|
| 时间 | 2026-11-27 17:2x（Asia/Shanghai） |
| 机器 | Windows 本机（与 11-19 那次同一台），Python 3.15 |
| 上游 | open-code-review v1.14.4（d4f568f7）windows/amd64，构建于 2026-11-26 |
| 模型端点 | `~/.opencodereview/config.json` 的 deepseek provider（deepseek-chat） |
| 样本 | 临时 git 仓库，`calc.py` 未提交改动引入未定义的 `tax_rate` |
| 结果 | 模型报 1 条，适配器转换 1 条，locator 指向 `calc.py` |

这条证据同时答掉了 11-19 之后一直悬着的问题：**适配器在 3a673c6 上仍然吃得下
diff 模式的输出**。11-19 的结论可以在「这个版本、这个形状」的范围内继续引用。

### 已证实（脚本 machinery 与上游界面，无模型也能验）

- 上游 CLI 界面未变：`review` 子命令仍在，`--format json`、`--from/--to/--commit`
  等用法与 11-19 时一致（v1.14.4 实测 `ocr version` 与 `review --help`）。
- 脚本 machinery 完整跑通：临时仓库、基线提交、坏改动都按预期生成。
- 适配器侧不需要任何改动即可被脚本驱动。

### 仍未证实的（别把上面那条真跑读大）

- 只跑过**一个**坏改动样本（单文件、未提交改动、一处未定义名）。多文件 diff、
  大 diff、只删不增的 diff、`--from/--to/--commit` 各变体，一次都没跑过。
- 只验过 deepseek 一家端点。模型换家，措辞变了，finding 条数可能变 0——
  那时退出码 1 未必是链路坏了，读脚本的错误文案区分。
- 「diff 审查定位比 scan 更准」是 11-19 的对比结论，这次没有重做对比，
  引用时仍按 11-19 的强度写。

## 本轮修掉的两个 Windows 缺陷（实测复现后修）

`qa/regress_diff_mode.py` 初版在 Windows 上有两处会直接崩，均已复现并修：

1. **`ocr` 找不到时抛 traceback，而不是干净报环境缺失。**
   npm 装的 `ocr` 是 `ocr.cmd` / `ocr.ps1` 垫片，`subprocess` 不带 shell 解析不了
   裸名 `ocr`，抛 `FileNotFoundError`，脚本以 rc=1 崩掉——和「链路断言失败」
   同一个退出码，等于把环境问题误报成代码问题。
   修法：`run()` 捕获 `FileNotFoundError`/`OSError`，返回 rc=127 的
   `CompletedProcess`，由调用方按「环境缺失」走 rc=3。
2. **`--ocr-cmd` 传 Windows 路径会被 `shlex.split` 吃掉反斜杠。**
   `shlex.split(r'C:\Users\A\AppData\Roaming\npm\ocr.cmd')` 的结果是
   `['C:UsersAAppDataRoamingnpmocr.cmd']`——整条路径被压平成一个不存在的名字。
   修法：`shlex.split(..., posix=(os.name != "nt"))`。

修完复验：裸 `ocr` 场景 rc=3 并打印
`环境缺失：ocr CLI 不可运行（'ocr'）：找不到可执行文件`；
反斜杠路径场景 rc=0 拿到上面的真跑结果。

## 在带模型的机器上复跑

```bash
ocr config   # 或设置 OCR_LLM_URL / OCR_LLM_TOKEN / OCR_LLM_MODEL
python qa/regress_diff_mode.py            # ocr 在 PATH 时
python qa/regress_diff_mode.py --ocr-cmd "C:/Users/me/AppData/Roaming/npm/ocr.cmd"     # Windows 垫片
python qa/regress_diff_mode.py --ocr-cmd "bun x @alibaba-group/open-code-review"       # 无 node 时
```

退出码 0 = 链路通过（适配器吃下 diff 输出、至少 1 条 finding 指向被改文件）；
1 = 链路断言失败（这才是需要查适配器的信号）；3 = 环境缺失；
2 = 脚本自身用法/内部错误。

## 与 scan 模式的关系

diff 模式不经过分段器：diff 本身就是小块，行号是仓库文件的真实行号，
适配器在无 manifest 模式下消费即可。因此分段器历次改动（chunk-manifest/2、
body_start 几何）不影响 diff 模式；真正可能影响它的是适配器的输入加载与
字段映射。3a673c6 的三处改动（顶层数组拒绝、行号规整、comments 类型守卫）
都针对 scan 模式的真实输出形状，而上面那次真跑证明 diff 模式（同为顶层对象
+ comments 数组）形状兼容——这一条现在是**跑出来的**，不是读代码推的。
