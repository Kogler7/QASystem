# NanoJev 常驻推理服务

与 Clef、Backend 共用 `qasystem-env-v1` 内的 Python 环境，启动时加载一次模型并预热，
完成后 `/health` 才就绪。单 worker、有界串行推理队列，复用 Clef 的限流、鉴权与错误脱敏。
权重由 `NANOJEV_MODEL_DIR` 指向只读外置目录；源码不打入镜像。

运行入口为 `python -m nanojev_service`。配置集中在根 `.env.example`；
部署和 GPU 分配见 [决策模型部署](../docs/部署/决策模型部署.md)。

`POST /v1/systemone` 使用 `model=nanojev`，支持 `state + questions`。
题型为 `choice`、`noul`、`score`；内部将 `noul` 对应到 checkpoint 的 boolean 题型。
Backend 设置 `NANOJEV_SERVICE_URL` 后共享该服务，不在各 GPU worker 中重复加载模型。
