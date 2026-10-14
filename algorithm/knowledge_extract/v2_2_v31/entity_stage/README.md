# v2.2-v31 Entity Stage（内嵌 v3.1 实体算法副本）

本目录是 **从 `v3_1` 同步的实体/属性/Skill 抽取副本**，算法与 live `v3_1` 对齐，
但 **运行时不 import `v3_1`**。

- 入口：`extract_split`（只产出实体；关系由外层 v2.2-v31 接续）
- Skill：本目录 `skills/`
- 同步：`v3_1` 实体侧有行为变更时，应重拷/对齐本树，避免漂移

外层编排见 `../v31_entity_bridge.py`。
