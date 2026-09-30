# Backend 请求记录说明

Backend 对外 HTTP API 支持**逐请求完整落盘记录**：普通 API 测试/联调时，除控制台日志外，自动把每次请求的上传文件、本体与参数、中间产物、各阶段耗时和详细日志保存到独立文件夹，并定期清理过期记录。用于联调复盘与线上"凭 request_id 定位问题"。

实现位置：`backend/app/api_recording.py`（记录核心）+ 各路由/服务层钩子；阶段耗时复用 `backend/app/observability.py` 的 `record_stage_duration` 监听器。

## 覆盖接口

| 接口 | 记录内容 |
| --- | --- |
| `POST /api/v1/pipelines/knowledge-ingestion`（含 deprecated 别名） | uploads、ontology、params、parse/chunk/extract 中间产物、分阶段耗时、response、run.log |
| `POST /api/v1/documents/parse` | uploads、params、解析 markdown、耗时、response、run.log |
| `POST /api/v1/documents/chunk` | params、切片结果、耗时、response、run.log |
| `POST /api/v1/graphs/extract` | ontology、params、抽取结果、耗时、response、run.log |

embeddings / merge 接口不记录。

## 配置项（根 `.env.example`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `KG_API_RECORD_ENABLED` | `1` | `0/false/no/off` 关闭，关闭后不产生任何记录 |
| `KG_API_RECORD_DIR` | `backend/data/api_records` | 记录根目录；默认在可写数据卷上（源码目录生产环境为只读挂载，不可用作记录目录） |
| `KG_API_RECORD_RETENTION_DAYS` | `7` | 保留天数；`<=0` 表示不清理；非法值告警后按 7 |

## 记录目录结构

每次请求一个目录，命名为 `<YYYYMMDD-HHMMSS>_<request_id>/`：

```
20260930-154528_b31a8c746a494f9c9028fe9e8dbf46e9/
├── uploads/<原始文件名>        # 实际上传的文档原文（分块写盘，重名追加序号）
├── params.json                 # method/endpoint/request_id/started_at + options 等参数（bytes 只记 <bytes:N> 占位）
├── ontology.txt                # 本体原文
├── stage_parse/<文件>.md       # 解析产物 markdown（批量时每文件一份）
├── stage_chunk/chunks.json     # 切片结果 {count, items}
├── stage_extract/result.json   # 抽取聚合结果
├── response.json               # 最终响应 payload
├── timing.json                 # total_seconds、status、stages[{stage, duration_seconds}]
└── run.log                     # 请求期间的详细日志（首部注明覆盖范围）
```

## 清理机制

- 触发时机：Backend 启动（lifespan）时一次 + 每次新建记录目录前一次。
- 只删除记录根目录下匹配 `^\d{8}-\d{6}_` 命名的目录；目录时间优先取目录名时间戳，解析失败回退 mtime；其他文件/目录一律不碰。
- 清理与记录全程 try/except 包裹：任何记录/清理失败只告警，绝不影响 API 响应。

## 边界与限制

- **异步队列模式**：执行层 worker 为独立进程，contextvars 不到达，`stage_parse/`、`stage_chunk/` 与分阶段耗时在记录目录中缺失；网关侧的 uploads/params/ontology/response/总耗时照常记录，`stage_extract/result.json` 从响应数据写入、两种模式都有。worker 侧中间产物可由服务层既有快照目录 `backend/data/extraction_snapshots/` 补充。
- **run.log 覆盖范围**：直挂 root 与挂载时已存在的 `propagate=False` logger，按 request_id contextvar 过滤；异步 worker 进程日志与请求中途新建的此类 logger 收不到。定位为"尽力而为"，结构化产物（stage_*）才是主要事实来源。
- **安全红线**：只写显式传入的参数与产物，从不记录请求头；Authorization 等鉴权内容不可能进入记录文件；二进制内容只记长度占位。
- **测试环境**：默认开启时跑测试也会产生记录（已 gitignore、到期自清）；不希望产生可设 `KG_API_RECORD_ENABLED=0`。

## 排障用法

线上用户反馈异常时：取响应/告警中的 `request_id` → 在 `KG_API_RECORD_DIR` 下按 `_<request_id>` 后缀找目录 → 看 `timing.json` 定位慢阶段、`run.log` 定位报错、`stage_*` 对比中间产物与预期。
