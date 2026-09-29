"""v3 抽取算法的统一入口（shim）。

实际抽取在 ``algorithm.knowledge_extract_v3``；本模块把 v3 的 flat facts 结果转换成
v2 聚合形状，使上层 ``project_items`` / ``deduplicate_chunk_graphs`` 可以无差别使用。
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Iterable

from algorithm.knowledge_extract_v2.graph_utils import entity_uid


def _v3_source_block_ids(refs: Any) -> list[str]:
    """Collect the chunk ids a V3 ``source_refs`` list points at, in order."""
    ids: list[str] = []
    for ref in refs or ():
        if not isinstance(ref, dict):
            continue
        chunk_id = str(ref.get("chunk_id") or "")
        if chunk_id and chunk_id not in ids:
            ids.append(chunk_id)
    return ids


#: V3 reports success/incomplete/failed; the adapter contract is the V2
#: succeeded/partial/failed vocabulary.
_V3_STATUS_MAP = {"success": "succeeded", "incomplete": "partial", "failed": "failed"}


def _v3_to_aggregate(result: dict[str, Any]) -> dict[str, Any]:
    """Reshape V3's flat fact list into the V2 aggregate contract."""
    entities = [item for item in (result.get("entities") or []) if isinstance(item, dict)]
    relations = [item for item in (result.get("relations") or []) if isinstance(item, dict)]

    nodes: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    for entity in entities:
        name = str(entity.get("name") or "")
        entity_type = str(entity.get("type") or "")
        uid = entity_uid(entity_type, name, None)
        properties = entity.get("properties")
        nodes.append(
            {
                "uid": uid,
                "label": entity_type,
                "properties": {"name": name, **(properties if isinstance(properties, dict) else {})},
            }
        )
        objects.append(
            {
                "id": uid,
                "type": entity_type,
                "identity": name,
                "evidence": [{"source_block_ids": _v3_source_block_ids(entity.get("source_refs"))}],
            }
        )

    relationships: list[dict[str, Any]] = []
    for relation in relations:
        rel_type = str(relation.get("rel_type") or "")
        if not rel_type:
            continue
        evidences: list[dict[str, Any]] = []
        for ref in relation.get("source_refs") or ():
            if not isinstance(ref, dict):
                continue
            chunk_id = str(ref.get("chunk_id") or "")
            if not chunk_id:
                continue
            entry: dict[str, Any] = {"source_block_ids": [chunk_id]}
            if ref.get("quote"):
                entry["quote"] = ref["quote"]
            evidences.append(entry)
        relationships.append(
            {
                "from_uid": entity_uid(relation.get("from_type"), relation.get("from_name"), None),
                "to_uid": entity_uid(relation.get("to_type"), relation.get("to_name"), None),
                "type": rel_type,
                "evidences": evidences,
            }
        )

    attempts: list[dict[str, Any]] = []
    for diagnostic in result.get("diagnostics") or ():
        if not isinstance(diagnostic, dict):
            continue
        attempts.append(
            {
                "attempt": 1,
                "type": str(diagnostic.get("code") or "diagnostic").lower(),
                "pass": None,
                "chunk": diagnostic.get("chunk_id") or diagnostic.get("observation_id"),
                "message": diagnostic.get("message") or diagnostic.get("code"),
                "response_status": None,
            }
        )

    status = _V3_STATUS_MAP.get(str(result.get("status") or ""))
    if status is None:
        status = "partial" if (entities or relations) else "failed"
    return {
        "status": status,
        "error_code": None,
        "graph": {"nodes": nodes, "relationships": relationships},
        "intermediate_model": {"objects": objects, "relations": []},
        "raw": {"entities": entities, "relations": relations},
        "attempts": attempts,
        "warnings": [],
        "profile": {"chunk_source_ids": {}, "extract_stats": result.get("stats") or {}},
    }


class _V3ClientBridge:
    """Give V3 the ``client(messages) -> StructuredLLMResponse`` callable it expects.

    Built on V2's configured client so both versions run against the same
    ``KG_EXTRACT_LLM_*`` endpoint (and so V3's ``ProviderConfig`` HTTPS-only
    validation, which would reject the internal HTTP LLM, is not involved).
    """

    def __init__(self, llm: Any):
        self._llm = llm
        # V3's harness reads ``client.config.max_tokens`` as output-token reserve.
        self.config = SimpleNamespace(max_tokens=getattr(llm, "max_tokens", 16384))

    def __call__(self, messages: list[dict[str, str]]) -> Any:
        return self._llm.generate_structured(
            {},
            messages,
            {"response_mode": "json_object", "schema_name": "kg_extract_v3"},
        )


def extract(
    ontology: str,
    chunks: Iterable[Any],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_workers: int | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Run V3's ``extract_detailed`` once over ``chunks``, V2-contract shaped."""
    from algorithm.knowledge_extract_v2.llm_client import create_llm_client
    from algorithm.knowledge_extract_v3 import extract_detailed

    llm = create_llm_client(base_url=base_url or "", api_key=api_key or "", model=model or "")
    result = extract_detailed(
        list(chunks),
        ontology,
        llm=_V3ClientBridge(llm),
        max_workers=max(1, int(max_workers or 4)),
    )
    return _v3_to_aggregate(result)
