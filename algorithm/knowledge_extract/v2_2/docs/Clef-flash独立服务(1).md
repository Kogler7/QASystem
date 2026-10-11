# Clef-flash 独立决策服务（88 配置与使用）

Clef-flash 是 Cloudflare 发布的 9B 决策模型（`Qwen/Qwen3.5-9B` 后训练 + joint schema head）：
输入一段 state（文本或 JSON，官方也支持图片/视频）和一组带类型的 questions，一次前向就给出
每个问题所有选项的概率，**不生成自由文本、不需要解析输出**。问题类型只有三种：

| 类型 | 语义 | 返回 |
| --- | --- | --- |
| `noul` | 该命题为真的概率（0~1） | `{"type":"noul","noul":0.9325}` |
| `choice` | 在 `criteria`（选项字典）里选一个 | `{"type":"choice","choice":"technical","confidence":0.9946,"probabilities":{...}}` |
| `score` | 在 `criteria`（有序数组）上加权打分 | `{"type":"score","score":1.9341,"confidence":0.9561,"legend":{...},"probabilities":{...}}` |

本仓库在 88 上把它作为**独立宿主机服务**运行：独立 venv、独立 GPU、只暴露 HTTP。
Backend 侧只通过 `external_services.clef.ClefFlashClient` 走 HTTP，不导入 Torch 与模型代码；
服务不替换任何现有抽取流程，当前只有 **v3.1 语义 harness** 会调用它。

## 1. 88 当前状态（2026-10-09 实测）

| 项目 | 值 |
| --- | --- |
| 服务 | `clef-flash.service`（用户级 systemd），`active (running)`，`enabled`，`Linger=yes` |
| 启动时间 | 2026-10-07 23:41:43 EDT（连续运行中） |
| 主进程 | PID 1088535，`/mnt/data3/qasys/services/clef-flash/venv/bin/python -m clef_service` |
| 监听 | `172.17.0.1:8188`（只绑 Docker 网关，服务器 LAN 地址访问不到） |
| 容器内访问 | `http://host.docker.internal:8188`（需 `--add-host host.docker.internal:host-gateway`） |
| 健康检查 | `curl --noproxy '*' http://172.17.0.1:8188/health` → `{"status":"ready","model":"clef-flash"}` |
| 鉴权 | `CLEF_API_KEY` 为空 → `/v1/systemone` 免认证；`/health` 永远免认证 |
| GPU | 卡 2，按 UUID `GPU-c10aaf4f-7c2c-3fa5-f47d-bb26db738a49` 绑定；实测占用约 19.7 GiB |
| 内存 | 常驻约 1.5 GiB |
| 生产 Backend | `qasystem_backend` 未设置任何 `CLEF_*`，当前不调用 Clef（默认抽取版本是 v2） |
| 调用方 | 88 上的 v3.1 评测 Backend（`minieval-v31-backend`）、以及手工/脚本调用 |

## 2. 目录与配置

| 项目 | 路径 |
| --- | --- |
| 权重 | `/mnt/data3/qasys/models/clef-flash` |
| 服务源码 | `/mnt/data3/qasys/services/clef-flash/source/clef_service` |
| 独立解释器 | `/mnt/data3/qasys/services/clef-flash/venv/bin/python` |
| 环境文件 | `/home/ycheng/.config/qasystem/clef-flash.env`（权限 600） |
| systemd 单元 | `/home/ycheng/.config/systemd/user/clef-flash.service`（仓库模板 `docker/clef-flash.service`） |
| 依赖清单 | `clef_service/requirements.txt`（仓库）；已装版本 `.../services/clef-flash/installed-requirements.txt` |
| 安装日志 / 备份 | `.../services/clef-flash/logs/install.log`、`.../deployment-backups/20261007-210920` |
| 验证记录 | `.../services/clef-flash/verification.json` |

权重来自 `Cloudflare/clef-flash`，固定 revision `17f0b0ad64efb65d273590632833508766b2aae6`：
17 个文件、19,083,377,402 bytes（4 个 backbone 分片 + `joint_head.safetensors` +
`joint_schema_model.py` 等），下载校验清单 `.download-manifest.json` 为 `complete`，
license Apache-2.0。加载强制离线（`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`），
模型目录只读、缓存写到 `services/clef-flash/cache`。

环境文件现状（`CLEF_API_KEY` 为空表示不鉴权）：

```ini
CLEF_SOURCE_DIR=/mnt/data3/qasys/services/clef-flash/source
CLEF_PYTHON=/mnt/data3/qasys/services/clef-flash/venv/bin/python
CLEF_MODEL_DIR=/mnt/data3/qasys/models/clef-flash
CLEF_BIND=172.17.0.1
CLEF_PORT=8188
CLEF_CUDA_VISIBLE_DEVICES=GPU-c10aaf4f-7c2c-3fa5-f47d-bb26db738a49
CLEF_DEVICE=cuda:0
CLEF_DTYPE=bfloat16
CLEF_MAX_LENGTH=2048
CLEF_MAX_REQUEST_BYTES=65536
CLEF_MAX_QUESTIONS=16
CLEF_MAX_OPTIONS=32
CLEF_MAX_TOTAL_OPTIONS=128
CLEF_MAX_QUEUE_SIZE=4
CLEF_API_KEY=
OMP_NUM_THREADS=4
HF_HOME=/mnt/data3/qasys/services/clef-flash/cache
TORCH_HOME=/mnt/data3/qasys/services/clef-flash/cache/torch
```

变量含义（模板统一在仓库根 `.env.example` 的 `CLEF_*` 段，分两半：上半是**调用方** Backend
读的 `CLEF_SERVICE_*`，下半是**服务自己**读的 `CLEF_*`）：

| 变量 | 作用 | 备注 |
| --- | --- | --- |
| `CLEF_SOURCE_DIR` / `CLEF_PYTHON` | 源码根与解释器，单元里 `cd "$CLEF_SOURCE_DIR" && exec "$CLEF_PYTHON" -m clef_service` | 必须独立 venv |
| `CLEF_MODEL_DIR` | 本地模型目录 | 不下载、不写回 |
| `CLEF_BIND` / `CLEF_PORT` | 监听地址与端口 | 88 用 Docker 网关地址 |
| `CLEF_CUDA_VISIBLE_DEVICES` | 物理卡号或 GPU UUID | 在导入 Torch 前生效；换机器必须重新查 |
| `CLEF_DEVICE` | 进程内设备 | 配合上面用 `cuda:0` 即可 |
| `CLEF_DTYPE` | `bfloat16` / `float16` / `float32` | 24GB 卡用 bfloat16 |
| `CLEF_MAX_*` | 输入与队列上限 | 见第 6 节 |
| `CLEF_API_KEY` | 非空则要求 `Authorization: Bearer <key>` | 须与 Backend 的 `CLEF_SERVICE_API_KEY` 相同 |

## 3. 启停、日志与运维

```bash
# 状态 / 启动 / 停止 / 重启（用户级 systemd，不需要 root）
systemctl --user status clef-flash.service --no-pager -n 30
systemctl --user start   clef-flash.service
systemctl --user stop    clef-flash.service
systemctl --user restart clef-flash.service      # 改完 clef-flash.env 必须重启才生效
systemctl --user is-active clef-flash.service; systemctl --user is-enabled clef-flash.service

# 健康检查（注意 --noproxy，避免走宿主机代理）
curl --noproxy '*' http://172.17.0.1:8188/health
nvidia-smi -i 2 --query-gpu=index,memory.used --format=csv      # 显存占用
```

- **日志**：`systemctl --user status ... -n 30` 能看到最近输出。88 上 `journalctl --user -u
  clef-flash.service` 返回 `-- No entries --`（当前用户不在 `adm` / `systemd-journal` 组，
  完整日志要用 `sudo journalctl`，需要密码）。要长期留存可在单元里加
  `StandardOutput=append:/mnt/data3/qasys/services/clef-flash/logs/service.log` 后
  `daemon-reload` + `restart`。
- **重启代价**：一个进程加载一个模型，启动到 `/health` 返回 `ready` 约 10 秒（2026-10-08 首部署实测），
  期间调用方会拿到 503。**不要**加 `--workers > 1` 或 `--reload`，会重复加载模型。
- **换卡**：`nvidia-smi --query-gpu=index,uuid --format=csv` 查目标卡 UUID，写进
  `CLEF_CUDA_VISIBLE_DEVICES`，`systemctl --user restart clef-flash.service`。
  其他机器不能沿用 88 的 UUID。
- **开鉴权**：环境文件填 `CLEF_API_KEY=<key>`，重启服务；同时给调用方 Backend 设置同值的
  `CLEF_SERVICE_API_KEY`。调用方用户的 Authorization **不会**转发给模型服务。
- **升级源码**：把仓库 `clef_service/` 覆盖到 `CLEF_SOURCE_DIR/clef_service`，重启并跑一次
  第 8 节的验证。升级依赖用独立 venv：
  `/mnt/data3/qasys/services/clef-flash/venv/bin/python -m pip install -r clef_service/requirements.txt`。
- **开机自启**：88 的 `ycheng` 已启用 `linger`，用户服务随系统启动；换主机需要管理员
  `loginctl enable-linger <user>`。

## 4. 调用方法

### 4.1 协议

`POST /v1/systemone`，`content-type: application/json`：

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

约束：`state` 是字符串（也可直接给 JSON 对象/数组）；`questions` 非空，键即问题 ID；
`choice` 必须有非空 `criteria` 对象，`score` 必须有非空 `criteria` 数组，`noul` 不需要 `criteria`；
`instructions` 可选。响应 `answers` 的键与请求 `questions` 完全一致，另有 `usage.input_tokens`。

### 4.2 命令行实测（2026-10-09，88 上直接跑）

```bash
curl -sS --noproxy '*' http://172.17.0.1:8188/v1/systemone \
  -H 'content-type: application/json' -d '{
    "state":"订单系统持续返回错误，所有客户无法提交订单。",
    "questions":{
      "department":{"type":"choice","criteria":{"billing":"账单或付款问题","technical":"故障或系统错误"}},
      "outage":{"type":"noul","instructions":"是否存在服务故障？"},
      "urgency":{"type":"score","criteria":["可以等待","本周处理","立即处理"]}}}'
```

实际返回（耗时 0.17 秒，`input_tokens=301`）：

```json
{"model":"clef-flash",
 "answers":{
   "department":{"type":"choice","choice":"technical","confidence":0.9946,
                 "probabilities":{"billing":0.0054,"technical":0.9946}},
   "outage":{"type":"noul","noul":0.9325},
   "urgency":{"type":"score","score":1.9341,"confidence":0.9561,
              "legend":{"0":"可以等待","1":"本周处理","2":"立即处理"},
              "probabilities":{"0":0.022,"1":0.022,"2":0.9561}}},
 "usage":{"input_tokens":301,"output_tokens":0}}
```

### 4.3 仓库客户端（Backend / 算法代码里用这个）

```python
from external_services.clef import ClefFlashClient, ClefServiceError

client = ClefFlashClient(base_url="http://host.docker.internal:8188")   # 或读 CLEF_SERVICE_URL
try:
    result = client.systemone({
        "state": "...",
        "questions": {"outage": {"type": "noul", "instructions": "是否存在服务故障？"}},
    }, timeout=10)                       # 覆盖单次超时，v3.1 用 10 秒
    print(result["answers"])
except ClefServiceError as exc:
    print(exc.error_kind, exc.status_code, exc.retryable)   # 不打印请求/响应正文
```

客户端只依赖 `httpx`，`trust_env=False`（不走宿主机代理、不继承用户 Authorization），
并把失败分类成 `configuration` / `input_too_long` / `schema_error` / `rate_limited` /
`service_unavailable` / `timeout` / `connection` / `invalid_response` / `http_error`，
`retryable` 标记 429/503/超时/连接类。响应会做结构校验（`model`、`usage`、`answers` 键集合、
选项值范围），不合法一律抛 `invalid_response`，不会把坏响应当成有效决策。

相关调用方环境变量（Backend 侧）：`CLEF_SERVICE_URL`（88 用 `http://host.docker.internal:8188`，
留空则调用显式失败）、`CLEF_SERVICE_PATH`（默认 `/v1/systemone`，注册在
`external_services/application.py` 的 `models.decisions.create`）、`CLEF_MODEL`（默认 `clef-flash`）、
`CLEF_SERVICE_API_KEY`、`CLEF_SERVICE_TIMEOUT_SECONDS`（默认 120）。

> 改 Compose 的 `.env` 后**只有重建容器**才会注入新环境变量，`docker compose restart` 不会刷新。
> 评测套件里改 `config.sh` 后用 `bash scripts/start_backends.sh --force v3.1` 重建 v3.1 Backend。

### 4.4 从容器里调用

```bash
docker exec minieval-v31-backend python -c "
import os,json,urllib.request
url=os.environ['CLEF_SERVICE_URL'].rstrip('/')+'/v1/systemone'
body=json.dumps({'state':'订单系统持续返回错误。','questions':{'outage':{'type':'noul'}}}).encode()
req=urllib.request.Request(url,data=body,headers={'content-type':'application/json'})
print(json.load(urllib.request.urlopen(req,timeout=30))['answers'])"
# {'outage': {'type': 'noul', 'noul': 0.9482}}
```

容器要能解析 `host.docker.internal`，启动时加 `--add-host host.docker.internal:host-gateway`
（生产 Compose 与 `mini-eval-deepseek` 套件都已加）。

## 5. 谁在用它

| 调用方 | 位置 | 用法与预算 |
| --- | --- | --- |
| v3.1 语义 harness | `algorithm/knowledge_extract/v3_1/decisions.py`（`ClefDecider`） | 抽取过程中把实体/关系判定打包成 Clef 问题；单次超时 `KG_EXTRACT_V31_CLEF_TIMEOUT=10` 秒，总调用预算 `KG_EXTRACT_V31_CLEF_MAX_CALLS=128`，另有 `KG_EXTRACT_V31_REQUEST_TIMEOUT=120`、`KG_EXTRACT_V31_DOCUMENT_TIMEOUT=900` |
| mini eval 套件 | `/mnt/data3/qasys/mini-eval-deepseek` | `config.sh` 注入 `CLEF_SERVICE_URL=http://host.docker.internal:8188`、`CLEF_MODEL=clef-flash` 到三套 Backend（只有 v3.1 真用）；`scripts/status.sh` 会检查 `172.17.0.1:8188/health` |
| 生产 Backend | `qasystem_backend` | 未配置 `CLEF_*`，不调用；要用需在部署 `.env` 里加 `CLEF_SERVICE_URL` 并重建容器 |

**服务不可用不等于「否定答案」**：v3.1 把超时、预算耗尽、无效响应等一律记为
`status=unavailable`（附 `deadline_exhausted` / `call_budget_exhausted` / `invalid_response` 等原因），
不会把失败当成「这条实体/关系不成立」。所以 Clef 挂掉时 v3.1 会退化而不是给出错误的负例。

## 6. 资源限制与错误码（实测）

| 触发条件 | 上限（88 当前） | HTTP | 返回 |
| --- | --- | --- | --- |
| 完整编码（state+问题+选项模板）超长 | `CLEF_MAX_LENGTH=2048` tokens | 422 | `Complete input exceeds CLEF_MAX_LENGTH; shorten state or questions` |
| 请求体过大 | `CLEF_MAX_REQUEST_BYTES=65536` | 413 | `Request body too large` |
| 问题数超限 | `CLEF_MAX_QUESTIONS=16` | 422 | `Too many questions` |
| 单题选项数 / 总选项数超限 | `CLEF_MAX_OPTIONS=32`、`CLEF_MAX_TOTAL_OPTIONS=128` | 422 | `Too many options` |
| 带 `images` / `videos` / `media_kwargs` | 本部署只收文本 | 422 | `Invalid text/JSON SystemOne request; images and videos are unsupported` |
| 推理队列满（单卡串行，最多等 4 个） | `CLEF_MAX_QUEUE_SIZE=4` | 429 | `Inference queue is full` + `Retry-After: 1` |
| 模型未就绪 / 显存不足 / 推理异常 | — | 503 | `Model not ready` / `Model memory capacity exceeded...` / `Model inference unavailable` |
| 启用 `CLEF_API_KEY` 后缺少或错误 Bearer | — | 401 | `Unauthorized` |

实测复核：`images` 字段 422、33 个选项 422、240 KB 请求体 413、JSON 对象 `state` 200、
三类问题一次调用 200（0.17 秒 / 301 input tokens）。响应和应用日志都不回显请求正文，
错误里也不带 URL 与凭据。

显存只有一张 24GB 卡、推理串行：**不要**为了吞吐直接开 16K 输入或多 worker，先实测；
输入按「状态 + 问题描述 + 候选项 + 模型模板」总长估算，不能只看 state 字数。

## 7. 排障

| 现象 | 原因与处理 |
| --- | --- |
| `curl 127.0.0.1:8188` 连接被拒 | 服务只绑 `172.17.0.1`（Docker 网关）。宿主机用 `172.17.0.1`，容器用 `host.docker.internal` |
| 容器内连接失败 | 缺 `--add-host host.docker.internal:host-gateway`，或容器网络到不了宿主网关 |
| `/health` 返回 503 | 模型还在加载（约 10 秒）或显存不足；看 `systemctl --user status -n 30` 与 `nvidia-smi -i 2` |
| 422 且 detail 提到 `CLEF_MAX_LENGTH` | 输入太长：减少问题数/选项数或压缩 state，服务不截断输入 |
| 422 `Too many options` | 单题 > 32 或总数 > 128 个选项，拆成多次调用 |
| 429 | 并发太高，按 `Retry-After` 重试或降低调用并发 |
| 客户端抛 `configuration` | `CLEF_SERVICE_URL` 为空或 URL 非法；88 上应为 `http://host.docker.internal:8188` |
| 客户端抛 `invalid_response` | 服务返回结构不合法（版本不匹配/被代理改写），检查是否直连、是否升级过 mismatch 的源码 |
| `journalctl --user` 没内容 | 88 的权限限制，见第 3 节；用 `systemctl --user status` 或加 `StandardOutput=append:` |
| 换了卡后启动报显存/设备错 | `CLEF_CUDA_VISIBLE_DEVICES` 里的 UUID 是 88 GPU 2 的，换卡必须重新查 |

## 8. 验证

不加载真实模型的单元测试（在仓库根目录）：

```bash
.venv/bin/python -m pytest -q \
  tests/backend/test_clef_service.py \
  tests/backend/test_clef_client.py \
  tests/backend/test_application_service.py \
  tests/backend/test_remote_runtime.py
```

上线验证清单：`/health` 返回 ready → 中文与英文的三类问题各一次 → 超长输入 422、
带 images 422、超大请求体 413 → 从**目标 Backend 容器内**发一次真实 HTTP 调用 →
之后 `/health` 仍 ready。这个接口验证**不等于**知识抽取质量评测；接入抽取/解析流程后
仍要走仓库完整发布门禁。

### 2026-10-09 复核

服务连续运行中（`active` 自 2026-10-07 23:41:43 EDT），`/health` = ready，GPU 2 占用 19.7 GiB；
宿主机与 `minieval-v31-backend` 容器内调用均 200；三类问题返回见 4.2/4.4；
`images` 422、33 选项 422、240 KB 请求体 413、JSON 对象 state 200。
`CLEF_API_KEY` 为空，`/v1/systemone` 免认证——只绑 Docker 网关，未对 LAN 开放。
第 8 节四组单元测试本轮复跑 **91 项通过**（0.27 秒，不含真实模型）。

### 2026-10-08 首部署记录

服务在 88 独立 venv 启动并设为随用户服务管理器启动；模型初始化约 10 秒，加载后 GPU 2 占用
约 18.1 GiB。未装 FLA/causal-conv1d 加速库，Qwen3.5 走 Torch 回退实现。
Backend 容器经 `ClefFlashClient(base_url=...)` 实测：英文三类问题 300 tokens / 0.712 秒、
中文 301 tokens / 0.190 秒、中文真假判断 152 tokens / 0.163 秒、长文本 1946 tokens / 0.741 秒；
超长输入 422、images 422、>64 KiB 413；相关单元测试 78 项通过（`verification.json`）。
当时 Backend 保持 Transformers 4.57.6、`pip check` 通过。

## 9. 相关文档

- 服务自身说明与运行回归：[clef_service/README.md](../../clef_service/README.md)
- 轻量 HTTP 客户端与错误分类：[external_services/clef.py](../../external_services/clef.py)
- 环境变量模板：[.env.example](../../.env.example) 的 `CLEF_SERVICE_*` / `CLEF_*` 两段
- v3.1 分工与预算：[知识抽取 V3.1 语义 Harness 与评测](../算法/知识抽取V3.1语义Harness与评测.md)、[知识抽取 Clef Harness 设计](../算法/知识抽取ClefHarness设计.md)
- 88 上的 v2/v3/v3.1 mini eval 套件：`/mnt/data3/qasys/mini-eval-deepseek/README.md`
