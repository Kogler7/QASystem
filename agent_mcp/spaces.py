"""Agent 专属知识空间工具——与旧 MCP graph/locator.py 行为一致（用户确认原样搬运）。

- locate_knowledge_space 内部保留 GRAPH_LLM（小模型）辅助选根空间，与旧 MCP 完全一致；
  agent 也可以不走它，用 get_all_subspaces 看全量候选自己选。
- 候选属性补齐（_attach_full_properties）用 EntityLinker 在本包内重新实现：
  agent-mcp 容器不挂 mcp_services 卷，不能导入旧 MCP 的内部函数。
- 空间选择、消歧重试均由 agent 自行决定，工具不预设业务链路。
"""

from functools import lru_cache
from typing import Any

from agent_mcp.graph import _as_list, _attach_full_properties, _km, _truncate
from agent_mcp.runtime import mcp


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
    """知识空间定位：根据问题语义从根知识空间中选出最匹配的一个，返回根空间、一跳子空间与维度刻度。内部用LLM辅助判断；无法判断时返回 可回答=false 及候选根列表。

    定位到空间后，用 search_space_units 按空间名/id 取回该空间的知识单元。"""
    return _locator().locate(user_question, app_requirement)


@mcp.tool(name="get_all_subspaces")
def get_all_subspaces() -> dict[str, Any]:
    """获取所有知识子空间：返回系统中全部已配置的知识子空间及关联刻度。需要自己挑选空间时用这个。"""
    subspaces = _locator().get_all_subspaces()
    return {"subspaces": subspaces, "count": len(subspaces)}


def _find_space(space_id=None, space_name=None):
    """定位单个 KnowledgeSpace：优先 space_id；其次 name 精确；再 name CONTAINS 模糊。

    返回 (space, ambiguous)：space 为 {id, name}；ambiguous 非空表示按名字命中多个，
    需调用方用更完整名称或空间 id 重新定位。
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
    """空间内知识单元：按空间名（支持精确与 CONTAINS 模糊匹配）或空间 id，
    一次取回该知识空间直接包含的 KnowledgeUnit（CONTAINS_UNIT 一跳），
    每个单元附完整属性（含 KnowledgeProperty 逻辑属性）；
    返回空表示该空间下没有直接包含的单元。

    空间名命中多个时报错并列出候选，换完整名称或空间 id 重试即可。
    单元 id 可继续用于 search_graph / get_entity_details。"""
    limit = max(1, min(int(limit), 100))
    space, ambiguous = _find_space(space_id, space_name)
    if ambiguous:
        raise ValueError(
            "知识空间名称不唯一，请用完整名称或空间 id 重试；候选："
            + "、".join(str(a) for a in ambiguous if a)
        )
    if not space:
        raise ValueError(
            f"未找到该知识空间: space_id={space_id or ''} space_name={space_name or ''}"
        )
    rows = _km().execute_cypher(
        "MATCH (sp:KnowledgeSpace {id:$id})-[:CONTAINS_UNIT]->(ku:KnowledgeUnit) "
        "RETURN ku.id AS id, ku.name AS name, ku.content AS content, labels(ku) AS labels "
        "LIMIT $limit",
        {"id": space["id"], "limit": limit},
    )
    units = [
        {
            "id": r.get("id"),
            "name": r.get("name") or "",
            "content": _truncate(r.get("content")),
            "labels": _as_list(r.get("labels")),
        }
        for r in (rows or [])
    ]
    _attach_full_properties(units)
    return {"space": space, "units": units, "count": len(units)}
