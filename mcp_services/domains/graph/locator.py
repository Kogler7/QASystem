from functools import lru_cache
from typing import Any

from mcp_services.result import ToolCode, invoke_tool, tool_error, tool_success
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


def _km():
    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    return KnowledgeMapClient()


def _find_space(space_id=None, space_name=None):
    """定位单个 KnowledgeSpace：优先 space_id；其次 name 精确；再 name CONTAINS 模糊。

    返回 (space, ambiguous)：space 为 {id, name}；ambiguous 非空表示按名字命中多个，
    需 Agent 用更完整名称或空间 id 重新定位。
    """
    sid = (space_id or "").strip()
    if sid:
        rows = _km().execute_cypher(
            "MATCH (sp:KnowledgeSpace {id:$id}) RETURN sp.id AS id, sp.name AS name LIMIT 2",
            {"id": sid},
        )
        if rows:
            return (rows[0], []) if len(rows) == 1 else (None, [r.get("name") for r in rows])
        return None, []

    name = (space_name or "").strip()
    if not name:
        return None, []

    rows = _km().execute_cypher(
        "MATCH (sp:KnowledgeSpace) WHERE sp.name = $name "
        "RETURN sp.id AS id, sp.name AS name LIMIT 5",
        {"name": name},
    )
    if not rows:
        rows = _km().execute_cypher(
            "MATCH (sp:KnowledgeSpace) WHERE sp.name CONTAINS $name "
            "RETURN sp.id AS id, sp.name AS name ORDER BY sp.name LIMIT 20",
            {"name": name},
        )
    if not rows:
        return None, []
    if len(rows) > 1:
        return None, [r.get("name") or "" for r in rows]
    return rows[0], []


@mcp.tool(name="search_space_units")
def search_space_units(
    space_id: str | None = None,
    space_name: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """空间内知识单元：返回某知识空间直接包含的 KnowledgeUnit（CONTAINS_UNIT 一跳），
    每个单元补齐完整属性（含 KnowledgeProperty 逻辑属性），供 Agent 按详情筛选。

    使用前提：**仅当方案步骤明确指明要去某个子空间/库（如“从规则库找信息”）时才调用**；
    用该空间名（支持精确/包含模糊匹配，如“规则库”命中“故障诊断规则库”）一次取回单元。
    不要用它做泛化探索或替代常规图谱查询；返回空表示该空间下未检索到直接包含的单元。"""
    queued = maybe_submit(
        "mcp.graph.search_space_units",
        {"space_id": space_id, "space_name": space_name, "limit": limit},
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        space, ambiguous = _find_space(space_id, space_name)
        if ambiguous:
            return tool_error(ToolCode.INVALID_ARGUMENT, "知识空间名称不唯一，请用完整名称或空间 id",
                              {"candidates": ambiguous})
        if not space:
            return tool_error(ToolCode.GRAPH_FAILED, "未找到该知识空间",
                              {"space_id": space_id, "space_name": space_name})
        rows = _km().execute_cypher(
            "MATCH (sp:KnowledgeSpace {id:$id})-[:CONTAINS_UNIT]->(ku:KnowledgeUnit) "
            "RETURN ku.id AS id, ku.name AS name, ku.content AS content, labels(ku) AS labels "
            "LIMIT $limit",
            {"id": space["id"], "limit": int(limit)},
        )
        units = [
            {
                "id": r.get("id"),
                "name": r.get("name") or "",
                "content": (r.get("content") or "") or "",
                "labels": r.get("labels") or [],
            }
            for r in (rows or [])
        ]
        # 与 find_problem_classes / find_solution_classes 一致：为每个单元补齐完整属性（含逻辑属性），
        # 便于 Agent 按详情筛选，而不是只看名字
        try:
            from mcp_services.domains.graph.retrieval import _attach_full_properties

            _attach_full_properties(units)
        except Exception:
            pass
        return tool_success({"space": space, "units": units, "count": len(units)}, "知识单元获取完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "知识单元获取失败", str(exc))
