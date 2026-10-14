# Clef-flash 模型服务

由 `ops config` 登记和勾选模型、人工审核后通过 `ops deploy` 启动。使用统一 `qasystem-env-v1`，源码和权重分别只读挂载；目录、设备、端口和原生参数统一保存在 `runtime-config/config.json.models`，不再通过 `CLEF_MODEL_DIR` 等独立启动变量配置。

记录使用 `engine=native`、`adapter=clef`、`manager=ops`。容器内固定8000端口，GPU隔离后内部设备为cuda:0，CPU需要显式选择。官方 `joint_schema_model.py`、模型及processor文件均来自外置完整release，启动时检查文件、加载和预热，不从网络下载。

HTTP接口仍为 `POST /v1/systemone`；choice/noul/score协议和队列限制保留。健康接口为 `/health`，加载成功才ready，模型失败不会切换CPU或其他服务。配置、GPU、迁移和认证操作见 [决策模型部署](../docs/部署/决策模型部署.md)，调用选择见 [模型服务路由](../docs/接口/模型服务路由.md)。
