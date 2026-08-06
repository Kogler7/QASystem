# 算法 API v1 迁移说明

> 并发执行架构在保留业务路径和业务 `data` 结构的同时，将生产默认执行方式改为异步。重任务通常返回 `202 + job_id`，调用方需通过 `/api/v1/jobs/{job_id}` 和 `/result` 获取最终结果；完整变化与部署步骤见[新架构完整镜像部署迁移指引](../docker/ENV_MIGRATION.md)。`Prefer: wait=N` 只提供最多 60 秒的有界等待，不应作为长任务的长期兼容方案。

本文面向正在调用历史算法 HTTP API 的开发、联调和测试团队，说明如何迁移到正式 `/api/v1` 接口。完整的新接口定义见[算法接口协议](算法接口协议.md)，错误处理见[错误码协议](错误码协议.md)。

## 1. 迁移结论

本次升级不只是路径变更，还调整了请求字段、响应结构和部分处理行为。调用方必须同步修改请求组装与响应解析代码。

特别注意：

- 历史路径中只有三个接口保留 deprecated alias，且仅兼容路径，不兼容旧请求字段或旧响应结构。
- deprecated alias 也必须使用 v1 请求格式，响应 `info` 会附带迁移提示。
- `/api/component/nl2cypher/text-to-vector` 已删除，没有 path alias。
- 新接口采用严格字段校验，传入旧字段或未定义字段会返回 HTTP `422`。
- 成功和失败响应统一包含 `code`、`info`、`data`、`error`，不再使用 `message`。

## 2. 路径迁移

| 历史路径 | v1 路径 | 历史路径状态 | 调用方动作 |
| --- | --- | --- | --- |
| `POST /api/knowledge/precipitation` | `POST /api/v1/pipelines/knowledge-ingestion` | 保留 deprecated alias | 改路径并重写请求、响应适配 |
| `POST /api/component/nl2cypher/text-to-vector` | `POST /api/v1/embeddings/texts` | 已删除 | 使用批量接口传一个 item |
| `POST /api/component/nl2cypher/text-to-vectors` | `POST /api/v1/embeddings/texts` | 保留 deprecated alias | 将 `texts` 改为 `items` |
| `POST /api/component/kg-update/calculate-confidence` | `POST /api/v1/graphs/confidence` | 保留 deprecated alias | 改路径并调整响应解析 |

新增的底层能力包括：

- `POST /api/v1/documents/parse`
- `POST /api/v1/documents/chunk`
- `POST /api/v1/graphs/extract`
- `POST /api/v1/graphs/merge`
- `POST /api/v1/embeddings/graphs`

调用方如果只需要完整知识沉淀流程，应继续使用聚合接口；只有需要自行编排流程时才调用底层接口。

## 3. 通用协议变化

### 3.1 统一响应

成功响应：

```json
{
  "code": 0,
  "info": "处理完成",
  "data": {},
  "error": null
}
```

失败响应：

```json
{
  "code": 4002,
  "info": "LLM 请求超时",
  "data": null,
  "error": {
    "type": "LLM_TIMEOUT",
    "stage": "knowledge_extract",
    "retryable": true,
    "details": {}
  }
}
```

调用方应同时检查 HTTP 状态和 `code`。程序分支使用 `code` 或 `error.type`，不要解析 `info` 中的自然语言，也不要再读取 `message`。

### 3.2 数组按顺序对应

v1 批量接口不再接收或返回业务无关的 `index`。输入、输出按数组顺序对应：

```text
request.items[i] ↔ response.data.items[i]
```

知识沉淀中还存在以下对应关系：

```text
data.extraction.ranges[i] ↔ data.artifacts.vector_ids[i]
```

当 `merge=false` 时：

```text
data.extraction.ranges[i] ↔ data.extraction.graphs[i] ↔ data.artifacts.vector_ids[i]
```

## 4. 知识沉淀接口迁移

### 4.1 请求格式

新接口仍使用 `multipart/form-data` 上传文件。旧文档中标注的 `application/json` 不适用于包含文件的请求。

历史字段改为一个严格的 `options` JSON 字符串：

| 历史字段 | v1 字段 | 说明 |
| --- | --- | --- |
| `file` | `file` | 保持不变，multipart 文件字段 |
| `ontology` | `ontology` | 保持为顶层 multipart 文本字段 |
| `lang` | `options.parser.mineru_options.language` | 默认 `ch` |
| `parser_engine` | `options.parser.engine` | 不再表示无条件强制；实际引擎以响应 `data.stats.document.parser` 为准 |
| `chunk_character` | `options.chunking.separators` | 统一为字符串数组，不再接受两种不同格式 |
| `chunk_size` | `options.chunking.size` | 默认 `500` |
| `chunk_type` | `options.chunking.strategy` | 从整数枚举改为字符串枚举 |
| `similarity` | `options.chunking.similarity` | 当前为预留参数，暂不影响四种现行策略 |
| `save_to_db` | `options.storage.persist_graph` | 控制是否写入服务端配置的 Neo4j |
| 无 | `options.extraction.model` | 可选择服务端允许的抽取模型 |
| 无 | `options.extraction.merge` | 默认 `true`，控制是否合并分片 graph |
| 无 | `options.storage.knowledge_space_id` | 透传给外部文件和向量服务 |

`chunk_type` 迁移关系：

| 旧值 | 旧名称 | v1 `strategy` | 迁移提示 |
| ---: | --- | --- | --- |
| `0` | 语义切分 | `recursive` | v1 默认策略是递归切分，不执行语义相似度切分 |
| `1` | 符号切分 | `symbols` | `separators` 使用字符串数组 |
| `2` | 递归切分 | `custom_recursive` | 使用调用方提供的 `separators` |
| `3` | Markdown 切分 | `markdown` | 按 Markdown 结构切分 |

如果旧调用方依赖 `chunk_type=0` 的语义相似度切片，不能机械替换参数，需要先验证新的切片结果。

旧请求示意：

```text
file=@demo.docx
ontology=@startuml ...
lang=ch
parser_engine=mineru
chunk_type=2
chunk_size=500
chunk_character=["。","\n"]
save_to_db=false
```

v1 请求：

```bash
curl -X POST 'http://127.0.0.1:8006/api/v1/pipelines/knowledge-ingestion' \
  -F 'file=@./demo.docx' \
  -F 'ontology=<./ontology.puml' \
  -F 'options={"parser":{"engine":"mineru","mineru_options":{"language":"ch"}},"chunking":{"strategy":"custom_recursive","size":500,"separators":["。","\n"]},"extraction":{"merge":true},"storage":{"persist_graph":false}}'
```

不要把 `options` 作为普通嵌套 multipart 对象发送；它必须序列化成合法 JSON 字符串。

### 4.2 解析器行为

旧字段 `parser_engine` 被描述为“强制使用指定引擎”。v1 默认会结合文件类型和内容特征选择首选解析器，显式请求的引擎也可能作为 fallback 候选；需要严格控制路线时，在 `options.parser` 中同时设置 `engine` 和 `force=true`。聚合接口调用方必须读取 `data.stats.document.parser` 中的实际状态：

```json
{
  "preferred_engine": "docling",
  "requested_engine": "mineru",
  "request_handling": "fallback_candidate",
  "final_engine": "mineru",
  "fallback": {
    "from_engine": "docling",
    "reason": "Docling 解析失败"
  }
}
```

不要根据请求的 `engine` 推断最终解析器，也不要在响应中查找 `mineru_options`；配置不重复回显。

### 4.3 响应字段

| 历史字段 | v1 字段 | 迁移方式 |
| --- | --- | --- |
| `data.name` | `data.name` | v1 明确取上传文件名去除最后扩展名 |
| `data.understanding` | `data.summary` | 修改字段名 |
| `data.origin_url` | `data.artifacts.markdown.url` | `artifacts` 可能为 `null`，同时可读取 `id/name/path` |
| `data.vector_url` | `data.artifacts.vector_ids` | 不再返回向量 Markdown 文件，改为外部向量存储 ID 数组 |
| `data.extract_url` | 无 | 不再生成抽取结果文件，直接读取 `data.extraction` |
| `data.graphs.graphs[].graph` | `data.extraction.graph` | `merge=true` 时返回一个去重后的 graph |
| `data.graphs.graphs[].graph` | `data.extraction.graphs[]` | `merge=false` 时返回顺序排列的分片 graphs |
| `graph.index` | 无 | 删除，使用数组顺序对应 |
| 无 | `data.extraction.ranges` | 每个切片在解析后 Markdown 中的字符范围 |
| 无 | `data.stats.document.parser` | 实际解析器选择和 fallback 状态 |
| 无 | `data.stats` | 文档、切片、抽取、调度和耗时统计 |
| 无 | 顶层 `error` | 成功为 `null`，失败时为结构化错误 |

`merge` 默认值为 `true`，这是与旧版分片 graph 包装结构的重要差异。如果调用方仍需要逐切片 graph，应显式设置：

```json
{"extraction": {"merge": false}}
```

### 4.4 文件与向量产物

v1 不提供本地文件保存或下载接口：

- 配置 `FILE_SERVICE_URL` 时，解析后的 Markdown 上传到文件服务，文本切片向量写入 `{FILE_SERVICE_URL}/third/vectors/store`。
- `data.artifacts.markdown` 返回文件服务的 `id`、`name`、`path`、`url`。
- `data.artifacts.vector_ids` 返回向量存储产生的 ID，顺序与 `ranges` 一致。
- 未配置 `FILE_SERVICE_URL` 时不上传 Markdown、不执行向量存储，`data.artifacts` 为 `null`。
- `_extract.json`、`_vectors.json`、向量 Markdown 等历史产物不再生成。

调用方必须对 `artifacts` 判空：

```python
artifacts = response["data"].get("artifacts")
markdown_url = artifacts["markdown"].get("url") if artifacts else None
vector_ids = artifacts.get("vector_ids", []) if artifacts else []
```

### 4.5 图谱读取示例

```python
data = response["data"]
extraction = data["extraction"]

if "graph" in extraction:
    # merge=true
    graphs = [extraction["graph"]]
else:
    # merge=false，与 ranges 按顺序对应
    graphs = extraction["graphs"]

ranges = extraction["ranges"]
```

## 5. 文本向量化接口迁移

单文本和多文本统一迁移到：

```http
POST /api/v1/embeddings/texts
Content-Type: application/json
```

旧单文本请求：

```json
{"text": "知识空间"}
```

v1 单文本请求：

```json
{"items": [{"text": "知识空间"}]}
```

旧多文本请求：

```json
{"texts": ["第一段", "第二段"]}
```

v1 多文本请求：

```json
{
  "items": [
    {"text": "第一段"},
    {"text": "第二段"}
  ]
}
```

v1 响应：

```json
{
  "code": 0,
  "info": "文本向量化完成，共 2 个向量",
  "data": {
    "count": 2,
    "dimension": 512,
    "items": [
      {"vector": [0.1, -0.2]},
      {"vector": [0.3, -0.4]}
    ]
  },
  "error": null
}
```

响应字段迁移：

| 历史字段 | v1 字段 | 说明 |
| --- | --- | --- |
| 单文本 `data.text` | 无 | 请求已经包含文本，响应不再重复返回 |
| 单文本 `data.vector` | `data.items[0].vector` | 单文本也使用一个 item 的批量协议 |
| 多文本 `data.vectors[i]` | `data.items[i].vector` | 严格按请求顺序对应 |
| `message` | `info` | v1 不返回 `message` |
| 无 | `data.dimension` | 当前向量维度 |

迁移要点：

- `text` 或 `texts` 改为 `items[].text`。
- `data.vector`、`data.vectors` 改为 `data.items[].vector`。
- 响应不重复返回原始文本，`data.items[i]` 与请求 `items[i]` 按顺序对应。
- 新增 `data.dimension`。
- 输出与输入按数组顺序对应，不使用 `index`。
- 空字符串仍保留，不会被静默过滤。

## 6. 置信度接口迁移

请求中的 `graph` 结构保持不变，正式路径改为：

```http
POST /api/v1/graphs/confidence
```

响应不再重复返回整个 graph，也不再把置信度写入 `relationships[].properties`。v1 分别返回两个顺序对齐的评分数组：

```json
{
  "code": 0,
  "info": "置信度评估完成",
  "data": {
    "evaluation_id": "EVAL-20260715143000123456",
    "total_count": 1,
    "relationships": [
      {
        "text_score": 0.8,
        "path_score": 0.77,
        "text_weight": 0.7,
        "path_weight": 0.3,
        "confidence": 0.791
      }
    ],
    "ontology_relations": []
  },
  "error": null
}
```

对应关系：

```text
request.graph.relationships[i] ↔ response.data.relationships[i]
request.graph.ontology_relations[i] ↔ response.data.ontology_relations[i]
```

如果调用方仍需要带置信度的 graph，应在客户端显式合并：

```python
graph = request_body["graph"]
scores = response["data"]

for relation, score in zip(graph.get("relationships", []), scores["relationships"]):
    relation.setdefault("properties", {})["confidence"] = score["confidence"]

for relation, score in zip(graph.get("ontology_relations", []), scores["ontology_relations"]):
    relation.setdefault("properties", {})["confidence"] = score["confidence"]
```

## 7. 错误处理迁移

旧调用方如果只判断 `code == 0`，至少应补充以下处理：

1. 检查 HTTP 状态，`4xx` 通常表示请求需要修改，`5xx`/`502`/`503`/`504` 通常表示服务或依赖异常。
2. 使用 `error.type` 区分稳定错误类型。
3. 仅当 `error.retryable=true` 时执行有上限的退避重试。
4. 聚合接口失败时，`data` 可能包含已经生成的部分产物；整个请求仍应视为失败。
5. 不要根据 `info` 或 `error.details` 中的自然语言做程序分支。

## 8. 推荐迁移步骤

1. 将服务地址从历史路径切换到对应 `/api/v1` 路径。
2. 知识沉淀请求改为 `file + ontology + options JSON string`。
3. 将整数 `chunk_type` 转换为字符串 `chunking.strategy`，重点回归切片数量和边界。
4. 将 `understanding/origin_url/vector_url/extract_url/graphs` 的解析逻辑改为 `summary/artifacts/extraction`。
5. 根据业务选择 `extraction.merge`，不要依赖默认值猜测响应是 `graph` 还是 `graphs`。
6. 向量化统一改为 `items` 批量协议；单文本也包装为一个 item。
7. 置信度调用方按顺序读取评分，并在需要时自行写回 graph。
8. 接入统一错误结构和 `retryable` 重试规则。
9. 在测试环境覆盖：文件服务已配置/未配置、`merge=true/false`、解析器 fallback、空文本向量、部分抽取失败。
10. 完成迁移后停止调用 deprecated alias，避免后续移除历史路径时再次改造。

## 9. 联调检查清单

- [ ] 请求的 `Content-Type` 与接口一致。
- [ ] multipart 中的 `options` 已进行一次 JSON 序列化。
- [ ] 请求未携带旧字段或其他未定义字段。
- [ ] 代码同时判断 HTTP 状态与响应 `code`。
- [ ] 代码不再读取 `message`。
- [ ] `artifacts` 为 `null` 时不会发生空指针错误。
- [ ] `merge=true` 读取 `extraction.graph`，`merge=false` 读取 `extraction.graphs`。
- [ ] 使用数组顺序建立切片、graph、vector ID 和置信度的对应关系。
- [ ] 已验证切片结果，特别是原 `chunk_type=0` 的调用场景。
- [ ] 已验证解析器 fallback 时读取 `data.stats.document.parser.final_engine`。
- [ ] 已按 `error.retryable` 设置重试策略和最大重试次数。
