# Docker 部署说明（轨道交通知识服务系统）

## 架构

| 服务 | 说明 |
|------|------|
| **frontend** | Nginx 托管 Vue 构建产物，`/api` 反代到 backend，`/health` 反代后端健康检查 |
| **backend** | FastAPI（Uvicorn），`PYTHONPATH` 含仓库根，可进程内加载 `algorithm/` |
| **db** | MySQL 8.0，utf8mb4；持久化用户与文档元数据等 |
| **neo4j**（可选 profile） | `docker compose --profile neo4j up` 时启动，供图谱问答、图谱更新、抽取落库等 |

## 前置要求

- Docker 20+ 与 Docker Compose v2（`docker compose` 子命令）
- 机器内存建议 **≥ 8GB**（后端镜像 `pip install` 含 PyTorch 等，构建与运行均较重）
- 首次构建可能 **10～30 分钟以上**，属正常现象

## 部署步骤

### 1. 获取代码

```bash
git clone <仓库地址>
cd qasystem
```

### 2. 环境变量

在**仓库根目录**：

```bash
cp .env.example .env
```

`backend` 服务使用 `env_file: .env`，`.env` 中大部分键会原样注入容器；`docker-compose.yml` 里的 `environment` 会覆盖 **`DATABASE_URL`**（固定为连接服务名 `db`）以及 `CONFLICT_NEO4J_*` 等与 Neo4j 的映射。若未创建 `.env`，`docker compose` 会报错。

至少修改：

- `MYSQL_ROOT_PASSWORD`、`MYSQL_PASSWORD`（生产强密码）
- `JWT_SECRET`（生产强随机串；`ENVIRONMENT=production` 时不可用默认值）
- `DIFY_API_URL`、`DIFY_API_KEY` 等（按实际 Dify 环境填写）

若 MySQL 密码含特殊字符，`DATABASE_URL` 由 Compose 拼接；请避免在密码中使用未转义的 `@`、`:` 等，或对密码做 [URL 编码](https://developer.mozilla.org/en-US/docs/Glossary/Percent-encoding)。

### 3. 构建并启动

```bash
docker compose build
docker compose up -d
```

需要 **Compose 自带的 Neo4j** 时（与 `.env` 中 `NEO4J_URI=bolt://neo4j:7687` 配合）：

```bash
docker compose --profile neo4j up -d --build
```

### 4. 验证

- 前端：`http://<主机>:${HTTP_PORT:-80}/`
- 健康检查：`http://<主机>/health` → 应接近 `{"status":"ok"}`（经 Nginx 转发）

```bash
docker compose ps
docker compose logs -f backend
```

### 5. 常用运维命令

```bash
docker compose ps
docker compose logs -f backend
docker compose logs -f db
docker compose restart backend

docker compose down
# 删除卷（清空 MySQL、上传文档、预处理输出、Neo4j 数据，慎用）
docker compose down -v
```

## 数据卷

| 卷名 | 用途 |
|------|------|
| `mysql_data` | MySQL 数据目录 |
| `backend_documents` | 后端 `/app/backend/data/documents`（上传文件） |
| `backend_preproc_output` | 文档预处理输出 |
| `neo4j_data` | 仅在使用 `profile neo4j` 时创建 |

`docker compose down` **不会**删除命名卷；`down -v` 会。

## 可选：本地 Milvus（意图识别）

与主栈独立编排：

```bash
export DOCKER_VOLUME_DIRECTORY=.
docker compose -f milvus-docker-compose.yml up -d
```

数据默认落在仓库下 `volumes/`（已在 `.gitignore` 中忽略）。在 `.env` 或 `algorithm/intent_recognition_model/.env` 中配置 `MILVUS_URI=http://host.docker.internal:19530`（后端容器访问宿主机）或按实际网络填写。

## 生产环境建议

1. **HTTPS**：前置 Nginx / Caddy / 云 LB 做 TLS，回源到本机 `HTTP_PORT`。
2. **端口**：`.env` 中保持 `MYSQL_BIND=127.0.0.1`，勿对公网暴露 3306、7687；仅开放 80/443。
3. **资源**：根据并发调高 Docker 内存与 CPU；长耗时接口已在 `nginx.conf` 中放宽代理超时。
4. **备份**：定期备份 `mysql_data`、业务所需的 `backend_documents` 与 Neo4j 卷。
5. **镜像**：后端构建上下文为**仓库根**，定义见 `docker/backend.Dockerfile`，说明见 `docker/README.md`。

## 故障排查

| 现象 | 处理 |
|------|------|
| 前端 502 / 空白 | `docker compose logs backend`，确认健康检查通过；首次启动 `pip`/模型加载慢，可增大 `start_period` 或等待。 |
| 数据库连不上 | 检查 `db` 是否 `healthy`；账号与 `MYSQL_*` 是否与 compose 中 `DATABASE_URL` 一致。 |
| 组件「模块不可用」 | 确认 `algorithm/` 已打入镜像；Neo4j/Milvus/LLM 地址在**容器内**可达（常用 `host.docker.internal`）。 |
| 构建 OOM | 增大 Docker Desktop 内存，或在本机 venv 开发、仅 Docker 跑 db+前端。 |

## 更新代码后重新发布

```bash
git pull
docker compose up -d --build
# 必要时
# docker compose build --no-cache backend
```
