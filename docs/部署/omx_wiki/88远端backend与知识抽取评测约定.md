---
title: "88远端Backend与知识抽取评测约定"
tags: ["88", "backend", "evaluation", "knowledge-extraction", "deployment"]
created: 2026-10-07T06:36:00.647Z
updated: 2026-10-07T06:36:00.647Z
sources: []
links: []
category: environment
confidence: medium
schemaVersion: 1
---

# 88远端Backend与知识抽取评测约定

## 远端环境

- 默认远端主机：SSH 别名 `88_3090`，主机名 `tangshan88`。
- 项目源码目录：`/mnt/data3/qasys/qasystem`。
- Backend 容器：`qasystem_backend`，宿主端口 `8006`；健康检查：`curl -fsS http://127.0.0.1:8006/health`。
- 当前已验证 Backend 健康，返回 `status: ok`。

## 远端启动配置

- Compose 工作目录：`/mnt/data3/qasys`。
- 环境文件：`/mnt/data3/qasys/.env`。
- Compose 文件：`/mnt/data3/qasys/docker-compose.yml`。
- Backend overlay：`/home/ycheng/ZhCHRAG/qasystem/tmp/qasys-backend-v2-siliconflow.yml`。
- overlay 当前设置 `KG_EXTRACT_ALGO_VERSION=v2`。
- 不要直接在项目目录执行普通 `docker compose up -d backend`：该目录没有 `.env`，会失败；需要使用远端 qasys 的 compose 配置。

## 评测约定

后续本项目知识抽取评测默认全部在 88 执行：

```bash
ssh 88_3090 \
  'cd /mnt/data3/qasys/qasystem && python3 eval/run.py --match-mode identity'
```

评测产物位于远端 ` /mnt/data3/qasys/qasystem/tmp/eval/runs/`。严格对比抽取算法时，先用 `--mode chunk-extract` 固定切片，再用 `--mode extract --from-run <运行目录>`；算法版本必须切换 Backend 的环境或 overlay，不能只给评测脚本进程设置 `KG_EXTRACT_ALGO_VERSION`。

## 源码边界

记录时远端 `develop` 为 `9d44bcb`，落后远端 `origin/develop` 的 `3ace211`，且没有 `v3.1` 目录；本地工作区含未提交的 `v3.1` 改动。未经明确同步，不要自动覆盖远端源码；远端评测以 `/mnt/data3/qasys/qasystem` 当前源码为准。
