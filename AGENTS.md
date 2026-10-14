# QASystem 仓库协作指南

本文件适用于整个仓库，供编码 Agent 和开发人员执行修改、测试、合并与交付时使用。QASystem 是知识图谱驱动的问答与求解系统，包含文档沉淀流水线、算法 HTTP/MCP 服务和 Agent 运行时。

修改前先读相关模块 README，再核对实际入口、配置和契约测试。当前行为以代码为准；本文中的交付、鉴权、目录和质量门禁是必须保留的约束。历史文档或现存脚本与约束不一致时，应明确差异，不能据此放宽约束。

## 1. 模块职责与入口

| 路径 | 职责与主要入口 |
| --- | --- |
| `backend/` | FastAPI HTTP 服务；`app/main.py` 装配路由，`app/schemas.py` 定义请求，`app/services/` 编排业务，`app/execution/` 管理队列、Worker、资源调度和事件；容器入口为 `python -m app.execution.supervisor` |
| `algorithm/` | 解析、切片、抽取、向量化、检索、SQL 与图表等算法和业务实现；HTTP/MCP 层不重复实现算法 |
| `external_services/` | 应用团队接口适配、能力注册和鉴权透传；统一入口为 `application.py` |
| `mcp_services/` | Backend 挂载在 `/mcp` 的 `qasystem-tools`，服务既有算法调用链路 |
| `agent/` | `agentcode` 运行时与异步任务 API：LLM/工具循环、Skill、预算、取消、SSE、任务存储和问题分解；入口 `python -m agentcode.api` |
| `agent_mcp/` | Agent 专属 `agent-tools`，提供图谱、知识空间、SQL 和图表工具；入口 `python -m agent_mcp`，与 Backend MCP 分开运行 |
| `clef_service/`、`nanojev_service/` | 可选常驻决策模型服务，通过主 Compose 的 `decisions` profile 启用 |
| `offline_ingestion/` | 调用现有 Backend 的批量沉淀 HTTP 编排，不另实现算法 |
| `eval/`、`test_data/`、`tests/` | 评测程序、数据及回归测试，不是运行时交付源码 |
| `docker/`、`ofd_converter/` | 环境校验、模型与离线验收辅助工具，以及 OFD 转换依赖 |
| `docs/` | 架构、接口、算法、Agent 和部署文档，入口为 `docs/文档索引.md` |

主要处理链路：

- **文档沉淀**：`preproc` 解析 → `knowledge_pipeline` 切片 → `knowledge_extract` 按本体抽取 → 向量化及已开通的应用服务存储。正式入口是 `/api/v1/pipelines/knowledge-ingestion`；图谱写入尚未开通，不能把抽取成功等同于已写入图谱。
- **问答求解**：Agent 调用专属 MCP，定位知识空间、检索图谱，再按图谱中的方案查询数据或生成图表。业务知识、字段和口径来自图谱，不在代码中硬编码，也不使用模型记忆填补缺失依据。
- **异步执行**：Backend 按资源类分队列执行；Agent 有独立的任务队列和 Worker。修改时分别核对两套任务、事件、取消和鉴权契约。

新增工具应进入对应的既有 MCP 服务，不在算法目录中另建 MCP Server。HTTP 与 MCP 错误码是不同契约，不能只按数值互相解释或转换。

## 2. 算法版本与接口契约

抽取版本以 `algorithm/knowledge_extract/version.py`、Backend 的 `schemas.py` 和 `services/knowledge_extract_adapter.py` 为准。

| 配置值 | `knowledge_extract/` 下的包 | 约定 |
| --- | --- | --- |
| `latest` | `latest` | 默认版本，直接运行独立实体目录 |
| `v0` | `v0` | 实体与属性抽取，不抽取关系 |
| `v2` | `v2` | V2 抽取流水线 |
| `v2.1` | `v2_1` | V2 流水线及规则文档路由扩展 |
| `v2.2` | `v2_2` | V2.2 抽取流水线及可选 Clef 预判 |
| `v3` | `v3` | V3 抽取流水线 |
| `v3.1` | `v3_1` | 实体与属性抽取、独立属性证据核验及定向修复；关系固定为空 |
| `jev` | `jev_extract` | 显式环境配置启用的实验副本，不是默认版本 |

- `latest` 不得改为软链或映射到其他版本。修改 `v3` 不会自动影响 `latest`；同步时须核对包引用、公开导出、测试和同步说明。
- `v1/` 保留历史实现与摘要等兼容能力，不是当前版本选择器支持的值。`v3_2`、`v3_3` 和旧 `v3_1` 实验实现已下线，不得根据历史文档恢复入口。
- 请求级版本覆盖与环境变量支持范围不同：当前 HTTP schema 不接受 `jev`。新增版本必须同时检查选择器、schema、适配器、配置覆盖、测试及接口文档。
- 模型和供应商配置集中管理；请求覆盖只使用服务端登记的供应商。不得将 API Key 写入供应商 JSON、评测参数或产物。
- 修改解析/切片时保留 `__source_range`、`range`、`metadata` 等下游契约。改变字段含义必须同步更新所有消费者，包括 `algorithm/knowledge_pipeline/chunking.py` 与评测复用路径。
- 本体由调用方提供，不得悄悄换用内置本体或 golden。验证、覆盖缺口、partial、隔离图及可发布状态必须如实保留，不能用非空图冒充质量通过。

## 3. 外部服务、鉴权与数据访问

- 应用团队服务统一使用 `APPLICATION_SERVICE_URL`；`FILE_SERVICE_URL` 已退役，不得在运行时读取，旧部署经配置 UI 迁移。
- 外部应用端点必须在 `external_services/application.py` 注册，不在算法或工具中散落第三方路径。
- 需要鉴权的外部调用必须保留队列执行。队列记录仅携带 `auth_context_id`，由框架存取鉴权上下文。
- Authorization 和其他鉴权头不得进入任务 payload、LLM 上下文、提示词、产物、日志、SSE、`execution` 或结果；评测 manifest 和请求记录同样不得保存凭据。
- Agent 的任务查询、流订阅和取消须保留任务归属校验与用户隔离。不得为调试绕过鉴权或让并行子任务共享错误的用户上下文。
- SQL 可保留配置允许的直接连接兼容；不得恢复 Neo4j 或 Elasticsearch 直连。
- 未开通的图谱写入、置信度、文档证据能力必须在调用时明确失败，不能返回伪成功或静默换成其他数据源。
- 图谱业务概念使用 `KnowledgeUnit`；逻辑属性通过 `HAS_LOGICAL_PROPERTY` 关联 `KnowledgeProperty`，工具返回逻辑属性不暴露原始 `properties_json`。空间层级沿 `HAS_CHILD_SPACE` / `CONTAINS_UNIT` 查询，不维护静态业务副本。

## 4. 目录与产物规则

- 算法版本目录只放算法代码、单元测试和就地 `README.md`。benchmark 数据、golden、评测 PDF/MD 不得放入算法目录。
- 正式评测数据放 `test_data/eval/`；历史 V2/V3 benchmark 分别放 `test_data/eval-v2/`、`test_data/eval-v3/`。单测专用 fixture 可随对应测试维护；V2 旧策略 fixture 保留在 `v2/tests/fixtures/archive/`。
- 评测/打分程序放 `eval/`，统一核心入口为 `eval/run.py`，辅助程序放 `eval/tools/`。不得向算法目录加入 `*_benchmark_eval.py`、`*_fixed_judge.py` 等评测脚本。
- V2 的 `standard_strategy.py`、`official_document_strategy.py`、`benchmark_lint.py`、`markdown_quality.py` 是在线包模块，保留原位置；数据门禁可运行 `python3 -m algorithm.knowledge_extract.v2.benchmark_lint`。
- 评测与实验输出统一放 `tmp/`，按算法、版本、时间或独立批次隔离；不得覆盖 golden 或污染源码。正式评测批次位于 `tmp/eval/runs/`。
- 模块设计、接口和运维文档统一放 `docs/`，按 `架构/`、`接口/`、`Agent/`、`算法/`、`部署/` 分类。模块目录只留就地 README，打包脚本从 `docs/` 引用交付文档。
- `tests/`、`test_data/`、`eval/`、`tmp/` 不得进入环境镜像或 `deploy/runtime` 源码包；调整交付范围时同步检查 `.dockerignore` 与 `package-deploy.ps1`，嵌套单测也必须排除。
- 合并带入布局冲突时，合并方须在合并提交中移动或删除冲突文件，并同步修正引用、测试和 README。

## 5. 本地开发与测试

命令默认在仓库根目录执行，依赖和服务配置先按根 `.env.example` 与模块 README 准备。

```bash
# 本地 Backend 开发入口；完整 GPU/队列环境使用 Compose
bash run.sh

# 单元测试；Windows 使用 .venv/Scripts/python.exe -m pytest -q
python3 -m pytest -q

# 按改动范围先运行相关测试
python3 -m pytest tests/backend -q
python3 -m pytest tests/agent -q

# 已有 Backend 上的小规模链路验证
python3 eval/mini_eval.py
```

- `pytest.ini` 的 `testpaths` 是显式白名单，并不自动覆盖仓库中所有测试。新增测试或修改白名单外的版本（例如 `latest`）时，须显式运行对应测试，并按维护需要更新收集范围。
- 根 `conftest.py` 已加入仓库根、`agent/` 与 `algorithm/` 的导入路径。
- 任何改变算法或服务可观察行为的提交，必须在同一提交中更新覆盖该行为的测试。不得推送测试失败或契约过期的行为变更。
- 合并导致测试失败时，必须先恢复契约或按新的预期同步消费者与测试；不得删除、跳过或静默失败测试以取得绿色结果。
- 外部服务、Redis、模型或 GPU 不可用时，明确说明未验证项；不能将依赖缺失、跳过或仅静态检查报告为链路通过。

## 6. 评测与生产发布门禁

**88 是测试服务器，统一复用 `qasystem_backend`，不得另建评测 Backend。** 测试部署执行本次改动相关测试与小规模链路验证。解析、切片、抽取或沉淀流水线实际发布到生产前，必须依次完成下列门禁，全部通过才允许发布。

1. **单元测试通过**：运行 `python3 -m pytest -q`（Windows 使用 `.venv/Scripts/python.exe`），另行覆盖默认收集范围之外的受影响测试。
2. **正式全量评测通过**：对运行中的 Backend 使用 LLM 匹配，命令如下。

   ```bash
   python3 eval/run.py --match-mode llm --require-scored --fail-on-empty \
     --min-f1 0.5 --max-case-seconds 1800 \
     --max-stage-seconds parse=300,chunk=120,extract=900
   ```

   退出码 `0` 才表示所执行检查未失败；`1` 为配置/发现失败，`2` 为 case 失败（含空抽取、重试后跳过），`4` 为质量门禁未过。必须核对 `summary.json` 的 `gate.empty_failures`、耗时、评分有效性和覆盖率。阈值以经确认的最新有效基线为准，不能为放行而降低。

3. **同口径基线对比通过**：使用 `--baseline <有效运行目录或summary.json>` 对比上一次有效全量基线。当前默认逐案最大 F1 降幅为 `0.02`、API 耗时增幅为 `0.2`，可分别通过 `--max-f1-drop`、`--max-latency-increase` 显式固定。数据、golden、本体、阶段、评分代码与 judge 口径须可比；旧评分或不完整批次不能直接作为质量结论。
4. **生产观测可用**：检查空抽取、慢阶段、本体无效告警写入本地日志，`/api/v1/observability/metrics` 可访问。生产不能访问外网，告警只使用本地日志和指标。空结果按 `request_id` 与 `stats.extraction.empty_reason` 定位。

评测约定：

- 默认 `pipeline` 模式调用正式知识沉淀 API；`chunk-extract` 与 `extract` 用于分阶段实验。比较抽取算法时，两边都用 `extract --from-run <同一冻结批次>`，不能把生成切片的批次直接当作同口径抽取基线。
- `mini_eval.py` 是小样本入口，`quick_eval.py` 是诊断入口，均不能替代生产全量门禁。`identity` 只用于连通性与精确匹配对照，不能替代正式 LLM 评分。
- judge 失败、降级、缺评分、未完成批次或 `gate.passed=null` 均不能称为质量通过；解释指标时同时报告覆盖率及失败 case，不能只报有效样本均值。
- 保留 `manifest.json`、`summary.json`、`report.md`、实际输入/输出与配置来源。API 耗时和 judge 评分耗时分开解释；复用解析/切片/预测时明确复用范围。
- 失败 case 必须沉淀为 `test_data/eval/` 回归 case 或对应层单测后才能关闭。完整规则见 `eval/README.md`。

## 7. 环境镜像与交付

- **唯一正式应用环境镜像是 `qasystem-env-v1`。** `Dockerfile` 仅负责 CUDA、vLLM、Torch、系统工具、语言运行时及依赖契约；最终镜像不得包含 Backend、Agent、算法、MCP 业务源码、模型、部署密钥或运行数据。
- 源码单独交付，通过 `QASYSTEM_SOURCE_DIR` 只读挂载到 `/app`；模型通过 `HOST_MODELS_DIR` 只读挂载到 `/models`。不得新增 runtime overlay 镜像、镜像转换脚本、复制业务源码的 Dockerfile，或把 Compose 源码烘焙入环境镜像。
- 可写数据、临时文件和 Agent 输出使用独立卷；MinerU `.locks` 使用单独可写挂载，不能把整个源码或模型挂载改成可写。
- 部署变量统一在根 `.env.example` 维护，真实值放部署端 `.env`。Compose 负责拓扑、挂载、命令、健康检查与端口，避免重复维护可调默认值。
- 保留 `GPU_DEVICE_IDS`；生产 8 卡 A40 常用 `4,5,6,7`。决策模型或 vLLM 独占卡须与 Backend GPU 保留配置同步，避免调度争抢。
- 只有环境依赖变化才重建、重发环境镜像；普通源码发布更新外部源码并重建相关服务容器即可。构建与打包脚本在长操作中必须输出带时间戳的阶段进度。
- 构建入口为 `build-image.ps1`，交付入口为 `package-deploy.ps1`；执行前检查参数与依赖，不按旧 README 假设存在基础/运行时双镜像流程。

当前编排边界：

- 主 `docker-compose.yml` 包含 `backend`、`agent`、`redis`，以及 `decisions` profile 下的 `clef`、`nanojev`。Backend 容器名为 `qasystem_backend`，默认宿主端口 `8006`；Agent 默认端口 `9180`，也可由 Backend `/agent` 代理访问。
- Agent 默认通过 `AGENT_MCP_URL` 连接 `http://agent-mcp:9181/mcp`。当前主 Compose 未定义 `agent-mcp`，`package-deploy.ps1` 的运行源码清单也未包含 `agent_mcp/`；部署 Agent 链路时必须核对该独立服务的源码交付、编排和可达地址，不能宣称主 Compose 已包含它。
- 仓库存在 `Dockerfile.remote-only` 等远程依赖环境文件，但其存在不改变上述正式交付镜像约束，不得直接扩展为第二套正式交付方案。

更新与验证：

```bash
# 测试机在原挂载目录内同步源码后，重启服务加载新代码
# 有 Git 元数据时沿用 git 同步，并遵守下文禁止快进的规则
docker compose restart backend agent

# .env、挂载、命令改变，或交付时替换源码目录后，重建容器
docker compose up -d --no-deps --force-recreate backend agent

curl -fsS http://127.0.0.1:8006/health
curl -fsS http://127.0.0.1:8006/agent/health
```

`restart` 不会重新注入 `.env`。替换磁盘文件也不代表 Python 进程已加载新代码；必须按受影响服务重启或重建，并验证实际运行行为。单独部署的 Agent MCP 和决策模型服务有改动时同样处理。

## 8. 配置维护规则

- 应用配置统一从 `docker.configuration` 读取，不得在 Backend、Agent、算法、外部服务适配器、模型服务或 MCP 应用中新增直接的 `os.getenv`、`os.environ.get`、环境下标读取。启动时绑定 GPU、传递子进程环境等进程管理操作不作为业务配置读取入口。
- 环境变量清单只在根 `.env.example` 维护；新增、删除、改默认值或生效方式，必须同步消费者、测试及 `docs/部署/配置体系说明.md` 中的迭代记录，并随代码提交。不得通过业务代码、Compose 或评测参数文件另设一套默认部署配置。
- 配置声明维护于 `docker/config-declarations.json`，部署默认值仍只在根 `.env.example` 定义；声明版本保存在 `docker/config-releases/`，旧版本不得覆盖。声明包括说明、默认值、消费者、热更新属性和兼容回退表达式。增删消费者、修改默认回退或热更新属性必须登记新声明版本。
- 发布、打包和部署必须通过 `python -m docker.config_contract` 门禁；不得静默重新扫描生成声明来消除失败，不得保留失去消费者的配置。外部依赖自行读取的参数必须明确登记外部消费契约。
- 部署 `.env` 和 `runtime-config/config.json` 的变更必须在 CLI UI 展示与上次批准版本的差异，由用户逐页审核、填写原因并人工确认。每次人工批准自动归档独立配置版本；保存批准、容器可读验证和进程实际读取分开记录。
- 配置 guard 必须在部署前及应用容器启动时执行。未经确认、文件与批准记录不符、清单升级未审核或历史记录不一致时拒绝启动，不得添加跳过 guard、自动确认或非交互批准选项。
- 原始 `.env` 归档与批准记录仅保存在部署运行时私有目录，凭据脱敏，不进入 Git、报告、JSON 历史视图或交付镜像；JSON 只保留凭据指纹。AI、脚本、CI 不得替用户生成生产/测试部署批准记录；单元测试的临时目录记录除外。修改 `.env` 需要 recreate；仅修改已挂载热配置影响新请求，无需重建环境镜像。

## 9. Git 与完成标准

- 合并一律使用 `git merge --no-ff`，即使可以快进也生成合并提交，保留功能分支分组与回滚边界。禁止 `git merge --ff-only`、`git pull --rebase` 等抹平分支拓扑的操作；仓库配置应保持 `merge.ff=false`。
- 开始修改前检查工作区状态，保留用户已有修改。源码快照若不含 `.git`，如实说明无法核对分支、提交与差异，不自行初始化 Git 或声称完成提交。
- 完成时说明改了什么、验证了什么、哪些因依赖或环境未验证。代码已改、测试通过、评测通过、已部署是不同状态，不得混为一谈。

## 10. 文档导航

- [文档总索引](docs/文档索引.md)
- [算法模块](algorithm/README.md)、[Backend](backend/README.md)、[既有 MCP 服务](mcp_services/README.md)、[外部服务](external_services/README.md)
- [Agent 运行时](agent/README.md)、[Agent 架构](docs/Agent/架构设计.md)、[Agent 开发扩展](docs/Agent/开发扩展指南.md)、[Agent 部署运维](docs/Agent/部署运维指南.md)
- [算法接口协议](docs/接口/算法接口协议.md)、[抽取请求配置覆盖](docs/接口/抽取请求配置覆盖.md)、[错误码协议](docs/接口/错误码协议.md)
- [应用服务 API 索引](docs/接口/应用服务API索引.md)、[外部应用服务与鉴权](docs/接口/外部应用服务与鉴权说明.md)
- [评测说明](eval/README.md)、[插件与数据库环境](docs/部署/插件与数据库环境索引.md)、[交付包说明](docs/部署/交付包说明.md)、[离线验收](docs/部署/离线验收说明.md)
