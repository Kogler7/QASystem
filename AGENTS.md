# Repository Delivery Rules

- The only application environment image is `qasystem-env-v1`.
- `Dockerfile` must contain CUDA, vLLM, Torch, system tools, language runtimes and dependency contracts only. It must not copy Backend, Agent, algorithm, MCP application source, models, deployment secrets or runtime data into the final image.
- Application source is delivered separately and mounted read-only from `QASYSTEM_SOURCE_DIR`. Do not add a runtime overlay image, image-conversion script, source-copying Dockerfile, or Compose source baked into the environment image.
- Models stay outside the image and are mounted read-only from `HOST_MODELS_DIR` to `/models`.
- Keep deployment variables in root `.env.example`. Compose should contain topology, mounts, commands, health checks and ports rather than duplicate tunable environment defaults.
- Preserve `GPU_DEVICE_IDS`; the production 8-card A40 host commonly sets `GPU_DEVICE_IDS=4,5,6,7`.
- Rebuild and resend `qasystem-env-v1` only when environment dependencies change. Ordinary source releases only replace the external source directory and recreate Backend/Agent containers.
- Build and packaging scripts must print timestamped phase progress during long operations.
