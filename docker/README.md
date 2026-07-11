# Docker 构建与部署

`backend.Dockerfile` 只构建后端运行环境，包含 Python 依赖、LibreOffice、OpenCode
和诊断工具，不包含业务源码或模型。

业务源码通过 `./qasystem-runtime:/app` 挂载，模型通过 `./models:/models:ro`
挂载。修改代码后只需同步运行目录并重启后端容器。

在仓库根目录构建运行环境镜像：

```bash
docker build -f docker/backend.Dockerfile -t qasystem-backend:latest .
```

生成本地交付目录：

```powershell
.\docker\package-deploy.ps1
```

脚本会重新生成 `deploy/qasystem-runtime`，并复制 Compose、环境变量模板和部署说明。
`deploy/` 与 `models/` 都不由 Git 跟踪，模型需要单独同步到服务器。
