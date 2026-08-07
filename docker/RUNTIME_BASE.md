# Runtime 基础镜像

`make-runtime-base.sh` 用已经上传到部署服务器的完整镜像生成一个不包含 QASystem 应用源码的运行时基础镜像。它不会重新下载 CUDA、vLLM 或 Python 依赖，也不会覆盖源镜像，因此适合在大镜像已经传输完成后执行。

## 在部署服务器执行

```bash
chmod +x ./docker/make-runtime-base.sh
./docker/make-runtime-base.sh \
  qasystem-backend:latest \
  qasystem-backend:runtime-base-20260807
```

脚本会检查 Docker 数据目录剩余空间（按源镜像大小约 3 倍加 2 GiB 估算，因为压平期间会同时存在源层、未压缩 rootfs 和新层），创建临时容器，删除 `/app/backend`、`/app/algorithm`、`/app/mcp_services`、`/app/agent` 和缓存，卸载 `agentcode` 自身安装包，然后执行 `docker export/import` 压平为单层。源镜像和目标镜像同时存在期间需要额外磁盘空间；确认新镜像可用后再由运维人员清理旧镜像。

临时 tar 默认写入 `/tmp`。大盘部署可显式指定目录，例如：

```bash
QASYSTEM_BASE_TMPDIR=/mnt/data3/qasystem-base-tmp \
  ./docker/make-runtime-base.sh
```

## 后续代码更新

代码更新镜像应以生成的 `runtime-base` 为 `FROM`，完整复制上述四个源码目录，并重新复制运行时脚本和 OFD JAR（若它们属于交付内容），最后恢复：

```dockerfile
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=15s --start-period=180s --retries=5 CMD curl -fsS http://127.0.0.1:8000/health || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "app.execution.supervisor"]
```

这一步只新增代码和少量元数据层。不要把新基础镜像导出成 tar 再上传；在同一台服务器本地构建时，Docker 会复用原有 CUDA/vLLM 层。

## 重要限制

- 这是一次性压平操作，不应作为每次发布流程；依赖、CUDA、Torch、vLLM、系统库或模型路径变化时仍需完整重建镜像。
- 该基础镜像不包含应用入口代码，不能直接用于启动 Backend；它只供后续代码 overlay 镜像使用。
- 脚本不删除任何镜像。确认 overlay 镜像通过健康检查后，再按部署方流程清理源镜像。
