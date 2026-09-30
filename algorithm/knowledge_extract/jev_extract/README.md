# jev_extract

`latest` 在提交 `e9d3015` 的整包副本。默认抽取仍是 `latest`。只有 `KG_EXTRACT_ALGO_VERSION=jev` 会进入本包。

`KG_JEV_ENABLED=0` 时不附加 `jev` 字段，抽取事实与分叉时的 `latest` 相同。任务开关打开后只增加影子报告，不合并实体、不删除关系。

契约和后续微调见 [JEV 决策层](../../../docs/算法/JEV决策层.md)。不要把本目录同步回 `latest`，除非另一次明确的版本替换。
