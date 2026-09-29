from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Protocol

from .schemas import EvidenceItem, GraphPath, LinkedEntity, ToolTraceItem
from .settings import settings


class ToolClient(Protocol):
    def locate_knowledge_space(self, question: str) -> tuple[str | None, ToolTraceItem]:
        ...

    def link_entities(self, question: str) -> tuple[list[LinkedEntity], ToolTraceItem]:
        ...

    def search_graph(self, entity: LinkedEntity, question: str, depth: int) -> tuple[list[GraphPath], ToolTraceItem]:
        ...

    def retrieve_supporting_evidence(
        self,
        question: str,
        entities: list[LinkedEntity],
        paths: list[GraphPath],
    ) -> tuple[list[EvidenceItem], ToolTraceItem]:
        ...


class LocalMockToolClient:
    """A deterministic local client used before real MCP wiring is enabled."""

    def locate_knowledge_space(self, question: str) -> tuple[str | None, ToolTraceItem]:
        if "采购" in question:
            space = "采购管理域-信息"
        elif "故障" in question or "列车" in question:
            space = "运维诊断域-规则"
        else:
            space = "通用知识空间"
        return space, ToolTraceItem(
            tool_name="local_mock.locate_knowledge_space",
            input={"question": question},
            output_summary=space,
        )

    def link_entities(self, question: str) -> tuple[list[LinkedEntity], ToolTraceItem]:
        entities: list[LinkedEntity] = []
        if "公开采购" in question:
            entities.append(
                LinkedEntity(
                    id="MOCK_CG_003",
                    name="公开采购中标金额",
                    score=0.94,
                    labels=["Indicator"],
                    properties={
                        "指标编码": "CG_003",
                        "数据来源表": "ads_dzcg_bidsum",
                        "计算SQL": "SELECT SUM(gkcaigouamount) AS value FROM ads_dzcg_bidsum",
                    },
                    reason="问题中直接出现公开采购和金额。",
                )
            )
        if "非公开采购" in question:
            entities.append(
                LinkedEntity(
                    id="MOCK_CG_PRIVATE",
                    name="非公开采购金额",
                    score=0.86,
                    labels=["Indicator"],
                    properties={"状态": "待补充指标口径"},
                    reason="问题中直接出现非公开采购。",
                )
            )
        if not entities:
            entities.append(
                LinkedEntity(id="MOCK_ENTITY", name="候选实体", score=0.5, reason="本地模拟候选。")
            )
        return entities, ToolTraceItem(
            tool_name="local_mock.link_entities",
            input={"question": question},
            output_summary=f"{len(entities)} entities",
        )

    def search_graph(self, entity: LinkedEntity, question: str, depth: int) -> tuple[list[GraphPath], ToolTraceItem]:
        path = GraphPath(
            path_id=f"path_{entity.id}_{depth}",
            nodes=[
                {"id": entity.id, "name": entity.name, "labels": entity.labels, "properties": entity.properties},
                {"id": "MOCK_DOMAIN", "name": "采购指标体系", "labels": ["KnowledgeSpace"]},
            ],
            relationships=[
                {"from": entity.id, "type": "BELONGS_TO", "to": "MOCK_DOMAIN"},
            ],
            score=max(entity.score - 0.05 * (depth - 1), 0.1),
        )
        return [path], ToolTraceItem(
            tool_name="local_mock.search_graph",
            input={"entity_id": entity.id, "depth": depth, "question": question},
            output_summary="1 path",
        )

    def retrieve_supporting_evidence(
        self,
        question: str,
        entities: list[LinkedEntity],
        paths: list[GraphPath],
    ) -> tuple[list[EvidenceItem], ToolTraceItem]:
        evidence: list[EvidenceItem] = []
        for idx, entity in enumerate(entities, start=1):
            evidence.append(
                EvidenceItem(
                    id=f"ev_entity_{idx}",
                    source_type="graph_entity",
                    content=f"{entity.name}: {entity.properties}",
                    score=entity.score,
                    entity_ids=[entity.id],
                    metadata={"reason": entity.reason},
                )
            )
        for idx, path in enumerate(paths, start=1):
            evidence.append(
                EvidenceItem(
                    id=f"ev_path_{idx}",
                    source_type="graph_path",
                    content=" -> ".join(node.get("name", node.get("id", "")) for node in path.nodes),
                    score=path.score,
                    entity_ids=[node.get("id", "") for node in path.nodes if node.get("id")],
                    path=path,
                )
            )
        return evidence, ToolTraceItem(
            tool_name="local_mock.retrieve_supporting_evidence",
            input={"question": question, "entity_count": len(entities), "path_count": len(paths)},
            output_summary=f"{len(evidence)} evidence items",
        )


class AgentcodeMcpToolClient:
    """ToolClient backed by the existing agentcode MCP wrapper.

    This adapter keeps the new Agentic GraphRAG package independent at import
    time: agentcode, pydantic, and mcp are imported only when this client is
    selected with AGENTIC_GRAPHRAG_TOOL_CLIENT=mcp.
    """

    def __init__(self, config_path: str | None = None) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        agentcode_root = repo_root / "agentcode"
        for path in (repo_root, agentcode_root):
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)

        from agentcode.agentcode.tools.mcp_tools.mcp import MCPToolWrapper
        from agentcode.agentcode.tools.mcp_tools.mcp_loader import load_mcp_servers

        self._wrapper = MCPToolWrapper()
        self._wrapper.connect_all(load_mcp_servers(config_path or settings.mcp_config))
        self._handlers = self._wrapper.get_tool_handlers()

    def close(self) -> None:
        self._wrapper.disconnect_all()

    def locate_knowledge_space(self, question: str) -> tuple[str | None, ToolTraceItem]:
        candidates = [
            ("knowledge-space-locator__locate_knowledge_space", {"user_question": question}),
            ("graph-retrieval__locate_knowledge_space", {"question": question}),
        ]
        tool_name, payload, trace = self._call_first(candidates)
        space = (
            payload.get("target_subspace")
            or payload.get("knowledge_space")
            or payload.get("subspace")
            or payload.get("name")
        )
        trace.output_summary = str(space or payload)[:200]
        trace.tool_name = tool_name
        return space, trace

    def link_entities(self, question: str) -> tuple[list[LinkedEntity], ToolTraceItem]:
        keywords_json = json.dumps(self._extract_keywords(question), ensure_ascii=False)
        tool_name, payload, trace = self._call_first([
            ("graph-search__link_entities", {"keywords_json": keywords_json, "question": question}),
            ("graph-retrieval__link_entities", {"question": question, "keywords_json": keywords_json}),
        ])
        entities = [self._linked_entity_from_raw(item) for item in payload.get("candidates", [])]
        trace.output_summary = f"{len(entities)} entities"
        trace.tool_name = tool_name
        return entities, trace

    def search_graph(self, entity: LinkedEntity, question: str, depth: int) -> tuple[list[GraphPath], ToolTraceItem]:
        tool_name, payload, trace = self._call_first([
            (
                "graph-search__search_graph",
                {"start_entity_id": entity.id, "question": question, "depth": depth},
            ),
            (
                "graph-retrieval__search_graph",
                {"start_entity_id": entity.id, "question": question, "depth": depth},
            ),
        ])
        paths = self._paths_from_raw(payload, entity)
        trace.output_summary = f"{len(paths)} paths"
        trace.tool_name = tool_name
        return paths, trace

    def retrieve_supporting_evidence(
        self,
        question: str,
        entities: list[LinkedEntity],
        paths: list[GraphPath],
    ) -> tuple[list[EvidenceItem], ToolTraceItem]:
        evidence: list[EvidenceItem] = []
        for idx, entity in enumerate(entities, start=1):
            evidence.append(
                EvidenceItem(
                    id=f"ev_entity_{idx}",
                    source_type="graph_entity",
                    content=f"{entity.name}: {entity.properties}",
                    score=entity.score,
                    entity_ids=[entity.id],
                    metadata={"reason": entity.reason},
                )
            )
        for idx, path in enumerate(paths, start=1):
            evidence.append(
                EvidenceItem(
                    id=f"ev_path_{idx}",
                    source_type="graph_path",
                    content=" -> ".join(node.get("name", node.get("id", "")) for node in path.nodes),
                    score=path.score,
                    entity_ids=[node.get("id", "") for node in path.nodes if node.get("id")],
                    path=path,
                )
            )
        return evidence, ToolTraceItem(
            tool_name="mcp.local_evidence_builder",
            input={"question": question, "entity_count": len(entities), "path_count": len(paths)},
            output_summary=f"{len(evidence)} evidence items",
        )

    def _call_first(self, candidates: list[tuple[str, dict]]) -> tuple[str, dict, ToolTraceItem]:
        errors: list[str] = []
        for tool_name, args in candidates:
            handler = self._handlers.get(tool_name)
            if not handler:
                errors.append(f"{tool_name}: unavailable")
                continue
            raw = handler(**args)
            payload = self._parse_payload(raw)
            if isinstance(payload, dict) and not payload.get("error"):
                return tool_name, payload, ToolTraceItem(tool_name=tool_name, input=args, ok=True)
            errors.append(f"{tool_name}: {payload}")
        return candidates[0][0], {}, ToolTraceItem(
            tool_name=candidates[0][0],
            input=candidates[0][1],
            output_summary="; ".join(errors),
            ok=False,
        )

    @staticmethod
    def _parse_payload(raw: object) -> dict:
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return {"value": raw}
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"text": text}

    @staticmethod
    def _extract_keywords(question: str) -> list[str]:
        separators = "，,。？?；;、 和 与 及 的 是 如何 怎么 构成 分析"
        keywords = [question]
        for sep in separators.split():
            next_keywords: list[str] = []
            for item in keywords:
                next_keywords.extend(part.strip() for part in item.split(sep) if part.strip())
            keywords = next_keywords or keywords
        return keywords[:8]

    @staticmethod
    def _linked_entity_from_raw(raw: dict) -> LinkedEntity:
        props = raw.get("properties") or {}
        return LinkedEntity(
            id=str(raw.get("id") or raw.get("qid") or raw.get("entity_id") or raw.get("name") or ""),
            name=str(raw.get("name") or props.get("name") or raw.get("id") or ""),
            score=float(raw.get("score") or raw.get("relevance_score") or 0.0),
            labels=list(raw.get("labels") or props.get("labels") or []),
            properties=props,
            reason=str(raw.get("llm_judgment") or raw.get("reason") or ""),
        )

    @staticmethod
    def _paths_from_raw(payload: dict, entity: LinkedEntity) -> list[GraphPath]:
        if isinstance(payload.get("paths"), list):
            return [
                GraphPath(
                    path_id=str(item.get("path_id") or f"path_{idx}"),
                    nodes=list(item.get("nodes") or []),
                    relationships=list(item.get("relationships") or []),
                    score=float(item.get("score") or entity.score),
                )
                for idx, item in enumerate(payload["paths"], start=1)
                if isinstance(item, dict)
            ]

        paths: list[GraphPath] = []
        for idx, triple in enumerate(payload.get("triples") or [], start=1):
            if not isinstance(triple, dict):
                continue
            head = triple.get("head_entity") or {"id": entity.id, "name": entity.name}
            tail = triple.get("tail_entity") or {}
            relation = triple.get("relation") or triple.get("type") or "RELATED_TO"
            paths.append(
                GraphPath(
                    path_id=f"path_{entity.id}_{idx}",
                    nodes=[head, tail],
                    relationships=[{"from": head.get("id"), "type": relation, "to": tail.get("id")}],
                    score=entity.score,
                )
            )
        return paths


def build_tool_client() -> ToolClient:
    if settings.tool_client.lower() == "mcp":
        return AgentcodeMcpToolClient(settings.mcp_config)
    return LocalMockToolClient()
