# Docker 构建说明

| 文件 | 说明 |
|------|------|
| `backend.Dockerfile` | 后端镜像：依赖 `backend/requirements.txt`，复制 `backend/app` 与 `algorithm/`。工作目录 `/app/backend`，`PYTHONPATH=/app`。 |
| 仓库根 `.dockerignore` | 构建后端镜像时忽略 `frontend/`、`node_modules`、本地数据与密钥等。 |

**构建命令**（必须在仓库根目录）：

```bash
docker compose build backend
```

勿再使用已删除的 `backend/Dockerfile`；Compose 已改为 `context: .` + `dockerfile: docker/backend.Dockerfile`。
