# QASystem Backend Deployment

本目录中的后端镜像只提供运行环境。业务代码位于 `qasystem-runtime/`，模型位于
`models/`，两者都通过宿主机目录挂载到容器。

## 首次启动

```bash
docker load -i qasystem-backend-latest.tar
cp .env.example .env
vi .env
docker compose up -d
docker compose ps
```

默认端口为 `8006`。模型可以单独传输或更新，目录位置由 `HOST_MODELS_DIR` 指定。

## 更新代码

覆盖 `qasystem-runtime/` 中的代码后执行：

```bash
docker compose restart
```

## 更新模型

将新模型同步到 `models/`，确保目录结构与 `.env` 中的模型路径一致，然后重启服务。

## 更新运行环境

Python 依赖或系统软件变化时，加载新镜像并重新创建容器：

```bash
docker load -i qasystem-backend-latest.tar
docker compose up -d --force-recreate
```

## 验证

```bash
curl http://127.0.0.1:8006/health
docker compose exec backend python -c "import app.main; print('ok')"
```
