from typing import Any

from mcp_services.runtime import mcp
from mcp_services.result import ToolCode, invoke_tool, tool_error
from mcp_services.dispatch import maybe_submit


@mcp.tool(name="kg_extract_v2")
def extract_knowledge(
    ontology: str = "",
    text: str = "",
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """从文本抽取知识。传 PlantUML ontology 时按元模型抽取实体/关系图谱；不传本体时走非结构化抽取，返回文本摘要。"""
    queued = maybe_submit(
        "mcp.knowledge.extract",
        {"ontology": ontology, "text": text, "model": model, "base_url": base_url},
        resource_class="io.llm",
    )
    if queued is not None:
        return queued
    return invoke_tool(
        lambda: _extract(ontology, text, model, base_url),
        ToolCode.KNOWLEDGE_FAILED,
        "知识抽取完成",
    )


def _extract(ontology, text, model, base_url):
    from algorithm.knowledge_extract import extract_v2, summarize_text

    if (ontology or "").strip():
        return extract_v2(ontology=ontology, text=text, model=model, base_url=base_url)
    # 非结构化：无本体时不按元模型抽取，直接生成文本摘要
    return summarize_text(text=text, model=model, base_url=base_url)


@mcp.tool(name="kg_get_dimension_scales")
def get_dimension_scales(dimension: str) -> dict[str, Any]:
    """Return all scales associated with a knowledge-space dimension."""
    return tool_error(
        ToolCode.EXTERNAL_CAPABILITY_UNAVAILABLE,
        "维度刻度查询接口尚未开通",
        {"capability": "graph.write"},
    )


@mcp.tool(name="kg_init_knowledge_space")
def init_knowledge_space(dimension_scales: dict[str, list[str]]) -> dict[str, Any]:
    """Create a knowledge space from a dimension-to-scales mapping."""
    return tool_error(
        ToolCode.EXTERNAL_CAPABILITY_UNAVAILABLE,
        "知识空间写入接口尚未开通",
        {"capability": "graph.write"},
    )


@mcp.tool(name="kg_store_extraction")
def store_extraction(graph: dict[str, Any]) -> dict[str, Any]:
    """Report that application-owned graph persistence is not yet available."""
    return tool_error(
        ToolCode.EXTERNAL_CAPABILITY_UNAVAILABLE,
        "知识图谱写入接口尚未开通",
        {"capability": "graph.write"},
    )
