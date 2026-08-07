# Repository Delivery Rules

- The deployable image pair is `qasystem-base-v1` plus `qasystem-runtime-v1`.
- `Dockerfile` defines the dependency-only base. It must not contain Backend, Agent, algorithm, or MCP application source and must not contain deployment environment settings.
- `Dockerfile.runtime` is the only deployable application image definition. Runtime source, entrypoint metadata, health checks, and application-specific image environment belong there.
- Normal code releases run `build-image.ps1` without `-BuildBase`. Rebuild the base only when CUDA, vLLM, Torch, system packages, Python dependency contracts, Node/OpenCode, Java, or OFD tooling changes incompatibly.
- The legacy mixed image name `qasystem-backend:latest` may appear only in `docker/make-runtime-base.sh` and its migration documentation. Do not restore it as a build, Compose, verification, or delivery default.
- `package-deploy.ps1` always packages the small runtime build context. Export `qasystem-base-v1` only when explicitly requested for a first deployment; never export `qasystem-runtime-v1` because `docker save` would duplicate all base layers.
- Keep deployment variables in the root `.env.example`. Compose should use `env_file` and contain topology, mounts, health checks, and ports rather than duplicate tunable environment defaults.
- Preserve `GPU_DEVICE_IDS`; the production 8-card A40 host commonly sets `GPU_DEVICE_IDS=4,5,6,7`.
- Fixed `v1` names are intentional. Increment both image contracts together only for incompatible dependency changes, not for ordinary source releases or dates.
- Build and migration scripts must print timestamped phase progress during long operations.
