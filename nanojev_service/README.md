# NanoJev 模型服务

使用 `qasystem-env-v1`，由ops统一管理常驻进程。`runtime-config/config.json.models`记录 `engine=native`、`adapter=nanojev`、`manager=ops`、相对权重路径、显式CPU/GPU选择和引擎参数。打开UI发现目录不会启用模型；人工审核保存后通过ops部署。

权重、backbone配置、tokenizer和官方预测/训练脚本来自只读 `/models`，启动前检查完整性。内部端口固定8000、GPU内部设备从绑定推导。HTTP协议共用Clef的SystemOne输入、队列限制与错误处理，服务模型名为nanojev，加载并预热后健康。

详情见 [决策模型部署](../docs/部署/决策模型部署.md)和[模型服务路由](../docs/接口/模型服务路由.md)。旧模型专属目录/device/端口变量只供UI迁移；服务部署成功不意味着领域JEV判别开关已经启用。
