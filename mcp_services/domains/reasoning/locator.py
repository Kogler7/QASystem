from typing import Any

from mcp_services.runtime import mcp


def _locator():
    from algorithm.ans.QA.config import Config
    from algorithm.ans.QA.tools.knowledge_space_locator import KnowledgeSpaceLocator

    return KnowledgeSpaceLocator(Config.load())


@mcp.tool(name="locate_knowledge_space")
def locate_knowledge_space(user_question: str) -> dict[str, Any]:
    """Locate the knowledge subspace that best matches a user question."""
    return _locator().locate(user_question)


@mcp.tool(name="get_all_subspaces")
def get_all_subspaces() -> dict[str, Any]:
    """Return all configured knowledge subspaces."""
    return _locator().get_all_subspaces()
