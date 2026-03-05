# Docker 部署说明（轨道交通知识服务系统）

## 架构说明

- **frontend**：Nginx 提供前端静态资源，并将 `/api` 反向代理到后端
- **backend**：FastAPI (uvicorn)，连接 MySQL
- **db**：MySQL 8.0，持久化数据与文档元数据

## 服务器要求

- 已安装 Docker 与 Docker Compose（或 `docker compose` 插件）
- 开放端口：80（对外访问）、可选 3306（仅需从本机连库时）

## 部署步骤

### 1. 上传代码到服务器

将项目整个目录上传到服务器（或使用 git clone）。

### 2. 配置环境变量

在项目根目录创建 `.env` 文件（可复制 `.env.example` 后修改）：

```bash
cp .env.example .env
# 编辑 .env，至少修改：
# - MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD（生产环境务必改强密码）
# - JWT_SECRET（生产环境务必改）
# - DIFY_* 按实际 Dify 配置填写
```

### 3. 构建并启动

```bash
cd /path/to/railway_assistant
docker compose build
docker compose up -d
```

### 4. 验证

- 浏览器访问：`http://服务器IP`（或域名），应看到前端页面
- 接口健康检查：`http://服务器IP/health` 应返回 `{"status":"ok"}`

### 5. 常用命令

```bash
# 查看运行状态
docker compose ps

# 查看后端日志
docker compose logs -f backend

# 查看数据库日志
docker compose logs -f db

# 停止
docker compose down

# 停止并删除数据卷（慎用，会清空数据库与上传的文档）
docker compose down -v
```

## 数据持久化

- **MySQL 数据**：保存在 Docker 卷 `mysql_data`，`docker compose down` 不会删除
- **上传的文档文件**：保存在卷 `backend_documents`，对应后端 `/app/data/documents`

## 生产环境建议

1. **HTTPS**：在服务器前加一层 Nginx/Caddy 或使用云厂商负载均衡做 SSL 终结，再反向代理到本机 80 端口
2. **密码**：`.env` 中 `MYSQL_ROOT_PASSWORD`、`MYSQL_PASSWORD`、`JWT_SECRET` 务必改为强随机值
3. **防火墙**：仅开放 80/443，不要对外暴露 3306、8000
4. **备份**：定期备份 `mysql_data` 卷及重要文档目录

## 仅更新代码后重新部署

```bash
git pull   # 或重新上传代码
docker compose build --no-cache   # 需要完全重建时使用
docker compose up -d --build
```

## 故障排查

- **前端能打开但接口 502**：检查 `docker compose logs backend`，确认后端已启动且能连 MySQL；确认 `db` 服务健康（`docker compose ps`）。
- **数据库连接失败**：确认 `.env` 中 `MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE` 与 `docker-compose.yml` 中 backend 的 `DATABASE_URL` 一致（默认用户为 `rail`，数据库为 `rail_assistant`）。
- **上传文档失败**：查看 backend 日志，确认 `/app/data/documents` 卷挂载正常且无权限问题。
