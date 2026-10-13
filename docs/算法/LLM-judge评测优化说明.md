# LLM-judge 评测逻辑与配置

默认 `--match-mode llm`，使用 API judge。抽取模型与 judge 独立选择；每次评测的 judge 参数来自该轮命令行，不再读持久化评测 YAML 或应用中的 judge 默认参数。

## 实际评分过程

1. 对预测和 golden 做类型、名称及属性检查，生成满足约束的确定性自动候选边。
2. 剩余候选按 golden × predicted 双侧窗口扫描。默认窗口为 40 × 20；每次模型返回窗口内匹配索引，不直接返回全局 F1。
3. 检查响应字段、索引范围、类型约束及问题覆盖，合并有效候选边。
4. 对全局候选边做一对一最大匹配，计算 TP、FP、FN，再分别计算实体和关系的 P/R/F1。

`llm` 模式也包含受约束的自动候选优化，已能确定匹配的窗口可能不调用模型；不能仅按 case 或实体数量估算调用次数。`identity` 是另一种明确指定的确定性评测模式，不能当作正式 API judge 的等价对照。

关系评分需要端点上下文。失败、截断、非法索引或缺失响应不能当成成功的空匹配；API 与本地模型对比必须走同一套候选、提示词、窗口和全局匹配逻辑。

## 本轮命令行

```sh
python3 eval/run.py --match-mode llm --fail-on-empty --min-f1 0.5

python3 eval/run.py --match-mode llm   --judge-base-url http://127.0.0.1:9998/v1   --judge-model Qwen/Qwen3.5-4B   --judge-adapter chat --judge-max-tokens 8192
```

命令从宿主机运行时使用模型宿主端口；容器内用相应 Compose 服务名和内部端口。服务模型名、可用地址、密钥与连接位置以实际部署为准；上述地址仅演示配置方法。

judge 地址、模型、adapter、输出上限、超时和并发只用于本轮。密钥由 `--judge-key-env` 指定变量名，默认可使用现有 `QASYSTEM_JUDGE_API_KEY`；官方默认 DeepSeek 可使用 `DEEPSEEK_API_KEY`。不要把密钥写在命令、报告、数据集或请求产物中。

旧 `QASYSTEM_JUDGE_BASE_URL/MODEL/ADAPTER/MAX_TOKENS` 已删除，不再覆盖本轮参数。完整参数以 `python3 eval/run.py --help` 为准，常用步骤见 [eval README](../../eval/README.md)。

## 模型协议与可靠性

抽取使用 JSONL；judge 仍使用单个 JSON 对象，实体任务返回 `entity_matches`，关系任务返回 `relation_matches`。共享底层调用代码不意味着两者输出协议相同。

chat 的 `auto` 模式按服务能力选择格式；官方 DeepSeek 优先 `json_object`，其他兼容服务优先 `json_schema`。只有明确的格式不支持错误才切换，不因超时、鉴权失败或非法成功响应而降级。SystemOne 适配器供 Clef/NanoJev 使用，其问题覆盖和分窗要求单独校验。

公共调用层只对明确不支持 thinking 参数的错误做一次兼容重试。官方 DeepSeek、SiliconFlow 和本地服务的参数选择不同，不能仅按模型名称猜测供应商。

本地小模型不能只看速度或单对题准确率。真实多候选窗口还需检查索引合法性、漏匹配、顺序敏感、同输入漂移和失败率。当前四模型实验见 [LLM 多候选 judge 对比](LLM多候选Judge对比20261012.md)；其代理复核标签并非独立人工真值，不可用实验窗口的数字替代正式全量抽取 F1。

## 代码与产物

| 位置 | 作用 |
| --- | --- |
| `eval/run.py` | 提示词、候选窗口、类型约束、最大匹配、指标与门禁 |
| `eval/judge_client.py` | HTTP/SystemOne、严格响应校验、脱敏诊断 |
| `external_services/llm/` | 共享请求构造、JSONL 工具及进程内配额机制 |
| `eval/rejudge_run.py` | 对已有预测重新 judge，避免再次抽取 |
| `eval/tools/judge_window_compare.py` | 同输入重复/反序的探索实验 |
| `test_data/judge_windows/v1/` | 冻结多候选实验输入与复核标签 |
| `tmp/eval/` | 评测输出、参数快照和实验报告；不作为下次默认配置 |

裁判使用本轮窗口并发，不自动接入 Backend 任务队列，也不与抽取进程共享跨进程账号限流。详情见 [LLM 公共调用层](LLM公共调用层.md)。部署配置见 [配置体系说明](../部署/配置体系说明.md)。
