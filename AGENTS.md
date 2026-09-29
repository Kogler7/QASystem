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

# Directory Layout Constraints

- `tests/`、`test_data/`、`eval/`、`tmp/` 不属于运行时应用源码，不得进入交付镜像或 `deploy/runtime` 源码包；`.dockerignore` 与 `package-deploy.ps1` 已将其排除。
- `algorithm/knowledge_extract/v2/`、`v3/`、`latest/` 只保留算法代码：
  - `v2/benchmark/` 已迁至 `test_data/eval-v2/`；
  - `v2/scripts/`、`v2/golden_task/` 已迁至 `eval/tools/`；
  - `v2/v2_benchmark_eval.py`、`v2/v2_fixed_judge.py`、`v3/benchmark.py` 已迁至 `eval/legacy/`；
  - `v2/archive/` 已迁至 `v2/tests/fixtures/archive/`（旧策略 fixture）。
- `latest` 必须是带完整代码的实体目录，不得是 `v3` 的软链或运行时映射；`KG_EXTRACT_ALGO_VERSION=latest` 直接运行 `latest` 包。
- 评测输出统一写到 `./tmp/` 并按算法/版本/时间隔离，不污染源码目录或 golden 数据。

# Documentation Index

- [Plugin and database environment](docs/插件与数据库环境索引.md): local VSIX, Attu/Milvus, Neo4j browser, and data-access boundaries.
- [Application-service API](docs/应用服务API索引.md): live knowledge-map service endpoint directory and ingestion contract; see [Swagger UI](https://zhongche.zhouhao.work/docs) for current schemas.
- [QASystem documentation index](docs/文档索引.md): all local architecture, API, deployment, and test documents.
