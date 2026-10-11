# Clef-flash 独立服务

`qasystem-env-v1` 中与 Backend、NanoJev 共用 Python、Torch 与 Transformers，不重复安装 CUDA 依赖。
通过 Compose 的 `decisions` profile 常驻并预热，详见 [决策模型部署](../docs/部署/决策模型部署.md)。
也可在宿主机独立 venv 中运行。权重和官方 `joint_schema_model.py` 由 `CLEF_MODEL_DIR` 指向外置的本地
模型目录；服务强制离线加载，不下载模型，不读取请求中的 URL，不写入模型目录。

配置项集中在仓库根 `.env.example` 的 `CLEF_*` 段。启动前由进程管理器加载环境变量，
然后在源码根目录运行：

```sh
python -m clef_service
# 或者（必须单 worker）
uvicorn clef_service.app:app --host 127.0.0.1 --port 8188 --workers 1 --no-access-log
```

`python -m clef_service` 读取 `CLEF_BIND`、`CLEF_PORT`，也接受 `--host` 和 `--port`。
`CLEF_CUDA_VISIBLE_DEVICES` 在导入 Torch 前应用；若用 GPU UUID 隔离，`CLEF_DEVICE=cuda:0`
指的是该进程可见的第一张卡。不要使用多个 workers 或 `--reload`，否则会重复加载模型。

- `GET /health`：未认证的就绪检查，只返回模型名和状态。
- `POST /v1/systemone`：兼容官方文本/JSON `model`、`state`、`questions` 请求。
  问题类型支持 `noul`、`choice`、`score`；拒绝 `images`、`videos`、`media_kwargs`。
- 可选 `CLEF_API_KEY` 启用 `Authorization: Bearer <key>`。密钥只通过环境传入。
- 完整编码超过 `CLEF_MAX_LENGTH` 返回 422，不截断输入；请求体超限返回 413，
  等待队列满返回 429，GPU 显存不足或推理失败返回 503。单 GPU 推理串行执行。

请求示例：

```json
{
  "model": "clef-flash",
  "state": "Our checkout started returning errors and orders are blocked.",
  "questions": {
    "outage": {"type": "noul", "instructions": "Is a service down?"},
    "department": {"type": "choice", "criteria": {"billing": "Payments", "technical": "Bugs"}},
    "urgency": {"type": "score", "criteria": ["Can wait", "Today"]}
  }
}
```

回归测试不加载 Torch 或真实模型：

```sh
python -m pytest -q tests/backend/test_clef_service.py
```
