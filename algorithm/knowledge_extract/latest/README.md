# knowledge_extract/latest

`latest` 是当前主版本知识抽取算法的**独立代码副本**，不是 `v2`/`v3` 的软链或运行时映射。
`KG_EXTRACT_ALGO_VERSION=latest`（默认）直接运行本包；对 `v3` 的后续实验性改动不会自动
影响本目录，必须显式同步后才生效。

## 同步状态

- 本次同步日期：2026-10-01
- 同步来源：`algorithm/knowledge_extract/v3/`（sxm-dev 版完整流水线）全量 `.py` + `tests/`
- 本次同步包含 P0 止血修复：证据闸 fail-open（`KG_EXTRACT_EVIDENCE_ADMISSION=warn` 默认，
  不可逐字定位/核验不一致的实体保留并标记 `evidence_status=unverified`，终验
  `EVIDENCE_INVALID` 降为 warning）、修复循环默认 2 轮（`KG_EXTRACT_MAX_REPAIR_ROUNDS`）、
  实体/关系 LLM 主调用多重试恢复（`KG_EXTRACT_LLM_MAX_ATTEMPTS`，默认 3）。
- 前次同步已移除旧版实验 harness（`adapter/client/context/facts/harness/prompts/schema`）。

## 如何再同步

1. 删除本目录下除本 README 外的全部内容；
2. 复制 `v3/` 的全部 `.py`（含 `v3/tests/`，不含 `__pycache__`）到本目录；
3. 将副本中的包名引用改写为本目录：
   `algorithm.knowledge_extract.v3` → `algorithm.knowledge_extract.latest`，
   `knowledge_extract_v3` → `knowledge_extract_latest`（测试的独立加载包名）；
4. 恢复本目录 `__init__.py` 的 docstring（注明同步日期与来源），并确认仍 re-export
   `ExtractionChunk`（旧 latest 公开过该符号，保持兼容）；
5. grep 验证无 `knowledge_extract.v3` / `from ..v3` / `from ..v2` 等跨包引用残留；
6. 跑 `tests/backend/test_knowledge_extract_adapter.py` 与本目录 `tests/`，再跑全量 pytest。

## 入口契约

```python
from algorithm.knowledge_extract.latest import (
    ExtractionChunk,
    extract,            # (chunks, ontology, **kwargs) -> {"entities": [...], "relations": [...]}
    extract_aggregate,  # (ontology, chunks, *, target_types, validation_mode, best_effort,
                        #  model, base_url, api_key, max_workers, llm, **_) -> 聚合契约
    extract_harness,    # 流水线本体：raw/graph/verification/quality/attempts/profile 聚合结果
    extract_split,      # 分段抽取底层入口
    deduplicate_chunk_graphs,
)
```

`extract_aggregate` 的聚合返回包含 `status` / `graph` / `raw` / `intermediate_model` /
`attempts` / `warnings` / `profile` / `verification`，由
`backend/app/services/knowledge_extract_adapter.py` 投影回逐 chunk 结果。

## 算法行为说明

本目录代码与 `v3` 同源，算法行为（两段式抽取、属性补全、pair 关系分类、official
文档结构抽取、验证与修复等）见 `v3/README.md`。benchmark 数据与评测脚本按仓库约定
位于 `test_data/` 与 `eval/`，不在本目录。
