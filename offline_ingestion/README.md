# 知识沉淀 HTTP 编排脚本

本目录只编排、只调已有 Backend 接口、只落盘响应。不含解析/切片/抽取实现，不 `import algorithm` / `backend`。

部署机上 Backend 已在跑时，拷走本目录即可跑。默认地址 `http://127.0.0.1:8006`。

## 1. 目录

```text
offline_ingestion/
  run.py / run.sh / params.yaml / README.md
  公文/
    docs/        # 原文件，可任意增删
    ontology/    # 该组权威本体（后端请求必填字段 ontology）
  标准/
    docs/
    ontology/
```

任意含 `docs/` 的子目录视为一组（忽略 `_` / `.` 开头）。本体默认读 `<组>/ontology`；也可用 `params.yaml` 的 `groups.<组名>.ontology` 覆盖。没有本体则跳过该组。

当前 `ontology/` 中的 PlantUML 来自仓库验证数据集，**作为该组请求发给后端的权威 schema**。后端不会自己读这些文件，必须由调用方放进 `ontology` 字段。可替换、可改路径。

## 2. 参数从哪里来

| 用到的值                  | 来源                                                                        | 说明                                                                                                              |
| ------------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Backend 地址              | 环境变量`QASYSTEM_BACKEND_URL`，否则 `params.yaml` 的 `backend_url`   | 默认`http://127.0.0.1:8006`（容器内端口是 8000）                                                                |
| 原文件                    | `<组>/docs/`                                                              | 对应接口字段`file`；每文件单独一次请求                                                                          |
| 本体                      | `<组>/ontology/` 或 `groups.<组名>.ontology`                            | 对应接口字段`ontology`（PlantUML 文本）。目录内全部 `.puml`/`.uml` 按文件名排序后拼成 **一个** 字符串 |
| 流水线配置                | `params.yaml` 的 `options`                                              | 对应接口字段`options`（JSON 字符串），结构即生产 `PipelineOptions`                                            |
| LLM 地址 / Key / 默认模型 | **服务端** `.env`：`KG_EXTRACT_LLM_*`                             | 脚本传不了`base_url`。`options.extraction.model` 只能覆盖模型名                                               |
| 向量/文件服务             | **服务端** `APPLICATION_SERVICE_URL`（`FILE_SERVICE_URL` 仅回退） | 配了才会上传 Markdown、写向量；脚本只传`storage.knowledge_space_id`                                             |
| 鉴权                      | 环境变量`AUTHORIZATION`                                                   | 只放 HTTP 头。无空格时脚本会加`Bearer ` 前缀。不写入 payload、日志、落盘 JSON                                   |
| 任务轮询                  | `poll_interval_seconds` / `poll_timeout_seconds`                        | **仅编排层**，后端没有这两个字段                                                                            |

`options` 缺项时 `run.py` 会按与 [`backend/app/schemas.py`](../backend/app/schemas.py) 相同的默认值补全后再发给后端。协议文档里的 `chunking.size: 500` 是示例，代码默认是 `4000`。

## 3. 脚本实际调用的接口

每个原文件走一遍下面这条链。不调用 parse / chunk / extract 分步接口。

### 3.1 `POST /api/v1/pipelines/knowledge-ingestion`

后端顺序：解析 Markdown → 切片 → 分片抽取 →（可选）向量存储。`persist_graph=true` 时后端直接返回未开通，不会写库。

**脚本发出的输入（multipart）**

| 字段         | 必填 | 本脚本填什么                                          |
| ------------ | :--: | ----------------------------------------------------- |
| `file`     |  是  | `<组>/docs/` 里的一个原文件（不用 `files=` 批量） |
| `ontology` |  是  | 该组权威本体全文                                      |
| `options`  |  否  | `params.yaml` 的 `options` JSON                   |

`options` 完整结构：

```json
{
  "parser": {
    "engine": "auto",
    "force": false,
    "page_sharding": "auto",
    "page_shard_size": 0,
    "max_page_shards": 16,
    "mineru_options": {
      "language": "ch",
      "ocr_mode": "auto",
      "table_enable": true,
      "formula_enable": true
    }
  },
  "chunking": {
    "strategy": "recursive",
    "size": 4000,
    "separators": null,
    "similarity": 0.6,
    "chunk_overlap": 0,
    "size_unit": "char",
    "breakpoint_method": "percentile",
    "chunk_threshold": 12000
  },
  "extraction": { "model": null, "merge": true },
  "storage": { "persist_graph": false, "knowledge_space_id": null }
}
```

默认不带 `Prefer`（同步等结果）。大文件可能仍返回 `202`。

**后端输出（成功 envelope）**

| 路径                       | 含义                                            | 脚本是否落盘                       |
| -------------------------- | ----------------------------------------------- | ---------------------------------- |
| `data.name`              | 文件名去扩展名                                  | 在`response.json` 里             |
| `data.summary`           | 抽取摘要句                                      | 同上                               |
| `data.artifacts`         | 配了文件服务才有 Markdown/向量 id，否则`null` | 同上；**没有 Markdown 正文** |
| `data.extraction.ranges` | 各切片在 Markdown 中的字符区间                  | `chunks/<stem>/ranges.json`      |
| `data.extraction.graph`  | `merge=true` 时的合并图                       | `extract/<stem>/graph.json`      |
| `data.extraction.graphs` | `merge=false` 时的分片图                      | 写入`graph.json` 的 `graphs`   |
| `data.stats.document`    | 解析元数据（无 markdown 字段）                  | `parse/<stem>/parser.json`       |
| `data.stats.chunking`    | 切片统计（无切片正文）                          | `ranges.json` 内                 |
| `data.stats.extraction`  | 抽取统计 / attempts / scheduler                 | 仅`response.json`                |
| `execution`              | 队列任务信息                                    | 仅`response.json`                |

完整响应另存 `extract/<stem>/response.json`。失败另存 `error.json`。

### 3.2 仅当沉淀接口返回 202 时

| 方法 | 路径                             | 输入                                                                      | 输出                                                                                   |
| ---- | -------------------------------- | ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| GET  | `/api/v1/jobs/{job_id}`        | 路径参数`job_id`（来自 202 的 `data.job_id` 或 `execution.job_id`） | `data.status`：`queued` / `running` / `succeeded` / `failed` / `cancelled` |
| GET  | `/api/v1/jobs/{job_id}/result` | 同上                                                                      | 成功后返回与 3.1 相同的业务 envelope；未完成时 HTTP 409                                |

轮询间隔/超时见第 2 节。

## 4. 后端内部会用到、但本脚本不直接调用的接口

沉淀接口复用这些能力。下面便于对照「若改成三步调用能多拿到什么」。

### 4.1 `POST /api/v1/documents/parse`

输入：`file` + `options`（即沉淀里的 `options.parser`）。
输出：`data.markdown`、`data.parser`、`data.content_length`。
沉淀成功响应 **不包含** `markdown` 正文。

### 4.2 `POST /api/v1/documents/chunk`

输入：`markdown` + `options`（即沉淀里的 `options.chunking`）。
输出：`data.items[]`，每项 `text` + `range`。
沉淀成功响应只有 `ranges`，**没有** `text`。

### 4.3 `POST /api/v1/graphs/extract`

输入：`ontology` + `items[].text` + `options`（`model` / `merge`）。
输出：`data.graph` 或 `data.graphs`。
与沉淀的 `data.extraction` 规则相同。LLM Base URL 不能由请求覆盖。

## 5. 怎么跑

```bash
chmod +x offline_ingestion/run.sh
# 把原文件放进 公文/docs 或 标准/docs
./offline_ingestion/run.sh
./offline_ingestion/run.sh --groups 公文
```

## 6. 与部署业务仍不一致、需要确认的点

脚本没有再实现算法，但编排选择和后端「一次请求」的语义仍有差别：

1. **每文件一次 `file=`，不用 `files=`**后端批量会把多文件 Markdown 拼成一份再抽一张图。脚本是一文件一张图。是否要改成按组 `files=` 一次提交？
2. **一组多个 `.puml` 会拼成一个 `ontology` 字符串**后端只收一个字符串，不会自己读目录。标准组当前三份模块本体是拼起来上传的。是否应改为每组只放一个权威文件？
3. **空本体**后端允许 `ontology=""` 走非结构化摘要。脚本把本体当必填，缺文件就跳过该组。是否保持必填？
4. **本地没有 Markdown / 切片正文**这是聚合接口的响应边界，不是脚本丢了。若验收需要这两类中间产物，要改调 4.1 / 4.2，或改后端沉淀响应。
5. **向量化和 Markdown 上传**由服务器是否配置 `APPLICATION_SERVICE_URL` 决定，脚本不能关，只能把 `persist_graph` 保持 `false`。部署环境若配了文件服务，跑脚本也会走存储。是否接受？
6. **`AUTHORIZATION` 自动加 `Bearer `**
   若网关要的是别的头格式，需要改脚本或改为原样转发。

协议原文：[`docs/算法接口协议.md`](../docs/算法接口协议.md)。
