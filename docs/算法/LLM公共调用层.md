# LLM 公共调用层

## 职责与目录

抽取和评测复用底层调用逻辑，评测脚本仍可以在 Backend 容器未启动时对已有抽取结果重新评分。线上解析、切片和抽取评测仍需对应的 Backend 接口。

| 文件 | 职责 |
| --- | --- |
| `external_services/llm/chat.py` | 构造聊天请求、关闭思考参数、明确不支持思考参数时的兼容重试；通过注入的 SDK/HTTP 函数发送 |
| `external_services/llm/jsonl.py` | 抽取侧 JSONL 提示约束、解码和完整结果读取 |
| `external_services/llm/runtime.py` | 进程内共享配额池、等待期限、HTTP 期限、输入预算和可选抽取跟踪 |
| `algorithm/knowledge_extract/request_runtime.py` | 抽取配置、算法版本选择、密钥读取及跟踪接入；重新导出原调度接口 |
| `algorithm/knowledge_extract/*/llm_client.py` | 各版本响应封装和输出协议；聊天请求调用公共函数 |
| `eval/judge_client.py` | 裁判 HTTP 和 SystemOne 适配、格式兼容、严格响应校验及安全诊断 |
| `eval/run.py` | 裁判提示词、候选生成、窗口遍历、类型过滤、最大匹配、指标与质量门禁；保留 `JudgeClient` 调用入口 |

公共层不读取或新增部署变量，不连接 Backend、Redis，也不加载模型或 OpenAI SDK。调用方继续使用现有配置入口；SDK 由各抽取版本的工厂延迟创建。密钥只保留在客户端中，配额池不存储请求载荷或认证头。

## 输出协议边界

本次整理没有把裁判改成 JSONL。抽取侧原有 JSONL 实现移到公共目录，原 `algorithm.knowledge_extract.jsonl_protocol` 路径保留兼容导入。每个算法版本继续使用自己的原有输出协议。

裁判仍要求单个 JSON 对象：实体评分只允许 `entity_matches`，关系评分只允许 `relation_matches`，每条匹配只能包含非负整数 `golden_index` 和 `predicted_index`。窗口索引范围、类型过滤和一对一最大匹配仍由原评测代码校验和计算。错误响应不能作为空匹配成功评分。

裁判 `auto` 继续按原规则选格式：官方 DeepSeek 优先 `json_object`，其他聊天兼容服务优先 `json_schema`；只有明确的 HTTP 400/422 响应格式能力拒绝才换格式。HTTP 200 后的字段错误、截断、鉴权失败、限流、超时或服务器错误均不触发格式切换。SystemOne 的问题覆盖、输入大小错误及分窗逻辑保持原样。

## 本地模型与兼容处理

本地 vLLM 和中转代理继续使用配置中给定的完整聊天 base URL，包括 `/v1` 或代理路径；已经以 `/chat/completions` 结尾的地址不重复追加。不会仅凭模型名包含 DeepSeek 就向本地服务加入供应商专用参数。

裁判沿用原有思考参数选择：官方 DeepSeek 使用 `thinking`，SiliconFlow 使用 `enable_thinking`，其他地址默认不发送专用开关。抽取仍由其客户端和供应商配置控制参数。公共函数只在明确拒绝该思考参数时移除开关重试一次，保留原响应格式和提示词；401/403、429、5xx 和一般 schema 错误不因此重试。

## 并发边界

抽取使用公共 `ScheduledClient`，每次实际请求（包括兼容重试）进入配额池，等待占用请求预算；同一个 `quota_group` 在同一进程内共享并发、QPS/QPM 计数。原算法级调度仍保留。

裁判保留原评测窗口并发控制，不接入 Backend 执行队列，也不自动绑定抽取账号的配额组。两者复用了请求构造和兼容代码，不意味着独立进程间已共享账号配额。跨进程限流或 Backend 中转属于后续独立改动。

## 验证

公共请求测试覆盖思考参数、限定的兼容重试、配额计数和超时；裁判回归覆盖本地/代理地址、结构化格式回退、严格 JSON 校验、安全诊断和评分逻辑；各抽取版本兼容测试验证原输出协议。测试使用本地 fixture，不访问生产模型服务。
