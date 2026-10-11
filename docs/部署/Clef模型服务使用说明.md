---
noteId: "7a8ba130c55111f192edfbc308186266"
tags: []

---

# Clef 模型服务使用说明（88 · 容器部署）

> 适用范围：88（`10.126.62.88`，`ssh 88_3090`，用户 `ycheng`）上 qasystem 使用的
> Clef-flash 决策模型服务。
> 服务形态是 `qasys` Compose 项目的 `clef` 服务（容器 `qasystem_clef`），**只在 Compose 内网监听 8188**，
> Backend 通过 `CLEF_SERVICE_URL=http://clef:8188` 调用。
> 历史宿主 systemd 部署（`clef-flash.service`，`172.17.0.1:8188`）已于 2026-10-11 停用，
> 见 [Clef-flash 独立服务](Clef-flash独立服务.md)（历史快照）。

## 1. 现状速查（2026-10-11 实测）

| 项目 | 值 |
| --- | --- |
| 容器 / 服务 | `qasystem_clef` / compose 服务 `clef`，项目 `qasys`（`/mnt/data3/qasys/docker-compose.yml`） |
| 镜像 | `qasystem-env-v1`（`/opt/qasystem/clef-venv` 独立解释器） |
| 状态 | `Up (healthy)`，`StartedAt=2026-10-11T08:41:52Z`，`RestartCount=0` |
| 监听 | 容器内 `0.0.0.0:8188`（容器 IP `172.24.0.6`，**未发布宿主端口**） |
| 健康检查 | `{"status":"ready","model":"clef-flash"}` |
| GPU | 卡 2，UUID `GPU-c10aaf4f-7c2c-3fa5-f47d-bb26db738a49`，占用约 19.3 GiB |
| 鉴权 | `CLEF_API_KEY` 与 Backend 的 `CLEF_SERVICE_API_KEY` **都是空** → `/v1/systemone` 免认证，只在内网可达 |
| 权重 | `/mnt/data3/qasys/models/clef-flash`（只读挂载到 `/models/clef-flash`） |
| 源码 | `/mnt/data3/qasys/qasystem`（只读挂载到 `/app`，`clef_service/app.py` md5 与仓库 HEAD 一致） |
| 实测延迟 | 1 题中文 0.17 s / 143 tokens、3 题英文 0.19 s / 280 tokens；短文本中位 148 ms（1 题）、368 ms（8 题） |
| 调用方 | `qasystem_backend`（已配 `CLEF_SERVICE_URL`）；但 Backend 当前 `KG_EXTRACT_ALGO_VERSION=v2`，**抽取链路尚未使用 Clef** |

姊妹服务：`qasystem_nanojev`（`http://nanojev:8189`，卡 0），协议相同，见
[决策模型部署](决策模型部署.md)。

## 2. 30 秒自检（三条命令）

```bash
# 1) 容器状态
ssh 88_3090 'cd /mnt/data3/qasys && docker compose ps clef'

# 2) 健康检查（宿主机 8188 没有监听，只能进容器查）
ssh 88_3090 'docker exec qasystem_clef sh -c "curl --noproxy \"*\" -fsS http://127.0.0.1:8188/health"'

# 3) 真实调用（从 backend 容器发，等价线上调用路径）
ssh 88_3090 'docker exec qasystem_backend sh -c "curl -sS --noproxy \"*\" \
  http://clef:8188/v1/systemone -H \"content-type: application/json\" \
  -d \"{\\\"state\\\":\\\"订单系统持续返回错误。\\\",\\\"questions\\\":{\\\"outage\\\":{\\\"type\\\":\\\"noul\\\"}}}\""'
# → {"model":"clef-flash","answers":{"outage":{"type":"noul","noul":0.9241}},"usage":{"input_tokens":144,"output_tokens":0}}
```

## 3. 从哪里调用

### 3.1 Backend 容器内（线上路径）

Backend 侧只走 HTTP，不导入 Torch 与模型代码。容器里已注入 `CLEF_SERVICE_URL=http://clef:8188`：

```bash
docker exec qasystem_backend python -c "
import os, json, urllib.request
url = os.environ['CLEF_SERVICE_URL'].rstrip('/') + '/v1/systemone'
body = json.dumps({'state': '订单系统持续返回错误。',
                   'questions': {'outage': {'type': 'noul'}}}).encode()
req = urllib.request.Request(url, data=body, headers={'content-type': 'application/json'})
print(json.load(urllib.request.urlopen(req, timeout=60))['answers'])"
```

Compose 内网里服务名 `clef` 即 DNS 名，无需 `--add-host`。

### 3.2 宿主机 88 上

宿主机没有发布 8188，但能直连容器 IP：

```bash
CIP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' qasystem_clef)
curl -sS --noproxy '*' http://$CIP:8188/health
```

容器 IP 会随重建变化，脚本里请现查现用（上面这条），不要写死。

### 3.3 从本机 Mac（SSH 隧道）

```bash
CIP=$(ssh 88_3090 "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' qasystem_clef")
ssh -N -L 18188:$CIP:8188 88_3090        # 前台占住一个终端；加 -f 可放后台
curl -sS --noproxy '*' http://127.0.0.1:18188/health
curl -sS --noproxy '*' http://127.0.0.1:18188/v1/systemone \
  -H 'content-type: application/json' \
  -d '{"state":"订单系统持续返回错误。","questions":{"outage":{"type":"noul"}}}'
```

### 3.4 代码里（仓库客户端，推荐）

```python
from external_services.clef import ClefFlashClient, ClefServiceError

client = ClefFlashClient(base_url="http://clef:8188")   # 88 上走服务名；本机隧道填 http://127.0.0.1:18188
try:
    result = client.systemone({
        "state": "订单系统持续返回错误，所有客户无法提交订单。",
        "questions": {
            "department": {"type": "choice", "criteria": {"billing": "账单", "technical": "故障"}},
            "outage": {"type": "noul"},
        },
    }, timeout=10)                       # v3.1 单次超时用 KG_EXTRACT_V31_CLEF_TIMEOUT=10
    print(result["answers"])
except ClefServiceError as exc:
    print(exc.error_kind, exc.status_code, exc.retryable)   # 不打印请求/响应正文
```

`ClefFlashClient` 只依赖 `httpx`、`trust_env=False`（不走代理、不继承用户 Authorization），
把失败分类为 `configuration` / `input_too_long` / `schema_error` / `rate_limited` /
`service_unavailable` / `timeout` / `connection` / `invalid_response` / `http_error`。
本机跑上面这段需要仓库 venv（`.venv/bin/python`）加一条隧道。

## 4. 接口协议

`POST /v1/systemone`，`content-type: application/json`。一次前向给出每个问题的概率，
**不生成自由文本、不需要解析输出**。

```json
{
  "model": "clef-flash",
  "state": "订单系统持续返回错误，所有客户无法提交订单。",
  "questions": {
    "department": {"type": "choice", "instructions": "应该由哪个团队处理？",
                   "criteria": {"billing": "账单或付款问题", "technical": "故障或系统错误"}},
    "outage": {"type": "noul", "instructions": "是否存在服务故障？"},
    "urgency": {"type": "score", "criteria": ["可以等待", "本周处理", "立即处理"]}
  }
}
```

| 题型 | 语义 | 返回字段 |
| --- | --- | --- |
| `noul` | 命题为真的概率 | `{"type":"noul","noul":0.9325}` |
| `choice` | 在 `criteria`（选项字典）里选一个 | `{"type":"choice","choice":"technical","confidence":0.9946,"probabilities":{...}}` |
| `score` | 在 `criteria`（有序数组）上加权打分 | `{"type":"score","score":1.9341,"confidence":0.9561,"legend":{...},"probabilities":{...}}` |

约束：`state` 为字符串（也接受 JSON 对象/数组）；`questions` 非空、键即问题 ID；
`choice` 必须有非空 `criteria` 对象，`score` 必须有非空 `criteria` 数组，`noul` 不需要 `criteria`；
`instructions` 可选。响应 `answers` 的键与请求 `questions` **完全一致**，另有 `usage.input_tokens`。

实测响应（2026-10-11，从 `qasystem_backend` 容器发出，中文三类问题 0.19 s）：

```json
{"model":"clef-flash",
 "answers":{
   "department":{"type":"choice","choice":"technical","confidence":0.9929,
                 "probabilities":{"billing":0.0071,"technical":0.9929}},
   "outage":{"type":"noul","noul":0.9047},
   "urgency":{"type":"score","score":1.9467,"confidence":0.9641,
              "legend":{"0":"可以等待","1":"本周处理","2":"立即处理"},
              "probabilities":{"0":0.0174,"1":0.0185,"2":0.9641}}},
 "usage":{"input_tokens":301,"output_tokens":0}}
```

## 5. 限制与错误码（2026-10-11 对运行中容器实测）

| 触发条件 | 当前上限 | HTTP | 返回 |
| --- | --- | --- | --- |
| 完整编码（state + 问题 + 选项模板）超长 | `CLEF_MAX_LENGTH=8192` tokens | 422 | `Complete input exceeds CLEF_MAX_LENGTH; shorten state or questions` |
| 请求体过大 | `CLEF_MAX_REQUEST_BYTES=65536` | 413 | `Request body too large` |
| 问题数超限 | `CLEF_MAX_QUESTIONS=16` | 422 | `Too many questions` |
| 单题 / 总选项数超限 | `CLEF_MAX_OPTIONS=32`、`CLEF_MAX_TOTAL_OPTIONS=128` | 422 | `Too many options` |
| 带 `images` / `videos` / `media_kwargs` | 本部署只收文本 | 422 | `Invalid text/JSON SystemOne request; images and videos are unsupported` |
| 队列满（单卡串行，最多等 4 个） | `CLEF_MAX_QUEUE_SIZE=4` | 429 | `Inference queue is full` + `Retry-After: 1` |
| 模型未就绪 / 显存不足 / 推理异常 | — | 503 | `Model not ready` / `Model memory capacity exceeded...` / `Model inference unavailable` |
| 启用 `CLEF_API_KEY` 后缺少或错误 Bearer | — | 401 | `Unauthorized` |

服务**不截断输入**：超长一律 422，需要调用方自己压缩 state 或拆分调用。
单卡 24GB、推理串行，不要为吞吐直接开大 `CLEF_MAX_LENGTH` 或多 worker。

判定失败**不等于“否定答案”**：v3.1 把超时、预算耗尽、无效响应记为 `status=unavailable`，
不会把失败当成“这条实体/关系不成立”。

## 6. 让 Backend 的抽取真正用上 Clef（v3.1）

服务在跑 ≠ 抽取在用。当前 Backend 是 `KG_EXTRACT_ALGO_VERSION=v2`，v2 不调用 Clef。
只有 **v3.1 语义 harness**（`algorithm/knowledge_extract/v3_1/decisions.py` 的 `ClefDecider`）会调用它。

需要改的配置（`/mnt/data3/qasys/.env`）：

```dotenv
KG_EXTRACT_ALGO_VERSION=v3.1
KG_EXTRACT_V31_CLEF_TIMEOUT=10          # 单次 Clef 调用超时
KG_EXTRACT_V31_CLEF_MAX_CALLS=512       # 每文档裁决调用预算
KG_EXTRACT_V31_REQUEST_TIMEOUT=120
KG_EXTRACT_V31_DOCUMENT_TIMEOUT=900
CLEF_SERVICE_URL=http://clef:8188       # 已配好，无需改
```

**改 Backend 前必读：**

- `docker compose restart` **不会**刷新容器环境变量，必须重建容器才注入新值。
- 88 的 `qasystem_backend` 不是只用 `docker-compose.yml` 建的，它还叠加了两个文件
  （见容器标签 `com.docker.compose.project.config_files`）：
  `/home/ycheng/ZhCHRAG/qasystem/tmp/qasys-backend-v2-siliconflow.yml`（其中把
  `KG_EXTRACT_ALGO_VERSION` 写死为 `v2`，compose `environment` 优先于 `.env`）
  与 `/mnt/data3/qasys/docker-compose.providers.yml`（引用权限 600 的 `extraction-providers.env`）。
  **直接 `docker compose up -d --force-recreate backend` 会丢掉这些 overlay**，请先与部署人（zjwei）确认
  原始 `-f` 组合，用同一组文件重建。
- 改了抽取行为要按仓库 `AGENTS.md` 的发布门禁验证（单测全绿 + 全量评测 + 与基线对比），
  不能只用一个 HTTP 调用证明可用。

## 7. 运维

```bash
cd /mnt/data3/qasys

docker compose ps clef                                  # 状态
docker compose up -d clef                               # 启动/更新（.env 已含 COMPOSE_PROFILES=decisions）
docker compose restart clef                             # 重启进程（不刷新 .env，改配置要用 --force-recreate）
docker compose up -d --force-recreate clef              # 改完 .env 后重建（clef 只用 docker-compose.yml，安全）
docker compose stop clef                                # 停止
docker logs --tail 100 -f qasystem_clef                 # 日志（启动约 20~35 s 加载权重，之后无输出）
nvidia-smi -i 2 --query-gpu=memory.used --format=csv    # 显存
```

- **换卡**：`nvidia-smi --query-gpu=index,uuid --format=csv` 查目标卡 UUID，写进 `.env` 的
  `CLEF_CUDA_VISIBLE_DEVICES`（**只允许一张卡**，不要写列表），再 `--force-recreate clef`。
  该变量在导入 Torch 前生效；其他机器不能沿用 88 的 UUID。
- **升级源码**：把仓库 `qasystem` 同步到 `/mnt/data3/qasys/qasystem` 后 `--force-recreate clef`
  （源码是只读挂载，无需重建镜像）。
- **开鉴权**：`.env` 填非空 `CLEF_API_KEY`，Backend 填同值 `CLEF_SERVICE_API_KEY`，重建两个容器。
  调用方用户的 Authorization 不会转发给模型服务。
- **调参**：`CLEF_MAX_LENGTH` / `CLEF_MAX_QUEUE_SIZE` 等都在 `.env` 的 `CLEF_*` 段（模板见仓库
  `.env.example`），改完必须重建容器。
- **不要**给服务加 `--workers > 1` 或 `--reload`，会重复加载模型。

## 8. 排障

| 现象 | 原因与处理 |
| --- | --- |
| 宿主机 `curl 127.0.0.1:8188` / `172.17.0.1:8188` 连接被拒 | 正常：容器没发布宿主端口。进容器查（第 2 节）或用容器 IP / SSH 隧道 |
| `docker compose ps clef` 没有 `qasystem_clef` | 没启用 `decisions` profile：确认 `.env` 里 `COMPOSE_PROFILES=decisions`，或 `docker compose --profile decisions up -d clef` |
| 容器 `Up` 但 `/health` 503 | 还在加载权重（`start_period` 120 s）或显存不足；看 `docker logs qasystem_clef` 与 `nvidia-smi -i 2` |
| 容器反复重启 | GPU 被占/换卡后 UUID 不对；日志里 `Clef model initialization failed` 表示权重或设备检查没通过 |
| Backend 报 `clef` 名称解析失败 | Backend 与 clef 不在同一 Compose 网络；确认两者都在 `qasys` 项目内 |
| 422 `CLEF_MAX_LENGTH` | 输入太长，服务不截断：减少问题/选项数或压缩 state |
| 429 | 并发过高，按 `Retry-After` 重试或降并发（单卡串行） |
| 客户端抛 `configuration` | `CLEF_SERVICE_URL` 为空或非法；88 上应为 `http://clef:8188` |
| Backend 日志里完全没有 `clef` 字样 | 抽取版本不是 v3.1（当前是 v2），服务没被调用，属预期；见第 6 节 |
| 旧的 `systemctl --user status clef-flash.service` 是 `inactive (dead)` | 已迁移到容器部署，属预期；不要再启动宿主服务（会与容器抢卡 2） |

## 9. 相关文档

- 部署方式与 GPU 分配：[决策模型部署](决策模型部署.md)
- 历史宿主部署（含权重下载与 venv 细节，地址已失效）：[Clef-flash 独立服务](Clef-flash独立服务.md)
- 服务自身说明：[clef_service/README.md](../../clef_service/README.md)
- HTTP 客户端与错误分类：[external_services/clef.py](../../external_services/clef.py)
- 环境变量模板：[.env.example](../../.env.example) 的 `CLEF_SERVICE_*` / `CLEF_*` 两段
- v3.1 分工与预算：[知识抽取 V3.1 语义 Harness 与评测](../算法/知识抽取V3.1语义Harness与评测.md)、[知识抽取 Clef Harness 设计](../算法/知识抽取ClefHarness设计.md)
