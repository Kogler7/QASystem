# 从旧完整镜像生成基础镜像

`make-runtime-base.sh` 是旧混合镜像唯一保留的入口。它用于复用已经传到服务器的大镜像，把其中的应用源码删除并压平为 `qasystem-base-v1`。正常构建、打包、部署和验证流程都不再使用旧混合镜像。

```bash
chmod +x docker/make-runtime-base.sh
QASYSTEM_BASE_TMPDIR=/mnt/data3/qasystem-base-tmp \
  docker/make-runtime-base.sh qasystem-backend:latest qasystem-base-v1
```

脚本会：

1. 检查源镜像、目标名称和磁盘空间。
2. 创建隔离临时容器，删除 `/app/backend`、`/app/algorithm`、`/app/mcp_services`、`/app/agent`、`agentcode` 包和缓存。
3. 用 `docker export/import` 生成无源码的单层 base，只恢复 CUDA/系统依赖所需环境元数据；应用环境变量由 runtime 和部署 `.env` 提供。
4. 校验源码已经移除，并确认 FastAPI、Torch、vLLM 和 tini 仍可用。
5. 保留源镜像，不自动删除，便于人工回滚。

压平期间源层、未压缩 rootfs 和新层会同时存在，临时盘和 Docker 数据盘建议各预留约源镜像三倍加 2GiB 的空间。脚本按阶段打印时间和长操作进度。`QASYSTEM_BASE_TMPDIR` 可把临时 tar 放到大容量磁盘；`KEEP_CONTAINER=1` 可在失败后保留临时容器排障。

生成 base 后，后续发布只需要交付 `runtime-context.tar` 并执行：

```bash
./apply-runtime.sh
```

不要重复执行迁移脚本，也不要导出 runtime 镜像。base 仅在底层依赖契约变化时更换。
