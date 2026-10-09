# Clef-flash 独立决策服务

Clef-flash 使用独立宿主机 Python venv 和 HTTP 接口。Backend 继续使用原来的 Docling、MinerU、Sentence Transformers 与 Transformers 4.x；`external_services.clef.ClefFlashClient` 只依赖 HTTP，不导入模型运行库。新服务不自动替换现有知识抽取或 NanoJev 流程。

## 88 服务器布局

| 项目 | 路径或地址 |
| --- | --- |
| 权重 | `/mnt/data3/qasys/models/clef-flash` |
| 服务源码 | `/mnt/data3/qasys/services/clef-flash/source/clef_service` |
| 独立解释器 | `/mnt/data3/qasys/services/clef-flash/venv/bin/python` |
| 服务配置 | `/home/ycheng/.config/qasystem/clef-flash.env` |
| 用户级 systemd | `clef-flash.service` |
| 宿主机监听 | `172.17.0.1:8188`，仅 Docker 宿主网关地址 |
| Backend 容器访问 | `http://host.docker.internal:8188` |
| GPU | 88 的 GPU 2，通过 UUID 绑定；Backend 的 `GPU_DEVICE_IDS=4,5,6,7` 保留 |

模型固定 revision 为 `17f0b0ad64efb65d273590632833508766b2aae6`。17 个原始文件共 19,083,377,402 bytes；下载校验清单在模型目录 `.download-manifest.json`。加载使用离线模式，模型源码和权重只读取，缓存不写入模型目录。

## 环境与启动

依赖契约在 `clef_service/requirements.txt`：Torch 2.10.0、torchvision 0.25.0、Transformers 5.10.2 等。仅安装到独立 venv，不能在 Backend 容器中执行该安装。部署变量的模板统一在根 `.env.example` 的 `CLEF_*` 段；服务实例把该段所需值写入自己的 systemd EnvironmentFile。

```bash
python3 -m venv /mnt/data3/qasys/services/clef-flash/venv
/mnt/data3/qasys/services/clef-flash/venv/bin/python -m pip install \
  -r clef_service/requirements.txt \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
/mnt/data3/qasys/services/clef-flash/venv/bin/python -m pip check
```

把 `clef_service/` 复制到上表服务源码目录。实例配置至少填写 `CLEF_SOURCE_DIR`、`CLEF_PYTHON`、`CLEF_MODEL_DIR`、`CLEF_BIND`、`CLEF_PORT`、`CLEF_CUDA_VISIBLE_DEVICES`。88 的绑定地址为 `172.17.0.1`；GPU 2 的 UUID 为 `GPU-c10aaf4f-7c2c-3fa5-f47d-bb26db738a49`。在其他机器上必须重新查询 `nvidia-smi`，按资源分配选择设备，不能沿用 88 的 UUID。

```bash
mkdir -p ~/.config/systemd/user ~/.config/qasystem
chmod 600 ~/.config/qasystem/clef-flash.env
cp docker/clef-flash.service ~/.config/systemd/user/clef-flash.service
systemctl --user daemon-reload
systemctl --user enable --now clef-flash.service
systemctl --user status clef-flash.service --no-pager
journalctl --user -u clef-flash.service -n 50 --no-pager
```

88 的 `ycheng` 已启用 linger。其他主机需要管理员配置 `loginctl enable-linger <user>`，才能保证退出登录后及重启时运行用户服务。systemd 在异常退出后重启服务；一个进程加载一个模型，不能增加 uvicorn workers 数量。

```bash
systemctl --user restart clef-flash.service
systemctl --user stop clef-flash.service
curl --noproxy '*' http://172.17.0.1:8188/health
```

服务仅绑定 Docker 网关时不对服务器 LAN 地址开放。当前内部部署未设置 API key；如扩展访问范围，可配置服务的 `CLEF_API_KEY`，并为 Backend 设置同值的 `CLEF_SERVICE_API_KEY`。调用方用户的 Authorization 不会转发给模型服务。业务任务仍由现有执行队列调度，不把服务密钥写入任务数据。

## 接口与 Backend 调用

`GET /health` 在模型加载成功后返回 `{"status":"ready","model":"clef-flash"}`。

`POST /v1/systemone` 支持文本或 JSON 的 `state`，以及 `noul`（真假概率）、`choice`（候选分类）、`score`（有序评分）问题。服务返回官方 `model`、`answers`、`usage` 结构。此版本明确拒绝图片和视频，不接受外部媒体 URL。

Backend 中可以直接指定地址，不需要重启现有进程来注入新环境变量：

```python
from external_services.clef import ClefFlashClient

client = ClefFlashClient(base_url="http://host.docker.internal:8188")
result = client.systemone({
    "state": "订单系统持续返回错误，所有客户无法提交订单。",
    "questions": {
        "department": {
            "type": "choice",
            "instructions": "应该由哪个团队处理？",
            "criteria": {"billing": "账单或付款问题", "technical": "故障或系统错误"},
        },
        "outage": {"type": "noul", "instructions": "是否存在服务故障？"},
        "urgency": {"type": "score", "criteria": ["可以等待", "本周处理", "立即处理"]},
    },
})
print(result["answers"])
```

采用环境变量时设置 `CLEF_SERVICE_URL=http://host.docker.internal:8188`，然后调用 `ClefFlashClient()`。Compose 的 `.env` 修改只有在容器重新创建后才会注入当前进程；普通 `docker compose restart` 不刷新容器环境。此部署使用显式地址从现有 Backend 容器验证调用，不为注入可选配置而重新创建 Backend。

客户端端点统一注册在 `external_services/application.py`。默认请求超时为 120 秒，设置 `trust_env=False`，以免宿主机调用进入 Backend 的外网代理。

## 资源限制和错误

默认完整编码输入最多 2048 tokens，最大请求 64 KiB，最多 16 个问题、每题 32 个选项、总计 128 个选项。输入包括状态、问题描述、候选项和模型模板，不能只按状态字数估算。超限返回 422 或 413，不会静默截断状态。

模型推理串行执行，最多等待 4 个请求；排队已满返回 429。显存不足或推理异常返回 503；响应和应用日志不回显请求正文。所有 `CLEF_MAX_*` 上限可由配置调整，但应先实测 24GB 显存，不应直接开启 16K 输入或多 workers。

## 验证

单元测试（不加载真实模型）：

```bash
.venv/bin/python -m pytest -q \
  tests/backend/test_clef_service.py \
  tests/backend/test_clef_client.py \
  tests/backend/test_application_service.py \
  tests/backend/test_remote_runtime.py
```

上线验证应覆盖服务健康、中文与英文的三类问题、超长输入拒绝、Backend 容器内 HTTP 调用，以及 Backend 原环境的 `pip check` 和健康检查。这个接口验证不等于知识抽取质量评测；将来若接入抽取或解析流程，需执行仓库完整发布门禁。

### 2026-10-08 实测记录

服务已在 88 的独立 venv 启动，`clef-flash.service` 已设置随用户服务管理器启动；模型初始化约 10 秒，加载后 GPU 2 占用约 18.1 GiB。未安装 FLA/causal-conv1d 加速库，Qwen3.5 使用 Torch 回退实现。

以下是现有 Backend 容器通过 `ClefFlashClient(base_url=...)` 实际 HTTP 调用的单次结果，耗时包含客户端与 HTTP 开销，不代表压力测试或质量基线：

| 请求 | 输入 tokens | 耗时 | 结果 |
| --- | ---: | ---: | --- |
| 英文故障描述，三类问题，首次请求 | 300 | 0.712 秒 | technical 概率 0.9612，outage 0.8384 |
| 中文订单故障，三类问题 | 301 | 0.190 秒 | technical 概率 0.9946，outage 0.9325 |
| 中文正常运行，真假判断 | 152 | 0.163 秒 | outage 0.0057 |
| 长文本真假判断 | 1946 | 0.741 秒 | 有效概率结果，无显存错误 |

完整编码超限返回 422，带 images 字段返回 422，请求超过 64 KiB 返回 413；之后健康检查仍正常。相关单元测试 78 项通过。

Backend 保持 Transformers 4.57.6，原依赖 `pip check` 通过，`/health` 返回 `{"status":"ok"}`。服务代码和轻量客户端已部署；没有更改现有抽取默认 provider。88 根部署 `.env` 对当前 SSH 用户不可写，未修改该文件；当前调用请显式传 `base_url`，后续如采用环境变量配置，由该文件维护者设置并在计划的容器重建中生效。

独立 venv 的完整已安装版本记录在 `/mnt/data3/qasys/services/clef-flash/installed-requirements.txt`，安装日志在同目录 `logs/install.log`，服务运行日志由 `journalctl --user -u clef-flash.service` 查看。
