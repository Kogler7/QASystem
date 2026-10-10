# 入图对齐

比较新抽取节点和调用方传入的已有节点，标出 `link`、`conflict` 或 `new`。只产出注解。

默认沉淀不调用本包。`KG_EXTRACT_ALGO_VERSION=jev` 且 `KG_JEV_GRAPH_ALIGN=1` 时，由 `jev_extract` 把注解放进影子报告。见 [JEV 决策层](../../docs/算法/JEV决策层.md)。
