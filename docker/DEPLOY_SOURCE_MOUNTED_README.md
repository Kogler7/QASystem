# QASystem 源码挂载部署

本目录是可选部署模式。它复用父目录中的 QASystem 与 Redis 镜像归档、`models/` 和外部服务配置，但会将本目录的 `runtime/` 挂载到容器 `/app`。

## 适用场景

- 需要在目标服务器查看或替换应用源码；
- 使用同一套运行镜像依赖，快速迭代 Python 业务代码；
- 需要与旧版 `qasystem-runtime:/app` 部署方式兼容。

## 启动

```powershell
cd .\source-mounted
Copy-Item .env.example .env
# 编辑 .env；模型目录默认指向父目录的 ../models
docker compose up -d
```

首次离线部署仍需在父目录导入镜像：

```powershell
cd ..
docker load -i .\qasystem-backend-latest.tar
docker load -i .\redis-7-alpine.tar
```

`runtime/` 和父目录的镜像必须来自同一交付包。不要将不同版本的源码、依赖镜像或 OFD 转换器混用。
