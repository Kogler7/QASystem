from functools import lru_cache
from typing import Any

from mcp_services.result import ToolCode, invoke_tool
from mcp_services.runtime import mcp
from mcp_services.dispatch import maybe_submit


@lru_cache(maxsize=1)
def _locator():
    from algorithm.ans.QA.config import Config
    from algorithm.ans.QA.tools.knowledge_space_locator import KnowledgeSpaceLocator

    return KnowledgeSpaceLocator(Config.load())


@mcp.tool(name="locate_knowledge_space")
def locate_knowledge_space(
    user_question: str,
    app_requirement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """知识空间定位：根据用户问题匹配最合适的知识子空间（业务领域）。内部用LLM辅助判断。定位失败不阻塞后续流程。"""
    queued = maybe_submit(
        "mcp.graph.locate_space",
        {"user_question": user_question, "app_requirement": app_requirement},
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    return invoke_tool(
        lambda: _locator().locate(user_question, app_requirement),
        ToolCode.GRAPH_FAILED,
        "知识空间定位完成",
    )


@mcp.tool(name="get_all_subspaces")
def get_all_subspaces() -> dict[str, Any]:
    """获取所有知识子空间：返回系统中全部已配置的知识子空间及刻度。用于locate_knowledge_space定位失败时查看候选。"""
    queued = maybe_submit("mcp.graph.get_all_subspaces", {}, resource_class="io.graph")
    if queued is not None:
        return queued
    return invoke_tool(
        lambda: _locator().get_all_subspaces(),
        ToolCode.GRAPH_FAILED,
        "知识子空间查询",
    )
