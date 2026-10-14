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

上述四类 HTTP 入口记录上传/参数/响应；所有由 `run_operation` 执行的队列任务另外记录执行侧产物、结果与日志（包括 embeddings / merge / MCP 操作）。健康检查、状态查询和其他未挂入口钩子的 HTTP 接口不保存完整请求。

## 配置项（根 `.env.example`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `API_RECORD_ENABLED` | `1` | `0/false/no/off` 关闭，关闭后不产生任何记录 |
| `API_RECORD_RETENTION_DAYS` | `7` | 保留天数；`<=0` 表示不清理；非法值告警后按 7 |

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
├── model_calls/<trace_id>/     # 原始模型输入、输出、结束原因；manifest关联request_id
├── worker_result.json          # 仅worker记录目录：任务最终结果
├── response.json               # 最终响应 payload
├── timing.json                 # total_seconds、status、stages[{stage, duration_seconds}]
└── run.log                     # 请求期间的详细日志（首部注明覆盖范围）
```

## 清理机制

- 触发时机：Backend 启动（lifespan）时一次 + 每次新建记录目录前一次。
- 只删除记录根目录下匹配 `^\d{8}-\d{6}_` 命名的目录；目录时间优先取目录名时间戳，解析失败回退 mtime；其他文件/目录一律不碰。
- 清理与记录全程 try/except 包裹：任何记录/清理失败只告警，绝不影响 API 响应。

## 边界与限制

- **异步队列模式**：HTTP 入口保留网关记录；`run_operation` 在 worker 中创建独立目录，名字追加 `_worker_<随机编号>`，避免多个子任务和重试同时写同一目录。入队时将非敏感 request_id 随 payload 传递；worker 的 `params.json` 同时提供原始 request_id 与 job/task/attempt 标识，模型调用保存在该目录的 `model_calls/<trace_id>`。旧任务没有 request_id 时使用 job_id。HTTP和worker目录不能混为一份完整端到端耗时，需要按ID关联。
- **run.log 覆盖范围**：每个实际记录进程挂 root 与当时已存在的 `propagate=False` logger，按 request_id contextvar 过滤；请求中途新建的此类 logger 仍不覆盖。记录失败只告警，无法写入时模型调用也不另写独立路径。
- **鉴权内容**：不采集请求头；结构化 Authorization、密钥、密码、headers 字段和 Bearer 文本脱敏。模型trace另按当前供应商密钥脱敏。上传原文、本体、普通文本仍是业务数据，不宣称自动清除其中所有敏感内容。
- **迁移**：旧 `KG_API_RECORD_ENABLED`、`KG_API_RECORD_RETENTION_DAYS` 只在 ops UI 展示迁移；新旧冲突要人工选择，运行时不读取旧名。旧目录和 `KG_EXTRACT_TRACE_DIR` 不继续使用；旧文件不自动搬迁或删除。
- **测试环境**：默认开启时跑测试也会产生记录（已 gitignore、到期自清）；不希望产生可设 `API_RECORD_ENABLED=0`。

## 排障用法

线上用户反馈异常时：取响应/告警中的 `request_id` → 在固定 `backend/data/api_records`（容器 `/app/backend/data/api_records`）下按 request_id 查找网关和worker目录 → 看 `timing.json` 定位慢阶段、`run.log` 定位报错、`stage_*` 对比中间产物与预期。
