from docker.configuration import getenv as config_getenv
import json
import logging
import os
from functools import lru_cache
from typing import Any, Literal

from mcp_services.result import ToolCode, normalize_tool_result, tool_error, tool_success
from mcp_services.runtime import mcp
from mcp_services.dispatch import maybe_submit

LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _graph_components():
    from algorithm.ans.QA.config import Config
    from algorithm.ans.QA.entity_linker import EntityLinker
    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    config = Config.load()
    km_client = KnowledgeMapClient()
    return km_client, EntityLinker(km_client, config)


def _search_neighbors(
    node_id: str,
    direction: str,
    limit: int,
    relationship_types: list[str] | None = None,
) -> dict[str, Any]:
    """经知识地图接口查询一跳邻居（自动带权限过滤）。

    只返回 KnowledgeUnit 标签的邻居（Term/问题/方案/DataTable 等都是 KnowledgeUnit），
    避免把知识空间、刻度、逻辑属性等无关节点一并带出。
    """
    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    km = KnowledgeMapClient()

    # 起点名称
    rows = km.execute_cypher(
        "MATCH (n {id:$id}) RETURN n.name AS name LIMIT 1",
        {"id": node_id},
    )
    start_name = rows[0].get("name", "") or "" if rows else ""

    # 方向过滤用"有向边模式"而非 startNode()/endNode()——接口受限引擎可能不支持这两个函数。
    # KnowledgeUnit 标签放在节点模式里约束邻居（Term/问题/方案/DataTable 等都是 KnowledgeUnit），
    # 避免把知识空间、刻度、逻辑属性等无关节点一起带出。
    if direction == "outgoing":
        pattern = "MATCH (n {id:$id})-[r]->(m:KnowledgeUnit) "
    elif direction == "incoming":
        pattern = "MATCH (n {id:$id})<-[r]-(m:KnowledgeUnit) "
    else:
        pattern = "MATCH (n {id:$id})-[r]-(m:KnowledgeUnit) "

    params: dict[str, Any] = {"id": node_id, "limit": limit}
    if relationship_types:
        pattern += "WHERE type(r) IN $relationship_types "
        params["relationship_types"] = relationship_types

    statement = (
        pattern +
        "RETURN type(r) AS rel, m.id AS id, m.name AS name, labels(m) AS labels, "
        "m.content AS content, m.resource_type AS resource_type "
        "LIMIT $limit"
    )
    rows = km.execute_cypher(statement, params)

    # 批量取邻居的逻辑属性（KnowledgeProperty），避免对每个邻居单独查询
    logical_by_owner: dict[str, list[dict[str, Any]]] = {}
    neighbor_ids = [r.get("id") for r in rows if r.get("id")]
    if neighbor_ids:
        try:
            logical_rows = km.execute_cypher(
                "MATCH (m) WHERE m.id IN $ids "
                "MATCH (m)-[:HAS_LOGICAL_PROPERTY]->(p:KnowledgeProperty) "
                "RETURN m.id AS owner_id, p",
                {"ids": neighbor_ids},
            )
            for lrow in logical_rows or []:
                owner = lrow.get("owner_id")
                if not owner:
                    continue
                entry = {}
                for k, v in lrow.items():
                    key = k.split(".", 1)[1] if isinstance(k, str) and "." in k else k
                    if key in ("owner_id", "properties_json"):
                        continue  # owner_id 归组用、properties_json 不对外暴露；其余逻辑属性字段全保留
                    entry[key] = v
                logical_by_owner.setdefault(str(owner), []).append(entry)
        except Exception as exc:
            LOGGER.warning("邻居逻辑属性拉取失败，跳过：%s", exc)

    triples: list[dict[str, Any]] = []
    found_entities: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    max_content = int(config_getenv("GRAPH_NEIGHBOR_CONTENT_MAX", "1200"))
    head = {"id": node_id, "name": start_name, "labels": [], "properties": {}}
    for row in rows:
        mid = row.get("id", "")
        mname = row.get("name", "") or ""
        mlabels = row.get("labels") or []
        content = (row.get("content") or "") or ""
        if len(content) > max_content:
            content = content[:max_content] + "…"
        tail = {
            "id": mid,
            "name": mname,
            "labels": mlabels,
            "content": content,
            "resource_type": row.get("resource_type") or "",
            "properties": {
                "content": content,
                "resource_type": row.get("resource_type") or "",
                "logical_properties": logical_by_owner.get(str(mid), []),
            },
        }
        triples.append(
            {
                "head_entity": head,
                "relation": row.get("rel", ""),
                "relationship_properties": {},
                "tail_entity": tail,
            }
        )
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            found_entities.append(tail)

    return {
        "depth": 1,
        "triples": triples,
        "found_entities": found_entities,
        "has_more": bool(found_entities),
        "filters": {
            "relationship_types": relationship_types or [],
            "direction": direction,
            "limit": limit,
        },
    }


@mcp.tool(name="link_entities")
def link_entities(
    keywords: list[str] | str,
    question: str = "",
    space_id: str | None = None,
    space_name: str | None = None,
) -> dict[str, Any]:
    """实体链接：将用户关键词对齐到知识图谱中的 KnowledgeUnit 节点。name 模糊匹配（CONTAINS），精确匹配项排最前。返回候选单元列表，含相关性评分和 LLM 判断理由。

    术语范围可用 `space_id` / `space_name` 圈定（对应术语所在的知识空间），
    此时按空间包含关系（CONTAINS_UNIT）取候选，不再依赖 resource_type 里的固定标识；
    空间名支持精确/包含模糊匹配，命中多个时会要求用更完整的名称或空间 id 重试。
    两者都不给时退化为原有行为。"""
    queued = maybe_submit(
        "mcp.graph.link_entities",
        {"keywords": keywords, "question": question, "space_id": space_id, "space_name": space_name},
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        if isinstance(keywords, str):
            try:
                keywords = json.loads(keywords)
            except json.JSONDecodeError:
                keywords = [keywords]
        resolved_space_id = (space_id or "").strip() or None
        if not resolved_space_id and (space_name or "").strip():
            # 复用与 search_space_units 相同的空间解析口径（id → 精确名 → 模糊名），
            # 保证两个工具的报错和消歧行为一致。
            from mcp_services.domains.graph.locator import _find_space

            space, ambiguous = _find_space(None, space_name)
            if ambiguous:
                return tool_error(
                    ToolCode.INVALID_ARGUMENT,
                    "知识空间名称不唯一，请用完整名称或空间 id",
                    {"candidates": ambiguous},
                )
            if not space:
                return tool_error(
                    ToolCode.GRAPH_FAILED,
                    "未找到该知识空间",
                    {"space_id": space_id, "space_name": space_name},
                )
            resolved_space_id = space.get("id")
        _client, entity_linker = _graph_components()
        result = entity_linker.link_entities_with_llm_judge(
            question, keywords=keywords, space_id=resolved_space_id
        )
        candidates = [
            {
                "name": entity.get("properties", {}).get("name", entity.get("qid", "")),
                "id": entity.get("qid", ""),
                "labels": entity.get("labels", []),
                "score": entity.get("relevance_score", 0),
                "is_relevant": entity.get("is_relevant", False),
                "llm_judgment": entity.get("reason", ""),
            }
            for entity in result.get("all_candidates", [])
        ]
        return tool_success({"candidates": candidates}, "实体链接完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "实体链接失败", str(exc))


# 干系人本体的 MetaClass uid（固定值；如本体 uid 变更则更新此常量）
STAKEHOLDER_META_UID = "ac611c54-8584-4e02-8056-f222eaca9cff"


@mcp.tool(name="get_stakeholders")
def get_stakeholders() -> dict[str, Any]:
    """获取当前知识图谱中的所有干系人实例。

    干系人 = KnowledgeUnit 中 resource_type 指向 Stakeholder 本体（uid 固定）的节点。
    返回干系人列表 [{id, name}, ...]，供 Agent 按问题语义筛选。
    """
    queued = maybe_submit("mcp.graph.get_stakeholders", {}, resource_class="io.graph")
    if queued is not None:
        return queued

    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    km = KnowledgeMapClient()
    try:
        rows = km.execute_cypher(
            "MATCH (n:KnowledgeUnit) WHERE n.resource_type CONTAINS $uid "
            "RETURN n.id AS id, n.name AS name LIMIT 50",
            {"uid": STAKEHOLDER_META_UID},
        )
        stakeholders = [{"id": r.get("id"), "name": r.get("name")} for r in rows]
        return tool_success({"stakeholders": stakeholders}, "干系人获取完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "干系人获取失败", str(exc))


@mcp.tool(name="query_entity_properties")
def query_entity_properties(entity_id: str) -> dict[str, Any]:
    """实体属性查询：根据实体ID获取节点的标签列表和全部属性。完整属性 = 节点自身字段 + 关联的 KnowledgeProperty 逻辑属性。"""
    queued = maybe_submit(
        "mcp.graph.query_entity",
        {"entity_id": entity_id},
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        _client, entity_linker = _graph_components()
        node = entity_linker.get_node_full_info(entity_id)
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "实体属性查询失败", str(exc))
    if not node:
        return tool_error(ToolCode.GRAPH_FAILED, f"未找到实体: {entity_id}")
    return tool_success(
        {
            "entity_id": entity_id,
            "name": node.get("name", ""),
            "labels": node.get("labels", []),
            "properties": node.get("properties", {}),
        },
        "实体属性查询完成",
    )


@mcp.tool(name="search_graph")
def search_graph(
    start_entity_id: str,
    question: str = "",
    relationship_types: list[str] | None = None,
    direction: Literal["outgoing", "incoming", "both"] = "both",
    limit: int = 50,
) -> dict[str, Any]:
    """图谱邻居查询：从指定节点出发沿关系边探索一跳邻居（经知识地图接口，自动带权限过滤）。固定单跳，没有 depth 参数，不要传 depth；多跳由 Agent 逐层调用本工具。不约束节点类型与关系类型。返回全部邻接 triples，邻居带 name/labels/content/resource_type 与逻辑属性。

    使用方式：search_graph(start_entity_id="<UUID>", direction="outgoing", question=用户问题)"""
    queued = maybe_submit(
        "mcp.graph.search",
        {
            "start_entity_id": start_entity_id,
            "question": question,
            "relationship_types": relationship_types,
            "direction": direction,
            "limit": limit,
        },
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        result = _search_neighbors(start_entity_id, direction, limit, relationship_types)

        if question:
            result["navigation_context"] = question
        result["start_entity_id"] = start_entity_id
        return normalize_tool_result(result, ToolCode.GRAPH_FAILED, "图谱搜索完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "图谱搜索失败", str(exc))


KNOWLEDGE_RELATION = "KNOWLEDGE_RELATION"


def _attach_full_properties(units: list[dict[str, Any]]) -> None:
    """为候选补齐完整节点属性与逻辑属性（等价于对每个候选执行一次 query_entity_properties）。
    问题类/方案类节点直接携带的信息很少，详细内容（含 KnowledgeProperty 逻辑属性）
    需要这一步才能拿到。在工具内一步补齐，避免 Agent 拿到候选后再逐个调属性查询。
    """
    try:
        _client, entity_linker = _graph_components()
    except Exception as exc:
        LOGGER.warning("图谱组件初始化失败，候选属性补齐跳过：%s", exc)
        return
    for unit in units:
        try:
            info = entity_linker.get_node_full_info(unit["id"])
        except Exception as exc:
            LOGGER.warning("补齐候选 %s 完整属性失败：%s", unit.get("id"), exc)
            info = None
        props = (info or {}).get("properties") or {}
        # 列表型工具的单元顶层已带 id/name/content/labels，properties 里再放一份纯属重复；
        # logical_properties_json 是 logical_properties 的原始 JSON 串，也只留解析版。
        for redundant in ("id", "name", "content", "labels", "logical_properties_json"):
            props.pop(redundant, None)
        unit["properties"] = props


def _list_neighbor_units(
    node_id: str,
    limit: int,
    relationship_types: list[str] | None = None,
    direction: str = "both",
    enrich_properties: bool = True,
) -> list[dict[str, Any]]:
    """确定性查询起始单元的一跳邻接知识单元；默认再为每个候选补齐完整属性与逻辑属性。

    供问题类/方案类候选导航复用：一次调用返回候选及其完整属性，
    Agent 据此直接判类/选候选，无需再逐个 query_entity_properties。
    默认沿 KNOWLEDGE_RELATION 关系查询。
    """
    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    km = KnowledgeMapClient()

    if direction == "outgoing":
        pattern = "MATCH (s {id:$id})-[r]->(m) "
    elif direction == "incoming":
        pattern = "MATCH (s {id:$id})<-[r]-(m) "
    else:
        pattern = "MATCH (s {id:$id})-[r]-(m) "

    params: dict[str, Any] = {"id": node_id, "limit": limit}
    rels = relationship_types if relationship_types else [KNOWLEDGE_RELATION]
    params["relationship_types"] = rels
    statement = (
        pattern +
        "WHERE type(r) IN $relationship_types "
        "RETURN m.id AS id, m.name AS name, m.content AS content, "
        "labels(m) AS labels, type(r) AS rel_type "
        "LIMIT $limit"
    )
    rows = km.execute_cypher(statement, params)

    units: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        mid = row.get("id") or ""
        if not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)
        units.append(
            {
                "id": mid,
                "name": row.get("name") or "",
                "labels": row.get("labels") or [],
                "content": (row.get("content") or "") or "",
                "rel_type": row.get("rel_type") or "",
            }
        )

    if not units and direction != "both":
        return _list_neighbor_units(node_id, limit, relationship_types, "both", enrich_properties)

    if enrich_properties:
        _attach_full_properties(units)
    return units


@mcp.tool(name="find_problem_classes")
def find_problem_classes(
    stakeholder_id: str,
    limit: int = 20,
    relationship_types: list[str] | None = None,
    direction: str = "both",
) -> dict[str, Any]:
    """问题类获取：从干系人出发，沿 KNOWLEDGE_RELATION 取一跳邻接知识单元（标准模型中即该干系人关联的问题类），一步返回每个候选的完整属性（含 content 与 KnowledgeProperty 逻辑属性）。

    使用方式：find_problem_classes(stakeholder_id="<干系人ID>")。返回的每个 candidate 已带完整 properties，
    Agent 据此直接选出与用户问题相符的问题类，无需再逐个 query_entity_properties。
    方向不匹配自动回退双向；返回空表示本次未检索到该干系人的问题类。"""
    queued = maybe_submit(
        "mcp.graph.find_problem_classes",
        {
            "stakeholder_id": stakeholder_id,
            "limit": limit,
            "relationship_types": relationship_types,
            "direction": direction,
        },
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        candidates = _list_neighbor_units(stakeholder_id, limit, relationship_types, direction)
        return tool_success(
            {"start_entity_id": stakeholder_id, "candidates": candidates, "count": len(candidates)},
            "问题类获取完成",
        )
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "问题类获取失败", str(exc))


@mcp.tool(name="find_solution_classes")
def find_solution_classes(
    problem_id: str,
    limit: int = 20,
    relationship_types: list[str] | None = None,
    direction: str = "both",
) -> dict[str, Any]:
    """方案类获取：从问题类出发，沿 KNOWLEDGE_RELATION 取一跳邻接知识单元（标准模型中即该问题的方案类），一步返回每个候选的完整属性（含 content/solutionTemplate/steps 与 KnowledgeProperty 逻辑属性）。

    使用方式：find_solution_classes(problem_id="<问题类ID>")。返回的每个 candidate 已带完整 properties，
    Agent 据此直接选出对应方案类并按其步骤执行，无需再逐个 query_entity_properties。
    方向不匹配自动回退双向；返回空表示本次未检索到该问题的方案类。"""
    queued = maybe_submit(
        "mcp.graph.find_solution_classes",
        {
            "problem_id": problem_id,
            "limit": limit,
            "relationship_types": relationship_types,
            "direction": direction,
        },
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        candidates = _list_neighbor_units(problem_id, limit, relationship_types, direction)
        return tool_success(
            {"start_entity_id": problem_id, "candidates": candidates, "count": len(candidates)},
            "方案类获取完成",
        )
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "方案类获取失败", str(exc))
