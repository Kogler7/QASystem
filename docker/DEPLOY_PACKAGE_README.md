# QASystem 双镜像交付包

正式部署由两个镜像契约协同组成：

- `qasystem-base-v1`：CUDA、vLLM、Torch、系统程序和 Python 依赖。仅首次部署或依赖契约不兼容升级时交付。
- `qasystem-runtime-v1`：Backend、Agent、算法和运行入口。每次代码发布都在目标机基于已有 base 构建。

运行配置不固化在 base，也不在 Compose 中重复枚举，统一放在本目录 `.env`。Compose 通过 `env_file` 将同一份配置注入 Backend 和 Agent。

## 文件

| 文件 | 用途 |
| --- | --- |
| `runtime-context.tar` | 日常发布内容；只含应用源码、运行时 Dockerfile 和校验工具 |
| `apply-runtime.sh` | 校验、构建、验证、切换服务，失败时恢复旧 runtime 标签 |
| `qasystem-base-v1.tar` | 可选的一次性基础镜像，仅 `-IncludeBaseImage` 时生成 |
| `redis-7-alpine.tar` | 可选离线 Redis 镜像，仅 `-IncludeRedisImage` 时生成 |
| `docker-compose.yml` | Backend、Agent、Redis 拓扑 |
| `.env.example` | 唯一运行环境变量模板 |
| `migrate-legacy-image-to-base.sh` | 仅用于把已经传到服务器的旧完整镜像转换为 base |
| `SHA256SUMS` / `交付清单.json` | 内容校验和版本记录 |

`docker save qasystem-runtime-v1` 会连同 30GB 左右的 base 层一起导出，因此正常交付禁止导出 runtime 镜像。传输 `runtime-context.tar` 后在目标机本地构建，Docker 会直接复用已有 base。

## 首次部署

若交付包包含 base 和 Redis：

```bash
sha256sum -c SHA256SUMS
docker load -i qasystem-base-v1.tar
docker load -i redis-7-alpine.tar
cp .env.example .env
# 编辑 .env 中的外部服务、密钥、模型路径和 GPU_DEVICE_IDS
chmod +x apply-runtime.sh
./apply-runtime.sh
```

如果大镜像已经以旧完整镜像形式存在，先按 `RUNTIME_BASE.md` 运行迁移脚本生成 `qasystem-base-v1`，无需再次上传 base。

## 日常代码升级

交付方运行：

```powershell
.\package-deploy.ps1
```

只传输新的 `runtime-context.tar`、`SHA256SUMS`、`交付清单.json` 以及有变更的部署文件。目标机保留现有 `.env`，但首次切换双镜像时必须按新 `.env.example` 合并关键变量；`apply-runtime.sh` 会拒绝旧镜像标签、空 GPU 范围或缺失的内部服务变量。然后执行：

```bash
./apply-runtime.sh
```

脚本先构建候选镜像并执行完整运行时校验，通过后才把固定标签切换到新镜像并重建服务。旧 runtime 会保留带时间戳的 rollback 标签；健康检查失败会自动恢复原标签。

只构建和验证镜像、不重启服务：

```bash
./apply-runtime.sh --build-only
```

## 配置边界

- 生产 8 卡 A40 服务器仅使用后四卡时保留 `GPU_DEVICE_IDS=4,5,6,7`。
- 普通环境参数修改只需更新 `.env` 并重新创建容器，不需要重建任一镜像。
- `BACKEND_IMAGE` 保持 `qasystem-runtime-v1`。
- 模型不在镜像中，继续通过 `HOST_MODELS_DIR` 只读挂载到 `/models`。
- 只有 CUDA、vLLM、Torch、系统库或 Python 依赖契约不兼容变化时，才同步升级 base/runtime 的版本号。
