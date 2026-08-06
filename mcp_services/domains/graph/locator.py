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
    """Locate the knowledge subspace that best matches a user question."""
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
    """Return all configured knowledge subspaces."""
    return invoke_tool(
        lambda: _locator().get_all_subspaces(),
        ToolCode.GRAPH_FAILED,
        "知识子空间查询",
    )
