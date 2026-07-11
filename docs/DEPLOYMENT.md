# 后端部署说明

当前部署方式只运行一个 `backend` 服务。镜像只提供 Python、系统依赖和运维工具，
业务代码与模型都通过宿主机目录挂载。

## 生成交付目录

在仓库根目录执行：

```powershell
.\docker\package-deploy.ps1
```

如需同时导出已经构建好的环境镜像：

```powershell
.\docker\package-deploy.ps1 -IncludeImage
```

脚本生成：

```text
deploy/
├── docker-compose.yml
├── .env.example
├── .env
├── README.md
├── qasystem-runtime/
│   ├── backend/
│   ├── algorithm/
│   └── agentcode/
└── qasystem-backend-latest.tar  # 使用 -IncludeImage 时生成
```

模型不随代码或镜像打包。将模型单独同步到 `deploy/models/`，或在 `.env` 中通过
`HOST_MODELS_DIR` 指向其他宿主机目录。

## 构建环境镜像

仅当 Python 依赖、系统软件或镜像工具发生变化时才需要重新构建：

```bash
docker build -f docker/backend.Dockerfile -t qasystem-backend:latest .
```

## 服务器启动

```bash
cd deploy
docker load -i qasystem-backend-latest.tar
cp .env.example .env
vi .env
docker compose up -d
curl http://127.0.0.1:8006/health
```

MySQL、Neo4j、Milvus、Elasticsearch、LLM 和文件服务均作为外部服务配置，当前
Compose 不负责启动这些组件。

## 更新策略

只修改业务代码时，替换 `qasystem-runtime/` 后执行：

```bash
docker compose restart
```

只更新模型时，同步模型目录并按需重启。更新环境镜像、Compose 或环境变量时执行：

```bash
docker compose up -d --force-recreate
```
