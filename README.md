# AuditX OCR Bridge

**把 [open-code-review](https://github.com/alibaba/open-code-review) 的审查结果，接进一个有签名、有哈希链、可独立复核的审查账本。**

```
OCR 评论  →  [映射]  →  [行号回映]  →  [假报过滤]  →  [去重]  →  账本事件
            schema        chunk          symbol        同位置
            对齐         manifest       存在性        保留最高级
```

> 单独的代码审查只能给你一条评论，它证明不了"谁在什么时候、
> 凭什么、看了哪一版代码"。这个桥把评论变成账本事件。

---

## 这个仓库里有什么

| 文件 | 作用 |
|---|---|
| `ocr_adapter.py` | OCR 的 JSON 输出 → 账本事件参数（映射、证据转换、行号回映、假报过滤、去重） |
| `chunk_big.py` | 按 AST 切分大 **Python** 文件，保证每块语法完整（避免审查模型把"缺 import"当成真缺陷） |
| `chunk_js.py` | 按 tree-sitter 语法树切分大 **JavaScript** 文件，同一套契约（`chunk-manifest/2`） |
| `js_symbols.py` | JS 符号表抽取（给跨块假报过滤器用；Python 侧对应的是内置 ast 抽取） |

**审查庭引擎本身（`review_brain_v2.py`）不在这个仓库里**，它是私有组件。
本仓库是纯转换层：不带 `--post` 时不需要私有依赖。

依赖：纯转换模式零依赖；**JS 分段需要 `tree_sitter` 与 `tree_sitter_javascript`**
（`pip install tree_sitter tree_sitter_javascript`，都有现成 wheel，无需编译）。
没有装这两个包时，Python 侧一切照常，`.js` 会明确报错而不是静默降级。

---

## 为什么需要做映射

OCR 的评价口径和账本的口径不一样，硬塞会直接被 schema 拒掉：

| OCR | 账本 | 说明 |
|---|---|---|
| `critical` / `high` / `medium` / `low` | `BLOCKER` / `MAJOR` / `MINOR` / `INFO` | 四档对四档 |
| `category`（bug / security / performance…） | `dimension`（facts / logic / completeness / security / fit / quality） | 按关键词归类，归不进去落 `quality` |
| `existing_code` | `evidence: [{kind: "quote", content}]` | 没有原文时降级为 `{kind:"ref"}` |
| `thinking` | **丢弃** | 模型思考链不进账本 |

---

## 三个真实存在的坑

这三个不是理论问题，是接入过程中实际踩到的：

### 1. 分段后的行号是错的

按 240 行切断 1478 行的文件后，模型报的是"第 5 行"——那是**它看到的那个块文件**的第 5 行，
不是源文件第 5 行。直接入库的后果是：报错位置指向一段无关代码。

关键是要认清模型看到的是什么：块文件 = 前导区 + 分隔空行 + （可能有一个补出来的 `class X:` 头）+ body。
模型报行号时**不会**帮你减掉前面那些行，它报的就是块文件的绝对行号。

`chunk_big.py` 因此在 `manifest.json` 里给每块记 `body_start`——body 第一行落在块文件的第几行。
适配器按 `源行 = start_line + (块内行 - body_start)` 回映：

```powershell
python chunk_big.py src/big.py --out .chunks
python ocr_adapter.py ocr.json --json --chunk-manifest .chunks/manifest.json --source-name src/big.py
```

**不要在外面自己再推这个偏移。** 前导区、分隔空行、类头各占几行，只有分段器自己知道；
以前适配器按"块内第 1 行 = body 第 1 行"推，少算了一个常数，导致所有回映行号整体偏移
`前导行数 + 1`。这个 bug 藏在测试里很久——因为测试断言的就是公式本身（拿实现当真值，循环论证）。

> **实测裁决**（2026-11-27，deepseek-chat 真实扫描）：源文件前导 12 行，
> `os.system(cmd)` 在源文件第 20 行、块文件第 16 行。模型报 **16**（块文件绝对行号）。
> 旧公式给 33，正确值 20。现在 `tests/test_bridge.py` 的 F1 组拿**源文件真值**断言，
> 不再断言公式。

### 2. 跨块假报

前导区（import / 常量）每块都补了一遍，但**定义在另一个块里的函数，在本块看来就是未定义**。
模型会认认真真地报"`sqlite3` 未导入"——而它明明就在源文件第 1 行。

适配器的策略是拿完整源文件做符号表核对（`--source`）：

- 报的是**名字未定义** → 去源文件查这个名字**有没有被定义**（函数、类、形参、赋值、import）
- **全部**提到的符号都已定义 → 判定为跨块假报，丢弃
- **只要有一个**没定义 → 保留（宁可信其有）

> 这里走过弯路：第一版按关键词粗筛，把一个真实的"`sqlite3` 漏导入"缺陷也丢掉了。
> 所以规则收紧成"全部命中才丢"，而不是"命中一个就丢"。

### 3. 同一行被报多次

不同块重叠、或模型就同一处给两条建议，会产生重复条目。
按 `(文件, 起始行)` 去重，保留严重度最高的那条。

---

## 用法

### 只转换，不入库（推荐先这样）

```powershell
python ocr_adapter.py ocr.json --json -o mapped.json
```

输出是账本 `post_finding` 的参数形状，`validation_problems` 里列出 schema 校验问题。

### 带分段和假报过滤

```powershell
python chunk_big.py src/big.py --out .chunks            # 切分 + 自检
ocr scan --path src/big.py --format json -o ocr.json    # 或逐块扫 .chunks
python ocr_adapter.py ocr.json --json `
  --chunk-manifest .chunks/manifest.json `
  --source src/big.py --source-name src/big.py
```

### JavaScript

同一套契约，只换分段器与符号表：

```powershell
pip install tree_sitter tree_sitter_javascript
python chunk_js.py src/index.js --out .chunks --no-git
ocr scan --path .chunks/chunk_02_submit.js --format json -o ocr.json
python ocr_adapter.py ocr.json --json `
  --chunk-manifest .chunks/manifest.json `
  --source src/index.js --source-name index.js
```

`--source` 按后缀自动选符号表：`.py` 用内置 ast，`.js/.mjs/.cjs/.jsx` 用 `js_symbols.py`。
JS 符号表包含语言内置全局（`Number`/`JSON`/`Object`…），否则提到内置对象的断言
永远满足不了"全部符号已定义"，跨块假报就过滤不掉。

分段器验收（切分 / 契约字段 / 块可解析 / `body_start` 几何真值）：
`python qa/regress_chunker.py --cmd "python chunk_js.py" --target <目录> --ext .js`

### 直接写进账本

```powershell
$env:PYTHONPATH = "C:\path\to\auditx-engine"   # 放 review_brain_v2.py 的目录
python ocr_adapter.py ocr.json --post cases/case-01 --seat S-OCR1
```

席位必须已经在账本里 `grant_seat` 过。引擎不在 `PYTHONPATH` 里时会明确报错并建议走纯转换模式。

---

## 分段器的三条保证

1. **每块都能 `ast.parse` 通过**——切完逐块自检，不过就报错退出（退出码 1）
2. **每块都带源文件的 import / 常量前导区**——不补的话模型会报一堆"未定义"
3. **大类按方法切时补 `class X:` 头**——不补的话方法体缩进直接 `IndentationError`

`--max-lines` 默认 240。太大模型会超时，太小会切碎语义。

> 这三条**只保语法，不保语义**。装饰器是踩过的坑：Python 3.8+ 起 `FunctionDef.lineno`
> 指向 `def` 行、不含装饰器，早期版本于是把第一个顶层函数的 `@decorator` 留在前导区、
> 补进每一块（在别的块里悬空、粘到下一个语法对象上），方法自己的 `@property` 又被切丢。
> 现在两边都用 `effective_start()`（取 `lineno` 与装饰器行的最小值），装饰器始终跟着它装饰的对象走，
> 前导区永远不含悬空装饰器。

---

## 边界（重要）

- **分段与行号回映目前支持 Python 与 JavaScript**（`.js/.mjs/.cjs/.jsx`）。
  其他语言只能整文件喂给 OCR，**没有 manifest，也就没有行号回映**。
  `.vue` 单文件组件、TypeScript 语法**暂不支持**——前者要先抽 `<script>`，后者要换语法库，
  都属于尚未实现的部分，别当成本桥能做。
  遇到不认识的 `language`，适配器会**明确拒绝回映**（`status=unsupported-language`），不猜。
- **这个桥不判断对错。** 它只做格式转换和去重。上游模型报错了，账本里就是一条错误的缺陷记录——
  只是这条错误记录是**可追溯、可质证、可被复核出来的**。
- **假报过滤只看符号是否定义，看不懂语义。** 模型说的"这个逻辑反了"它一概不管。
- **不保证覆盖率。** 模型没报的地方就是没报，账本不会告诉你"这里没审到"。
- **依赖上游 OCR 的输出格式。** `@alibaba-group/open-code-review` 改版可能让映射失配。
- **`category` 归类靠关键词匹配**，中英双语都收，仍会有归错维度的情况（不影响严重度）。
- **行号回映依赖 `manifest.json` 的 `body_start`。** 旧版分段器产出的清单没有这个字段，
  适配器会降级为旧公式并**明确告警**（`legacy`），不会假装正确。请用同版本 `chunk_big.py` 重新切分。
  块路径对不上时也不再静默跳过：`stats.remap.unmatched_paths` 会列出来。

---

## 测试

```powershell
python -m py_compile ocr_adapter.py chunk_big.py
python tests/test_bridge.py        # 67 项自检，不依赖 pytest
python chunk_big.py tests/fixture_big.py --out .test_chunks --no-git
```

`tests/test_bridge.py` 分十组：

| 组 | 覆盖 |
|---|---|
| A | 分段器三条保证（每块可解析 / 带前导区 / 方法块带类头 / 行号单调） |
| B | 映射、证据转换、假报识别、去重、校验器 |
| C | 纯转换端到端（含行号回映真值） |
| D–J | 已修 bug 的回归：装饰器边界、回映边界、中文语义、脏 JSON、输出护栏、fail-closed、`-o` |

CI 在 Python 3.11 / 3.12 × Ubuntu / Windows 上跑 `py_compile` + 全量自检。

---

## License

MIT。上游 [open-code-review](https://github.com/alibaba/open-code-review) 是 Apache-2.0，
本仓库只做格式转换，不包含其代码。

## 相关

- [AuditX Verifier](https://github.com/Comet815/auditx-verifier) — 账本的独立复核器（V1-V11）
- [open-code-review](https://github.com/alibaba/open-code-review) — 上游审查工具
