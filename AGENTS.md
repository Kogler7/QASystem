# Repository Delivery Rules

- The only application environment image is `qasystem-env-v1`.
- `Dockerfile` must contain CUDA, vLLM, Torch, system tools, language runtimes and dependency contracts only. It must not copy Backend, Agent, algorithm, MCP application source, models, deployment secrets or runtime data into the final image.
- Application source is delivered separately and mounted read-only from `QASYSTEM_SOURCE_DIR`. Do not add a runtime overlay image, image-conversion script, source-copying Dockerfile, or Compose source baked into the environment image.
- Models stay outside the image and are mounted read-only from `HOST_MODELS_DIR` to `/models`.
- Keep deployment variables in root `.env.example`. Compose should contain topology, mounts, commands, health checks and ports rather than duplicate tunable environment defaults.
- Preserve `GPU_DEVICE_IDS`; the production 8-card A40 host commonly sets `GPU_DEVICE_IDS=4,5,6,7`.
- Rebuild and resend `qasystem-env-v1` only when environment dependencies change. Ordinary source releases only replace the external source directory and recreate Backend/Agent containers.
- Build and packaging scripts must print timestamped phase progress during long operations.
- Use `APPLICATION_SERVICE_URL` for the application-team server and keep `FILE_SERVICE_URL` only as a fallback. Register external endpoints in `external_services/application.py`; do not scatter third-party paths through algorithms.
- Preserve queue execution for authenticated external calls. Queue records carry only `auth_context_id`; never place Authorization headers in payload artifacts, logs, SSE, `execution`, or results.
- SQL may retain the configured direct connection compatibility. Do not restore direct Neo4j or Elasticsearch access; unopened graph-write, confidence, and document-evidence capabilities must fail explicitly when invoked.

# Git Workflow Rules

- 合并一律禁止使用 fast-forward（`git merge --no-ff`），即使可以快进也要生成合并提交，保留功能分支的提交分组与回滚边界；禁止 `git merge --ff-only` 与 `git pull --rebase` 等会抹平分支拓扑的操作。仓库已配置 `merge.ff=false` 作为兜底。

# Directory Layout Constraints

- `tests/`、`test_data/`、`eval/`、`tmp/` 不属于运行时应用源码，不得进入交付镜像或 `deploy/runtime` 源码包；`.dockerignore` 与 `package-deploy.ps1` 已将其排除。
- `algorithm/knowledge_extract/v1/`、`v2/`、`v3/`、`v3_1/`（配置值 `v3.1`）、`latest/` 只保留算法代码与其单元测试（`v3_2`、`v3_3` 与旧 `v3_1` 实验实现已下线）：
  - benchmark 数据集统一放 `test_data/`：`v2` 对应 `test_data/eval-v2/`，`v3` 对应 `test_data/eval-v3/`；golden、评测文档（PDF/MD）一律不得进入算法目录；
  - benchmark 评测/打分脚本统一放 `eval/`（统一入口 `eval/run.py`，直接评测线上链路；旧版 legacy 脚本已清理），算法目录下不得出现 `*_benchmark_eval.py`、`*_fixed_judge.py` 等评测脚本；
  - 评测辅助脚本放 `eval/tools/`（如 `v2_scripts/merge_official_results.py`）；
  - `v2` 的策略模块（`standard_strategy.py`、`official_document_strategy.py`）与 benchmark 数据门禁（`benchmark_lint.py`、`markdown_quality.py`）是在线包模块，保留在 `v2/` 内，可用 `-m algorithm.knowledge_extract.v2.benchmark_lint` 运行；旧策略 fixture 归档在 `v2/tests/fixtures/archive/`。
- 从其他分支合并带入的文件与上述布局冲突时，合并方须在合并提交内按约定归位（移动/删除冲突文件，并同步修正 README、脚本与测试引用），不得把 benchmark 数据、评测脚本重新引入算法目录。
- `latest` 必须是带完整代码的实体目录，不得是 `v3` 的软链或运行时映射；`KG_EXTRACT_ALGO_VERSION=latest` 直接运行 `latest` 包。
- 评测输出统一写到 `./tmp/` 并按算法/版本/时间隔离，不污染源码目录或 golden 数据。
- 模块级设计/接口/运维文档统一收在 `docs/` 按主题分目录（`接口/`、`Agent/`、`算法/`、`部署/`），不得在模块目录里另散落 `.md`；模块目录只保留就地说明的 `README.md`。打包脚本引用文档一律从 `docs/` 取（见 `package-deploy.ps1`）。

# Test Maintenance Rules

- Any commit that changes observable behavior of an algorithm or service must update the tests covering that behavior in the same commit. Do not push behavior changes with failing or stale tests.
- Tests under `tests/` encode production contracts (e.g. the splitter's `__source_range` metadata consumed by `algorithm/knowledge_pipeline/chunking.py`). Dropping or changing such a contract requires updating every consumer in the same commit.
- If a merge brings in changes that break existing tests, the person merging must reconcile them before pushing — either restore the contract or update the tests to the new intended behavior. Never delete or silence failing tests just to get green.

# Release Quality Gate

改了解析、切片、抽取或沉淀流水线后，发布前必须按顺序过门禁，全绿才允许部署；任何一项失败先排查修复再发布：

1. **单元测试全绿**：`.venv/Scripts/python.exe -m pytest -q`（Windows 开发机）或 `python3 -m pytest -q`。
2. **全量评测门禁**（对运行中的 Backend，正式判定用 `--match-mode llm`，先 identity 跑通链路也可）：
   `python3 eval/run.py --match-mode llm --fail-on-empty --min-f1 0.5 --max-case-seconds 1800 --max-stage-seconds parse=300,chunk=120,extract=900`
   退出码 `0` 才过；`2`=有空抽取或失败 case（必须排查 `summary.json` 的 `gate.empty_failures`）；`4`=F1 或耗时预算未过；`1`=配置错误。阈值以最新基线为准（见 `tmp/eval/runs/` 历次全量记录）。
3. **基线对比**：与上一次全量基线比，实体 F1 不得明显下降、单 case 耗时不得明显上升；回退即挡。
4. **生产观测**：确认 `QASYSTEM_ALERT_WEBHOOK` 已配置（空抽取/慢阶段/本体无效告警），`/api/v1/observability/metrics` 可访问；用户反馈的空结果问题凭 `request_id` 与 `stats.extraction.empty_reason` 定位到阶段。

部署沿用 git 同步 + `docker compose restart backend agent`；评测失败 case 必须沉淀为 `test_data/eval/` 回归 case 或对应层单测后再关闭。

# Documentation Index

- [Plugin and database environment](docs/部署/插件与数据库环境索引.md): local VSIX, Attu/Milvus, Neo4j browser, and data-access boundaries.
- [Application-service API](docs/接口/应用服务API索引.md): live knowledge-map service endpoint directory and ingestion contract; see [Swagger UI](https://zhongche.zhouhao.work/docs) for current schemas.
- [QASystem documentation index](docs/文档索引.md): all local architecture, API, deployment, and test documents.
