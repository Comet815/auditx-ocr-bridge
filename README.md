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
| `chunk_big.py` | 按 AST 切分大 Python 文件，保证每块语法完整（避免审查模型把"缺 import"当成真缺陷） |

**审查庭引擎本身（`review_brain_v2.py`）不在这个仓库里**，它是私有组件。
本仓库是纯转换层：不带 `--post` 时零第三方依赖、零私有依赖。

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

按 240 行切断 1478 行的文件后，模型报的是"第 5 行"——那是块内第 5 行，不是源文件第 5 行。
直接入库的后果是：报错位置指向一段无关代码。

`chunk_big.py` 会产出 `manifest.json` 记下每块的原始行号区间，适配器用 `--chunk-manifest`
把它换算回去。

```powershell
python chunk_big.py src/big.py --out .chunks
python ocr_adapter.py ocr.json --json --chunk-manifest .chunks/manifest.json --source-name src/big.py
```

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

---

## 边界（重要）

- **这个桥不判断对错。** 它只做格式转换和去重。上游模型报错了，账本里就是一条错误的缺陷记录——
  只是这条错误记录是**可追溯、可质证、可被复核出来的**。
- **假报过滤只看符号是否定义，看不懂语义。** 模型说的"这个逻辑反了"它一概不管。
- **不保证覆盖率。** 模型没报的地方就是没报，账本不会告诉你"这里没审到"。
- **依赖上游 OCR 的输出格式。** `@alibaba-group/open-code-review` 改版可能让映射失配。
- 中文语境下 `category` 归类靠关键词匹配，会有归错维度的情况（不影响严重度）。

---

## 测试

```powershell
python -m py_compile ocr_adapter.py chunk_big.py
python chunk_big.py tests/fixture_big.py --out .test_chunks --no-git
```

CI 在 Python 3.11 / 3.12 × Ubuntu / Windows 上跑同样的两步。

---

## License

MIT。上游 [open-code-review](https://github.com/alibaba/open-code-review) 是 Apache-2.0，
本仓库只做格式转换，不包含其代码。

## 相关

- [AuditX Verifier](https://github.com/Comet815/auditx-verifier) — 账本的独立复核器（V1-V11）
- [open-code-review](https://github.com/alibaba/open-code-review) — 上游审查工具
