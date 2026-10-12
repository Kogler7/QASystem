"""Phase 1 Step 10：GraphAssembler + Compat（§26-§28）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：
- §26 O2：graph / triples 只由 accepted Intermediate Objects 映射；
- §27 UID 原则：graph node UID 只能由稳定类型 + canonical identity 生成，
  普通属性不得影响 UID；
- §28 Result Contract：保留现有 raw / graph / warnings / attempts / profile
  等 API，并附带 intermediate_model / triples / coverage_gaps / mapping 等新字段；
  ``compat_raw`` 对应 ``intermediate_model.compat_raw()``。

本模块不依赖 openai，可独立加载与测试。
"""
from __future__ import annotations

from typing import Any

from .graph_utils import build_node_properties, entity_uid


class GraphIntegrityError(ValueError):
    """Final reconciliation found an internal referential-integrity bug."""


class GraphAssembler:
    """O1 中间对象模型 -> O2 graph / triples（§26）。"""

    @staticmethod
    def uid_for(type_id: str, identity: str) -> str:
        """§27：稳定 UID 只由稳定类型 + canonical identity 生成。"""
        return entity_uid(type_id, identity, None)

    def build_graph(
        self,
        objects: list[dict[str, Any]],
        relations: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """O2 graph：nodes + relationships（端点必须是已确认对象，无悬空）。"""
        nodes: list[dict[str, Any]] = []
        node_index: dict[str, dict[str, Any]] = {}
        for obj in objects:
            type_id = str(obj.get("type") or "")
            identity = str(obj.get("identity") or "")
            uid = self.uid_for(type_id, identity)
            if uid not in node_index:
                compat_properties = obj.get("compat_properties")
                node = {
                    "uid": uid,
                    "label": type_id,
                    "properties": build_node_properties(
                        obj.get("display_name") or identity,
                        compat_properties
                        if isinstance(compat_properties, dict)
                        else obj.get("properties") or {},
                    ),
                }
                nodes.append(node)
                node_index[uid] = node

        by_id = {obj.get("id"): obj for obj in objects}
        relationships: list[dict[str, Any]] = []
        relationship_keys: set[tuple[str, str, str]] = set()
        for relation in relations:
            from_obj = by_id.get(relation.get("from"))
            to_obj = by_id.get(relation.get("to"))
            if from_obj is None or to_obj is None:
                raise GraphIntegrityError(
                    "relation references a non-accepted entity: "
                    f"{relation.get('from')!r} -> {relation.get('to')!r}"
                )
            from_uid = self.uid_for(str(from_obj.get("type") or ""), str(from_obj.get("identity") or ""))
            to_uid = self.uid_for(str(to_obj.get("type") or ""), str(to_obj.get("identity") or ""))
            relation_type = str(relation.get("type") or "")
            key = (from_uid, to_uid, relation_type)
            if key in relationship_keys:
                continue
            relationship_keys.add(key)
            relationships.append({"from_uid": from_uid, "to_uid": to_uid, "type": relation_type})

        return {"nodes": nodes, "relationships": relationships}

    def build_triples(self, graph: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        return [
            {
                "head": relationship["from_uid"],
                "relation": relationship["type"],
                "tail": relationship["to_uid"],
            }
            for relationship in graph.get("relationships", [])
        ]

    @staticmethod
    def compat_raw(
        objects: list[dict[str, Any]],
        relations: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """§28：``intermediate_model.compat_raw()``——兼容现有 raw 契约。"""
        entities: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for obj in objects:
            compat_properties = obj.get("compat_properties")
            entity: dict[str, Any] = {
                "type": obj.get("type"),
                "name": obj.get("display_name") or obj.get("identity"),
                "properties": dict(
                    compat_properties
                    if isinstance(compat_properties, dict)
                    else obj.get("properties") or {}
                ),
                "candidate_id": obj.get("id"),
            }
            evidence = obj.get("evidence") or []
            if evidence:
                entity["evidence"] = evidence[0]
            entities.append(entity)
            by_id[obj.get("id")] = obj

        raw_relations: list[dict[str, Any]] = []
        for relation in relations:
            from_obj = by_id.get(relation.get("from"))
            to_obj = by_id.get(relation.get("to"))
            if from_obj is None or to_obj is None:
                raise GraphIntegrityError(
                    "relation references a non-accepted entity: "
                    f"{relation.get('from')!r} -> {relation.get('to')!r}"
                )
            raw_relation: dict[str, Any] = {
                "from_id": relation.get("from"),
                "from_type": from_obj.get("type"),
                "from_name": from_obj.get("display_name") or from_obj.get("identity"),
                "rel_type": relation.get("type"),
                "to_type": to_obj.get("type"),
                "to_name": to_obj.get("display_name") or to_obj.get("identity"),
                "to_id": relation.get("to"),
            }
            evidence = relation.get("evidence") or []
            if evidence:
                raw_relation["evidence"] = evidence[0]
            raw_relations.append(raw_relation)
        return {"entities": entities, "relations": raw_relations}

__all__ = ["GraphAssembler", "GraphIntegrityError"]
