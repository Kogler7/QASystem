---
noteId: "5dfb9cc0865c11f1a953d7289dee1b45"
tags: []

---

# Max-Min 语义切片算法详解

## 1. 背景与动机

### 1.1 为什么需要语义切片

传统的规则切片（recursive、markdown、symbols）仅基于字符分隔符（如 `\n\n`、`。`）切分文本，完全不理解文本的**语义内容**。这导致：

- **主题断裂**：同一个主题的连续论述被硬切到不同切片中，检索时无法召回完整上下文。
- **主题混杂**：不同主题的内容被装入同一个切片，检索精度下降。

语义切片的目标是：**在语义边界处切分，让每个切片内部保持主题连贯**。

### 1.2 centroid 方法的问题

早期语义切片的典型做法是 **centroid 分组**（如 LangChain 的 `SemanticChunker` 和本项目旧版实现）：

```
对于每对连续句子 (sᵢ, sᵢ₊₁):
    计算 sᵢ₊₁ 与当前 chunk 平均向量（centroid）的余弦相似度
    如果相似度 >= 阈值 → 合并
    否则 → 切断，开始新 chunk
```

这种方法的核心缺陷是 **centroid 漂移**（centroid drift）：

```
Chunk 初始: [A₁, A₂, A₃]          centroid ≈ A 主题
加入 B₁ (相似度仍 ≥ 阈值): [A₁, A₂, A₃, B₁]   centroid 向 B 偏移
加入 B₂: [A₁, A₂, A₃, B₁, B₂]     centroid 进一步偏移
加入 C₁ (与当前 centroid 相似，但与早期 A 完全不相似):
  → C₁ 被合并！但实际上 C 与 A 是两个完全不同的话题
```

centroid 是不断变化的聚合值，随着 chunk 增长逐渐偏离初始语义。后期片段可能只与当前 centroid 相似，而与 chunk 早期内容完全无关——这就是**弱语义边界**（weak semantic boundary）问题。

## 2. Max-Min 算法原理

### 2.1 核心思想

Max-Min 算法由 Bhat et al. (2025) 在论文 *"Max–Min semantic chunking of documents for RAG application"* 中提出。核心思想是用 **pairwise 相似度比较** 替代 centroid 聚合：

> 一个新片段能否加入当前 chunk，取决于它是否与 chunk 内至少一个已有片段足够相似——"足够"的标准是：不低于 chunk 内现有最不相似的片段对之间的相似度。

直观理解：

> 新人要加入一个团体，他最好的朋友（MaxSim）必须至少和团体内最疏远的两个人（MinSim）一样亲密。

### 2.2 数学定义

设当前 chunk $C = \{s_0, s_1, ..., s_{k-1}\}$，新片段为 $s_k$，其 embedding 向量为 $\mathbf{e}_k$。

**MinSim（chunk 内部最弱连接）**：

$$\text{MinSim}(C) = \min_{0 \leq i < j < k} \cos(\mathbf{e}_i, \mathbf{e}_j)$$

**MaxSim（新片段与 chunk 的最强关联）**：

$$\text{MaxSim}(s_k, C) = \max_{0 \leq i < k} \cos(\mathbf{e}_k, \mathbf{e}_i)$$

**决策规则**：

- 当 $|C| = 1$（chunk 内只有 1 个片段）时：
  - 若 $\text{MaxSim}(s_1, C) \geq \tau$ 且 $\text{MaxSim}(s_1, C) \geq \theta_{\min}$ → 合并
  - 否则 → 切断
  - 其中 $\tau$ 为初始阈值（由 `similarity` 参数控制，默认 0.6）

- 当 $|C| \geq 2$ 时：
  - 若 $\text{MaxSim}(s_k, C) \geq \text{MinSim}(C)$ 且 $\text{MaxSim}(s_k, C) \geq \theta_{\min}$ → 合并
  - 若上述条件不满足，但 $|C| < k_{\min}$（`min_segments` 保底，由 `split_semantically` 以 `min_segments_per_chunk` 传入，默认 2）且加入后不超过 `chunk_size` → 仍强制合并
  - 否则 → 切断
  - 其中 $\theta_{\min}$ 为相似度下限（由 $\tau - 0.15$ 自动推导，下限 0.3）

### 2.3 为什么 Max-Min 消除了 centroid 漂移

Max-Min 不依赖聚合向量，而是直接比较 segment 之间的 pairwise 相似度：

- **MinSim 自适应**：随着 chunk 增长，内部最弱连接自然下降（更多样化的内容 → 更低的 MinSim），新片段需要更低的相似度门槛才能加入。这比固定阈值更符合语义直觉。

- **不丢失早期信息**：新片段需要与 chunk 内的**每一个**已有片段形成通路——它必须与 chunk 内至少一个片段比"当前最弱的 pair"更相似。这保证了语义链的连续性。

- **最弱连接保护**：如果新片段的加入会让 chunk 内部出现比当前 MinSim 更弱的 pair，它就会被拒绝。这防止了语义漂移。

### 2.4 算法伪代码

```python
def maxmin_group(segments, embeddings, chunk_size, tau, min_similarity, breakpoints, min_segments):
    chunks = []
    chunk_segs = [segments[0]]
    chunk_embs = [embeddings[0]]
    chunk_minsim = 1.0  # 只有 1 个片段时，无 pair，设为最大值

    for i in range(1, len(segments)):
        seg = segments[i]
        emb = embeddings[i]

        # 1. 硬断点 → 强制切分
        if i in breakpoints:
            chunks.append("".join(chunk_segs))
            chunk_segs = [seg]
            chunk_embs = [emb]
            chunk_minsim = 1.0
            continue

        # 2. 计算新片段与 chunk 内所有片段的 pairwise 相似度
        pairwise_sims = [cosine(emb, e) for e in chunk_embs]
        max_sim = max(pairwise_sims)

        # 3. 长度超限 → 关闭当前 chunk
        if current_length + len(seg) > chunk_size:
            chunks.append("".join(chunk_segs))
            chunk_segs = [seg]
            chunk_embs = [emb]
            chunk_minsim = 1.0
            continue

        # 4. Max-Min 决策
        if len(chunk_segs) == 1:
            # 第一个合并：使用 tau 作为初始阈值
            if max_sim >= tau and max_sim >= min_similarity:
                chunk_segs.append(seg)
                chunk_embs.append(emb)
                chunk_minsim = max_sim  # 2 元素集的唯一 pair
            else:
                chunks.append("".join(chunk_segs))
                chunk_segs = [seg]; chunk_embs = [emb]; chunk_minsim = 1.0
        elif max_sim >= chunk_minsim and max_sim >= min_similarity:
            # 更新 MinSim：检查是否有新 pair 比当前 MinSim 更弱
            for sim in pairwise_sims:
                if sim < chunk_minsim:
                    chunk_minsim = sim
            chunk_segs.append(seg)
            chunk_embs.append(emb)
        elif len(chunk_segs) < min_segments and current_length + len(seg) <= chunk_size:
            # 最小片段数保底：Max-Min 判定不合并时也先凑够 min_segments
            for sim in pairwise_sims:
                if sim < chunk_minsim:
                    chunk_minsim = sim
            chunk_segs.append(seg)
            chunk_embs.append(emb)
        else:
            chunks.append("".join(chunk_segs))
            chunk_segs = [seg]; chunk_embs = [emb]; chunk_minsim = 1.0

    if chunk_segs:
        chunks.append("".join(chunk_segs))

    # 5. 超长回退：任何单个 chunk 超过 chunk_size 的，按字符硬切
    return split_oversized(chunks, chunk_size)
```

## 3. 完整处理流水线

`split_semantically()` 实现的完整流水线包含 9 个步骤。另有一条贯穿全程的回退：本地 SentenceTransformer 与 remote-only 远程 embedding 均不可用时，文本片段退化为规则递归切分（等价 `recursive` 策略），不执行向量化。

### 3.1 流水线总览

```
输入文本
  │
  ▼
[1] JSON 块识别与隔离        ← _split_text_with_json_protection / _extract_json_blocks
  │
  ▼
[2] Markdown 标题预拆分      ← _split_by_markdown_headers，按 # / ## 拆分 section
  │
  ▼
[3] 句子级拆分               ← _split_into_segments，每个 section 内按标点拆分为原子片段
  │
  ▼
[4] 批量向量化               ← model.encode()，所有片段一次性编码（避免多次模型调用）
  │
  ▼
[5] 微小片段合并             ← _merge_tiny_segments，< 20 字符的噪声片段就近合并
  │
  ▼
[6] 语义断点检测             ← _detect_breakpoints，在 consecutive similarity 谷值处标记硬断点
  │
  ▼
[7] Max-Min 语义分组         ← _group_segments_maxmin，核心算法；组内超长由 _hard_split 兜底
  │
  ▼
[8] 跨块碎片清扫             ← _merge_no_leftover / _eliminate_leftovers
  │
  ▼
[9] 字符级重叠后处理         ← _apply_overlap / _tail_with_boundary（仅相邻文本块）
  │
  ▼
输出切片列表
```

步骤 [1] 的 JSON 块识别由所有切片策略共享（见[文档切片算法说明](文档切片算法说明.md) 2.1 节）；识别出的 JSON 块不参与语义切分，直接整块落盘。注意 `semantic`（与 `markdown`）路径对超长 JSON 不做结构切分——`_split_json_by_structure` / `_hard_split_json` 只在 `recursive` / `custom_recursive` 路径执行。另注意步骤 [4] [5] 的顺序：先对全部原子片段批量编码，微小片段合并时用缓冲区各片段向量的均值合成代表向量。

### 3.2 步骤详解

#### Step 1: JSON 块识别与隔离（`_split_text_with_json_protection` / `_extract_json_blocks`）

扫描整段文本，用括号匹配 + `json.loads` 双重验证，识别所有完整的顶层 JSON 块（对象 `{...}` 与数组 `[...]`）。提取时向前检查前一个字符（避免把 `x{"a":1}` 误判）、向后检查后一个字符（避免把 `{"a":1}bc` 误判），只保留真正独立的 JSON 块；JSON 块连同紧贴的 ```` ```json ```` / ```` ``` ```` 围栏作为独立片段。随后将文本拆成有序的 `(片段, is_json)` 列表。

**意义**：保证结构化数据不被切坏。JSON 一旦被拦腰切断，下游向量化、检索都会拿到残缺内容。识别出的 JSON 块不参与语义切分流程，直接整块落盘，结构与语义边界都得以保全。

#### Step 2: Markdown 标题预拆分（`_split_by_markdown_headers`）

使用正则 `^(#{1,6}\s)` 识别 Markdown 标题行，将文档拆分为独立 section。每个 section 以标题行开头，包含该标题下的所有内容。若首个 section 只有标题、无正文内容（标题下无其他行），将其合并进紧随其后的 section，避免产生标题孤岛。

```
输入:
  # 第一章
  内容A...
  ## 1.1 小节
  内容B...
  # 第二章
  内容C...

输出 sections:
  [0] "# 第一章\n内容A..."
  [1] "## 1.1 小节\n内容B..."
  [2] "# 第二章\n内容C..."
```

**意义**：标题是作者明确的语义边界标记。在此处预拆分可以防止后续语义分组跨标题合并内容，确保不同章节的内容始终保持独立。每个 section 独立编码、独立分组，避免跨章节的噪声干扰相似度计算。

无标题的文档作为一个整体 section 处理。

#### Step 3: 句子级拆分（`_split_into_segments`）

在每个 section 内，按以下分隔符优先级逐个匹配前缀，拆分为原子片段。匹配到分隔符时将其并入前一个片段的尾部，保证切分点落在标点之后，不产生半句：

```python
[
    "\n\n",   # 段落空行（最高优先级）
    "。", "！", "？", "；",   # 中文句末标点
    ". ", "! ", "? ", "; ",   # 英文句末标点（含尾随空格，防止误切缩写）
]
```

**不使用** `\n`（单换行）和 `:` / `：`（冒号）：
- 单换行在结构化 Markdown（术语库、表格）中不代表语义边界，使用会导致行级过度切分。
- 冒号广泛存在于 `术语名称: xxx` 等结构化字段中，误切会拆分属性键值对。

**意义**：把文本降到句子粒度。语义相似度的最小计算单元是句子，粒度再细会引入大量噪声，再粗则无法在句间找到真正的语义断点。

#### Step 4: 批量向量化（`model.encode()`）

将所有 section 的全部片段收集后，**一次性**调用 `model.encode()` 进行批量编码。这利用了 SentenceTransformer 的批量推理优化（GPU/CPU batched inference），避免多次模型调用的开销。

使用 `BAAI/bge-small-zh-v1.5` 模型，输出 512 维向量。

**意义**：把文本映射到向量空间，使"语义相近"可被余弦相似度量化。这是语义切分区别于递归切分、Markdown 切分的核心——它依据内容含义而非分隔符或标题层级做决策。

#### Step 5: 微小片段合并（`_merge_tiny_segments`）

长度 < `min_segment_len`（默认 20 字符）的片段（如 YAML frontmatter、空行标记、分隔线 `---` 等）不产生有意义的独立 embedding。它们在向量化后与其相邻的内容片段合并：

- 孤立微小片段向前合并到下一个非微小片段；缓冲区内的多个短片段先对各自向量取均值，合成一个代表向量
- 尾部微小片段合并到最后一个非微小片段
- 合并后的 embedding 取各片段 embedding 的均值
- 若文档全部由短片段组成（无可合并的正常片段），则保留合并后的结果

**意义**：过滤掉标点、编号、孤立短词这类无实际语义的片段。它们单独编码后向量方向随机，会制造大量伪断点，拉低分组质量。

#### Step 6: 语义断点检测（`_detect_breakpoints`，Breakpoint Detection）

在 Max-Min 分组之前，先进行一次全局扫描，识别 consecutive similarity 曲线的**谷值**作为硬断点：

```python
# 计算所有相邻片段的余弦相似度
similarities = [cosine(emb[i], emb[i+1]) for i in range(n-1)]

# 根据方法确定阈值
if method == "percentile":
    threshold = percentile(similarities, 15)  # 底部 15% 的相似度值
elif method == "stddev":
    threshold = mean(similarities) - 1.5 * std(similarities)
elif method == "none":
    return set()  # 关闭断点检测
```

相似度低于阈值的相邻位置被标记为**硬断点**（hard breakpoint），Max-Min 算法在这些位置**强制切分**，不执行 Max-Min 决策。

**为什么需要断点检测？** 在某些文档中，segment A 和 segment B 都在同一个宽松主题内（如"技术发展趋势"），Max-Min 可能将它们合并。但 consecutive similarity 的骤降（如从 0.85 骤降到 0.35）可能表明这里存在一个自然的子主题边界。断点检测捕获这些信号，为 Max-Min 提供额外的边界提示。它是一道保险：即使 Max-Min 的局部决策倾向于把新片段并入当前组，遇到强断点也必须落盘。

**参数选择建议**：

| method | 适用场景 | 行为 |
| --- | --- | --- |
| `"percentile"` | 通用，适合大多数文档 | 将底部 15% 的低相似度对标记为断点 |
| `"stddev"` | 相似度分布接近正态的文档 | 低于均值 1.5 倍标准差的标记为断点 |
| `"none"` | 片段数较少（< 5）或主题非常连贯 | 关闭检测，完全依赖 Max-Min |

**已知限制**：当片段数 < 4 时，percentile 和 stddev 方法均不可靠（统计样本太少）。此时建议使用 `"none"`。

#### Step 7: Max-Min 语义分组（`_group_segments_maxmin`）

详见第 2 节。核心改进在于：

- **消除 centroid 漂移**：用 pairwise 比较替代 centroid 聚合
- **自适应阈值**：MinSim 随 chunk 大小自动调整
- **最小片段数保底**：`min_segments_per_chunk`（默认 2）保证单片至少包含的片段数，判定不合并时也强制凑够
- **硬断点优先**：步骤 6 检测到的断点强制切分，不受 Max-Min 规则影响；加入新片段会超过 `chunk_size` 时也直接切断

相比简单的"与组内均值比较"，Max-Min 能持续接受语义连贯的内容，一旦遇到偏离组内主题的内容就及时断开，天然产出边界清晰、主题一致的块。

#### Step 8: 组内兜底硬切与碎片清扫（`_hard_split` / `_merge_no_leftover` / `_eliminate_leftovers`）

Max-Min 分组后，组内内容仍可能超过 `chunk_size`（单个长片段或组边界处的累积）：`_group_segments_maxmin` 对超长组调用 `_hard_split` 按固定宽度切断。

所有组再统一进入 `_merge_no_leftover` 按原文顺序装箱：同类型片段能装就装进同一缓冲块，相邻且合并后不超过 `chunk_size` 的切片合并为一块；类型切换或超长时落盘；最后若末块偏短则并入前一块（允许略微超长），并用 `_eliminate_leftovers` 正向扫描彻底清除残留碎片。

**意义**：补齐两条兜底约束：**无过长段**——语义分组不保证长度收敛，必须硬切；**无碎片**——语义分组可能因断点密集产生大量单片段小块，必须合并。这一步让语义切片的最终输出同样满足"块长在目标区间内"的统一标准。

#### Step 9: 字符级重叠后处理（`_apply_overlap` / `_tail_with_boundary`）

若配置了 `chunk_overlap > 0`，对最终切片结果应用字符级滑动窗口重叠：取前一块末尾最多 `chunk_size` 字符，用 `_tail_with_boundary` 对齐到句子/段落边界作为当前块的前缀。叠加后若块长超过 `chunk_size + chunk_overlap`，从重叠部分头部截掉溢出量。重叠只在相邻文本块之间施加，JSON 块与类型切换处不施加。

**意义**：在相邻块的交界处制造内容重叠，使检索时能命中被切断在块尾的查询词或句子，缓解硬切边界处的信息丢失。JSON 块不参与 overlap，避免污染结构化数据。

## 4. 参数说明

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `similarity` | float (0–1) | 0.6 | Max-Min 的 tau（初始阈值），控制前两个片段合并的敏感度。也用于自动推导 `min_similarity` |
| `breakpoint_method` | string | `"percentile"` | 断点检测策略：`"percentile"` / `"stddev"` / `"none"` |
| `size` | int | 4000 | 目标切片字符长度（Unicode 字符数）；底层函数签名默认 500，API 层 `ChunkOptions.size` 默认 4000 |
| `chunk_overlap` | int | 0 | 相邻切片重叠字符数 |
| `min_segments_per_chunk` | int | 2 | 单片最小片段数保底（函数级参数，API 未暴露） |

**自动推导参数**：

- `min_similarity = max(0.3, similarity - 0.15)` — MaxSim 的下限。防止 MinSim 因 chunk 内部差异大而过低时，新片段仅凭低相似度就加入。

**threshold（tau）选参建议**：

| tau 值 | 行为 | 适用场景 |
| --- | --- | --- |
| 0.7 ~ 0.8 | 严格：对主题转换敏感 | 结构化术语库、FAQ，需逐条目分离 |
| **0.5 ~ 0.6** | **适中（默认 0.6）** | 通用中文文档 |
| 0.3 ~ 0.4 | 宽松：仅在大主题跳跃处切分 | 主题连贯的长文、技术手册 |

## 5. 与 centroid 方法的对比

### 5.1 算法对比

| 特性 | centroid 方法（旧） | Max-Min 方法（新） |
| --- | --- | --- |
| 合并判断 | `cos(新片段, centroid) >= 阈值` | `MaxSim >= MinSim AND MaxSim >= min_similarity` |
| 阈值类型 | 固定值 | 自适应（MinSim 动态变化） |
| centroid 漂移 | **存在** | **无**（pairwise 比较） |
| 弱语义边界 | 可能漏掉 | 正确识别 |
| 计算复杂度 | O(n) per segment | O(k²) per chunk（k 为 chunk 内片段数，通常 < 10） |

### 5.2 实际场景对比

**场景：多主题文档**（轨道交通 → 食品安全 → 金融风控）

```
旧 centroid (similarity=0.6):  4 chunks
  [0] 轨道交通（全部合并，包括子主题切换）
  [1] 食品安全
  [2] 金融风控 part 1
  [3] 金融风控 part 2

新 Max-Min (tau=0.6):          5 chunks
  [0] 轨道交通 part 1（基础设施描述）
  [1] 轨道交通 part 2（现代化挑战）← 新增，正确识别子主题切换
  [2] 食品安全
  [3] 金融风控 part 1
  [4] 金融风控 part 2
```

旧 centroid 方法将轨道交通 section 内所有内容合并为一个 chunk（144 chars），因为 centroid 逐渐漂移，"现代化挑战"子主题的向量与漂移后的 centroid 仍足够相似。

新 Max-Min 方法检测到 consecutive similarity 从 0.73 骤降到 0.53（paragraph gap），正确切分为两个独立 chunk。

**场景：结构化术语库**（39 个 `##` 术语条目）

两种方法产出相同的 40 个 chunks（每术语一个 chunk）。这是因为 Markdown 标题预拆分在此类文档中起主导作用——每个 `##` 创建独立 section，section 内内容远小于 `chunk_size`。

## 6. 性能特征

| 指标 | 数值 |
| --- | --- |
| Embedding 模型 | BAAI/bge-small-zh-v1.5（512 维）；remote-only 模式走远程 embedding 服务 |
| 模型内存占用 | ~100 MB（本地模式） |
| 100 片段编码耗时 | < 1s（Mac M 系列）/ 数秒（x86） |
| 批量编码策略 | 所有片段一次性编码，避免多次模型调用 |
| 分组计算复杂度 | O(n × k²)，k 为 chunk 内平均片段数（通常 3–10） |

断点检测 (`"percentile"` / `"stddev"`) 在 30+ 片段时额外耗时 < 5ms，可忽略不计。`"none"` 模式最快。模型不可用时整条语义路径回退为规则递归切分，无 embedding 开销。

## 7. 实现位置

| 文件 | 函数/类 | 作用 |
| --- | --- | --- |
| `algorithm/NL_to_cypher/splitter.py` | `split_semantically()` | 流水线编排（含 JSON 保护、模型回退） |
| 同上 | `_split_text_with_json_protection()` / `_extract_json_blocks()` | JSON 块识别与隔离（所有策略共享） |
| 同上 | `_split_json_by_structure()` / `_split_json_object()` / `_split_json_array()` / `_hard_split_json()` | 超长 JSON 结构切分与硬切（仅 `recursive` / `custom_recursive` 路径） |
| 同上 | `_split_by_markdown_headers()` | Markdown 标题预拆分 |
| 同上 | `_split_into_segments()` | 句子级片段拆分 |
| 同上 | `_merge_tiny_segments()` | 微小片段合并 |
| 同上 | `_detect_breakpoints()` | 语义断点检测 |
| 同上 | `_group_segments_maxmin()` | Max-Min 核心算法（组内超长经 `_hard_split()` 兜底） |
| 同上 | `_merge_no_leftover()` / `_eliminate_leftovers()` | 装箱合并与碎片清扫 |
| 同上 | `_apply_overlap()` / `_tail_with_boundary()` | 字符级重叠后处理（边界对齐） |
| `algorithm/NL_to_cypher/vector.py` | `_load_model()` | 模型加载与缓存（remote-only 走远程服务） |
| `algorithm/knowledge_pipeline/chunking.py` | `chunk_document()` | 策略路由 |
| `backend/app/schemas.py` | `ChunkOptions` | API 参数定义 |

## 8. 参考文献

- Bhat, S.R. et al. (2025). "Max–Min semantic chunking of documents for RAG application." *Discover Computing*, Vol 28. [DOI: 10.1007/s10791-025-09638-7](https://doi.org/10.1007/s10791-025-09638-7)
- Moreno-Cediel, A. et al. (2025). "Optimising retrieval performance in RAG systems: A new growing window semantic chunking strategy to address weak semantic boundaries." *Knowledge-Based Systems*, Vol 331. [DOI: 10.1016/j.knosys.2025.113359](https://doi.org/10.1016/j.knosys.2025.113359)
- Kamradt, G. (2024). "5 Levels of Text Chunking." GitHub: [FullStackRetrieval-com/RetrievalTutorials](https://github.com/FullStackRetrieval-com/RetrievalTutorials)
