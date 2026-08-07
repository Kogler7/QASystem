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
