# QASystem 环境镜像与外置源码部署

部署只使用一个 Docker 镜像 `qasystem-env-v1`。它包含 CUDA、vLLM、Torch、Python/Node/Java 依赖、LibreOffice、OFD 转换工具和其他系统程序，不包含 Backend、Agent、算法、MCP 源码、模型、密钥或运行数据。

应用源码位于交付目录的 `runtime/`，由 Compose 只读挂载到容器。模型位于 `models/`，同样只读挂载。

## 交付内容

| 路径 | 用途 |
| --- | --- |
| `qasystem-env-v1.tar` | 可选环境镜像；只有 `-IncludeEnvImage` 时生成 |
| `runtime/` | Backend、Agent、算法、MCP、外部应用服务适配器和运行辅助代码 |
| `models/` | 外部模型目录，不纳入镜像 |
| `docker-compose.yml` | Backend、Agent、Redis 拓扑和只读挂载 |
| `.env.example` | 唯一运行配置模板 |
| `SHA256SUMS` / `交付清单.json` | 镜像、源码与部署文件校验 |

## 首次部署

```bash
sha256sum -c SHA256SUMS
docker load -i qasystem-env-v1.tar
docker load -i redis-7-alpine.tar      # 交付包包含时执行
cp .env.example .env
# 编辑外部服务、密钥、模型目录和 GPU_DEVICE_IDS
docker compose config --quiet
docker compose up -d
```

目标 8 卡 A40 主机只允许使用后四卡时保留 `GPU_DEVICE_IDS=4,5,6,7`。`QASYSTEM_SOURCE_DIR=./runtime` 和 `HOST_MODELS_DIR=./models` 分别指向外置源码和模型。

## 普通代码更新

普通发布不构建、不传输镜像，只替换完整的 `runtime/` 目录并重建应用容器：

```bash
sha256sum -c SHA256SUMS
docker compose up -d --force-recreate backend agent
```

不要在运行中的 `runtime/` 内逐文件覆盖。应先把新交付目录完整放好，再整体切换目录或停服务后替换，避免多个进程同时读取不同版本代码。回滚时恢复上一份完整 `runtime/` 后重新创建 Backend 和 Agent。

## 环境升级

只有 CUDA、vLLM、Torch、Python 依赖、系统程序或语言运行时变化时才重新构建并发送 `qasystem-env-v1.tar`：

```powershell
.\build-image.ps1
.\package-deploy.ps1 -IncludeEnvImage
```

模型始终不进入环境镜像。
