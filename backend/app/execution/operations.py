from __future__ import annotations
from docker.configuration import getenv as config_getenv

import base64
import os
from contextlib import contextmanager
from functools import lru_cache
from typing import Any


@contextmanager
def _mcp_activity_context():
    """Prevent a Worker Activity from submitting itself back to the queue."""
    previous = config_getenv("MCP_EXECUTION_MODE")
    os.environ["MCP_EXECUTION_MODE"] = "inline"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MCP_EXECUTION_MODE", None)
        else:
            os.environ["MCP_EXECUTION_MODE"] = previous

from backend.app.schemas import (
    ChunkOptions,
    GraphEmbeddingRequest,
    GraphExtractionItem,
    GraphItem,
    GraphRequest,
    PipelineOptions,
    TextChunk,
    TextEmbeddingItem,
)


def _decode_content(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return base64.b64decode(value)
    if isinstance(value, dict) and "__bytes_base64__" in value:
        return base64.b64decode(value["__bytes_base64__"])
    raise ValueError("payload content must be bytes or base64")


def _parse_document(payload: dict[str, Any]) -> dict[str, Any]:
    from .document_sharding import orchestrate_document_parse

    return orchestrate_document_parse(payload)


def _parse_document_shard(payload: dict[str, Any]) -> dict[str, Any]:
    from .document_sharding import parse_document_shard

    return parse_document_shard(payload)


def _chunk_document(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.app.services.algorithm_capabilities import document_chunking_service

    return document_chunking_service.chunk(
        payload["markdown"], ChunkOptions.model_validate(payload.get("options") or {})
    )


def _unstructured_extract(payload: dict[str, Any]) -> dict[str, Any]:
    """非结构化抽取：无本体时对每个切片生成摘要，不产图谱。"""
    from algorithm.knowledge_extract.v1.batch import summarize_chunks

    items = payload.get("items") or []
    results = summarize_chunks(
        [item.get("text", "") for item in items],
        model=(payload.get("options") or {}).get("model"),
    )
    chunks = []
    for source, res in zip(items, results):
        chunks.append({
            "index": res["index"],
            "text": res["text"],
            "range": source.get("ranges", source.get("range")),
            "metadata": dict(source.get("metadata") or {}),
            "summary": res["summary"],
            "error": res.get("error"),
            "error_code": res.get("error_code"),
        })
    return {"mode": "unstructured", "count": len(chunks), "chunks": chunks}


def _extract_graphs(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.app.services.algorithm_capabilities import graph_extraction_service

    ontology = str(payload.get("ontology") or "").strip()
    items = [GraphExtractionItem.model_validate(item) for item in payload["items"]]
    if not ontology:
        # 兼容：不传元模型时走非结构化抽取（切片+摘要）
        return _unstructured_extract(payload)
    result = graph_extraction_service.extract(
        ontology,
        items,
        model=(payload.get("options") or {}).get("model"),
        merge=(payload.get("options") or {}).get("merge", True),
        **({"extraction_config": payload["__extraction_config"]} if "__extraction_config" in payload else {}),
    )
    return result.graph_data()


def _embed_texts(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.app.services.algorithm_capabilities import embedding_service

    return embedding_service.texts([TextEmbeddingItem.model_validate(item) for item in payload["items"]])


def _embed_graphs(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.app.services.algorithm_capabilities import embedding_service

    return embedding_service.graphs([GraphItem.model_validate(item) for item in payload["items"]])


def _merge_graphs(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.app.services.algorithm_capabilities import graph_merge_service

    return graph_merge_service([GraphItem.model_validate(item) for item in payload["items"]])


def _confidence(payload: dict[str, Any]) -> dict[str, Any]:
    from external_services.application import ExternalCapabilityUnavailable

    raise ExternalCapabilityUnavailable(
        "confidence.calculate", "第三方置信度接口尚未开通"
    )


@lru_cache(maxsize=1)
def _knowledge_service():
    from backend.app.services.algorithm_capabilities import (
        document_chunking_service,
        document_parsing_service,
        embedding_service,
        graph_extraction_service,
    )
    from backend.app.services.knowledge_precipitation import KnowledgePrecipitationService

    return KnowledgePrecipitationService(
        parsing=document_parsing_service,
        chunking=document_chunking_service,
        extraction=graph_extraction_service,
        embeddings=embedding_service,
    )


def _knowledge_ingestion(payload: dict[str, Any]) -> dict[str, Any]:
    # 协作式取消（execution 层挂载点）：此处守住流水线入口，取消发生在
    # 出队后、开始前的任务在此干净中止。job_id 随请求传入 service，
    # KnowledgePrecipitationService.process 已在解析阶段边界（解析前、
    # 解析完成→chunk 前）调用 manager.is_cancel_requested(job_id)，运行中
    # 也能干净中止；页分片编排（KNOWLEDGE_PIPELINE_PARSE_SHARDING）内部另有
    # 自己的父任务取消监听，命中后级联取消子页段任务。
    # TODO(阶段边界): chunk 完成→extract 前的边界仍未挂载。
    from backend.app.execution.manager import ExecutionCancelled, get_execution_manager

    execution_meta = payload.get("__execution") or {}
    if get_execution_manager().is_cancel_requested(execution_meta.get("job_id")):
        raise ExecutionCancelled(f"知识沉淀任务 {execution_meta.get('job_id')} 已在入队期间被取消")

    from backend.app.services.knowledge_precipitation import (
        BatchKnowledgeIngestionRequest,
        FileItem,
        KnowledgeIngestionRequest,
    )

    options = PipelineOptions.model_validate(payload.get("options") or {})
    files = payload.get("files") or []
    service = _knowledge_service()
    ontology = str(payload.get("ontology") or "")
    if len(files) == 1 and not payload.get("batch"):
        item = files[0]
        return service.process(
            KnowledgeIngestionRequest(
                file_name=item["file_name"],
                content=_decode_content(item["content"]),
                ontology=ontology,
                options=options,
                job_id=execution_meta.get("job_id"),
                extraction_config=payload.get("__extraction_config"),
            )
        )
    return service.process_batch(
        BatchKnowledgeIngestionRequest(
            files=[FileItem(file_name=item["file_name"], content=_decode_content(item["content"])) for item in files],
            ontology=ontology,
            options=options,
            extraction_config=payload.get("__extraction_config"),
        )
    )


def _mcp_execute_sql(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.data.sql import _execute_sql

    from mcp_services.result import ToolCode, normalize_tool_result

    result = _execute_sql(
        payload["sql"],
        payload.get("params"),
        payload.get("database_url"),
        payload.get("read_only", True),
    )
    return normalize_tool_result(result, ToolCode.DATA_FAILED, "SQL 执行")


def _mcp_knowledge_extract(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.knowledge.extraction import _extract
    from mcp_services.result import ToolCode, normalize_tool_result

    result = _extract(
        str(payload.get("ontology") or ""),
        str(payload.get("text") or ""),
        payload.get("model"),
        payload.get("base_url"),
    )
    return normalize_tool_result(result, ToolCode.KNOWLEDGE_FAILED, "知识抽取完成")


def _mcp_knowledge_store(payload: dict[str, Any]) -> dict[str, Any]:
    from external_services.application import ExternalCapabilityUnavailable

    raise ExternalCapabilityUnavailable("graph.write", "第三方图谱写入接口尚未开通")


def _mcp_search_graph(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.retrieval import search_graph

    with _mcp_activity_context():
        return search_graph(
            payload["start_entity_id"],
            question=payload.get("question", ""),
            relationship_types=payload.get("relationship_types"),
            direction=payload.get("direction", "both"),
            limit=payload.get("limit", 50),
        )


def _mcp_link_entities(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.retrieval import link_entities

    with _mcp_activity_context():
        return link_entities(
            payload.get("keywords", []),
            question=payload.get("question", ""),
            space_id=payload.get("space_id"),
            space_name=payload.get("space_name"),
        )


def _mcp_query_entity(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.retrieval import query_entity_properties

    with _mcp_activity_context():
        return query_entity_properties(payload["entity_id"])


def _mcp_locate_knowledge_space(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.locator import locate_knowledge_space

    with _mcp_activity_context():
        return locate_knowledge_space(payload["user_question"], payload.get("app_requirement"))


def _mcp_get_all_subspaces(_payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.locator import get_all_subspaces

    with _mcp_activity_context():
        return get_all_subspaces()


def _mcp_search_space_units(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.locator import search_space_units

    with _mcp_activity_context():
        return search_space_units(
            space_id=payload.get("space_id"),
            space_name=payload.get("space_name"),
            limit=payload.get("limit", 20),
        )




def _mcp_get_stakeholders(_payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.retrieval import get_stakeholders

    with _mcp_activity_context():
        return get_stakeholders()


def _mcp_find_problem_classes(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.retrieval import find_problem_classes

    with _mcp_activity_context():
        return find_problem_classes(
            payload["stakeholder_id"],
            limit=payload.get("limit", 40),
            relationship_types=payload.get("relationship_types"),
            direction=payload.get("direction", "both"),
        )


def _mcp_find_solution_classes(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.graph.retrieval import find_solution_classes

    with _mcp_activity_context():
        return find_solution_classes(
            payload["problem_id"],
            limit=payload.get("limit", 40),
            relationship_types=payload.get("relationship_types"),
            direction=payload.get("direction", "both"),
        )


def _mcp_draw_chart(payload: dict[str, Any]) -> dict[str, Any]:
    from mcp_services.domains.visualization.charts import _draw

    method = str(payload.get("method", "")).strip()
    allowed_methods = {
        "bar_chart",
        "pie_chart",
        "line_chart",
        "scatter_plot",
        "histogram",
        "area_chart",
        "radar_chart",
        "box_plot",
        "bubble_chart",
        "heatmap",
    }
    if method not in allowed_methods:
        raise ValueError("invalid chart method")
    arguments = dict(payload.get("arguments") or {})
    with _mcp_activity_context():
        return _draw(method, arguments)


_OPERATIONS = {
    "document.parse": _parse_document,
    "document.parse.shard": _parse_document_shard,
    "document.chunk": _chunk_document,
    "knowledge.unstructured_extract": _unstructured_extract,
    "graph.extract": _extract_graphs,
    "embedding.texts": _embed_texts,
    "embedding.graphs": _embed_graphs,
    "graph.merge": _merge_graphs,
    "graph.confidence": _confidence,
    "knowledge.ingestion": _knowledge_ingestion,
    "mcp.execute_sql": _mcp_execute_sql,
    "mcp.knowledge.extract": _mcp_knowledge_extract,
    "mcp.knowledge.store": _mcp_knowledge_store,
    "mcp.graph.search": _mcp_search_graph,
    "mcp.graph.link_entities": _mcp_link_entities,
    "mcp.graph.query_entity": _mcp_query_entity,
    "mcp.graph.locate_space": _mcp_locate_knowledge_space,
    "mcp.graph.get_all_subspaces": _mcp_get_all_subspaces,
    "mcp.graph.get_stakeholders": _mcp_get_stakeholders,
    "mcp.graph.find_problem_classes": _mcp_find_problem_classes,
    "mcp.graph.find_solution_classes": _mcp_find_solution_classes,
    "mcp.graph.search_space_units": _mcp_search_space_units,
    "mcp.visualization.draw": _mcp_draw_chart,
}


def run_operation(operation: str, payload: dict[str, Any]) -> Any:
    try:
        handler = _OPERATIONS[operation]
    except KeyError as exc:
        raise ValueError(f"unknown execution operation: {operation}") from exc
    return handler(payload)
