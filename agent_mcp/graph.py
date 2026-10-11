"""Agent 专属图谱工具——通用检索为主、工具内零 LLM。

所有查询经 KnowledgeMapClient → 前端接口，自动带权限过滤；
用户上下文请求头由 app.py 的中间件捕获并随查询透传。
"""

import json
import logging
import os
import re
import time
from functools import lru_cache
from typing import Any, Literal

from agent_mcp.runtime import mcp
from external_services.application import get_user_headers

LOGGER = logging.getLogger(__name__)

# 邻居/候选的 content 截断长度，避免超长文档撑爆 Agent 上下文
CONTENT_MAX = int(os.getenv("AGENT_MCP_CONTENT_MAX", "1200"))

# 只读防线：run_cypher 拒绝包含写子句的语句
_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH\s+DELETE|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|CALL)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _km():
    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    return KnowledgeMapClient()


@lru_cache(maxsize=1)
def _linker():
    from algorithm.ans.QA.config import Config
    from algorithm.ans.QA.entity_linker import EntityLinker

    return EntityLinker(_km(), Config.load())


def _truncate(content: Any) -> str:
    text = (content or "") or ""
    if len(text) > CONTENT_MAX:
        return text[:CONTENT_MAX] + "…"
    return text


def _as_list(value: Any) -> list:
    """网关对 labels() 的返回形状不稳定，统一成列表。"""
    return value if isinstance(value, list) else []


def _attach_full_properties(units: list[dict[str, Any]]) -> None:
    """为候选/单元补齐完整节点属性与逻辑属性（等价于对每个单元执行一次 get_entity_details）。

    与旧 MCP 的 _attach_full_properties 行为一致；实现上用 EntityLinker 而非导入旧 MCP
    （agent-mcp 容器不挂 mcp_services 卷）。
    """
    try:
        linker = _linker()
    except Exception as exc:
        LOGGER.warning("图谱组件初始化失败，单元属性补齐跳过：%s", exc)
        return
    for unit in units:
        try:
            info = linker.get_node_full_info(unit["id"])
        except Exception as exc:
            LOGGER.warning("补齐单元 %s 完整属性失败：%s", unit.get("id"), exc)
            info = None
        props = (info or {}).get("properties") or {}
        # 单元顶层已带 id/name/content/labels，properties 里再放一份纯属重复；
        # logical_properties_json 是原始 JSON 串，只留解析版。
        for redundant in ("id", "name", "content", "labels", "logical_properties_json"):
            props.pop(redundant, None)
        unit["properties"] = props


#: 多空间 IN 查询一旦被网关拒绝就永久降级为逐空间查询，避免每次都白试一轮。
_IN_LIST_UNSUPPORTED = False

#: 根空间 → 子空间 id 的 TTL 缓存（子空间结构基本不变，避免每次检索都多一次图谱往返）。
_SUBSPACE_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_SUBSPACE_TTL = float(os.getenv("AGENT_MCP_SUBSPACE_TTL_SECONDS", "300"))


def _child_space_ids(root_name: str) -> tuple[str, ...]:
    """按【根空间名】取其一跳子空间 id（HAS_CHILD_SPACE）。

    只能按 name 查——locate_knowledge_space 返回的根空间就是名称，不是 id。
    当前环境所有子空间都是根的一跳，因此这些 id 即"该场景下的全部单元容器"。
    """
    name = (root_name or "").strip()
    if not name:
        return ()
    now = time.monotonic()
    cached = _SUBSPACE_CACHE.get(name)
    if cached and cached[0] > now:
        return cached[1]
    try:
        rows = _km().execute_cypher(
            "MATCH (p:KnowledgeSpace {name:$root})-[:HAS_CHILD_SPACE]->(c:KnowledgeSpace) "
            "RETURN c.id AS id ORDER BY c.name",
            {"root": name},
        )
    except Exception as exc:                        # noqa: BLE001
        LOGGER.warning("展开根空间 %s 的子空间失败：%s", name, exc)
        return ()
    ids = tuple(str(r.get("id")) for r in (rows or []) if r.get("id"))
    _SUBSPACE_CACHE[name] = (now + _SUBSPACE_TTL, ids)
    return ids


def _space_id_by_name(name: str) -> str:
    """按空间名反查 id：先精确，再 CONTAINS；命中多个一律判为不确定（返回空串）。"""
    key = (name or "").strip()
    if not key:
        return ""
    exact = _km().execute_cypher(
        "MATCH (sp:KnowledgeSpace {name:$name}) RETURN sp.id AS id LIMIT 2",
        {"name": key},
    )
    if len(exact) == 1:
        return str(exact[0].get("id") or "")
    if len(exact) > 1:
        LOGGER.warning("空间名 %s 精确命中多个，无法唯一确定 id", key)
        return ""
    fuzzy = _km().execute_cypher(
        "MATCH (sp:KnowledgeSpace) WHERE sp.name CONTAINS $name RETURN sp.id AS id LIMIT 3",
        {"name": key},
    )
    if len(fuzzy) == 1:
        return str(fuzzy[0].get("id") or "")
    if len(fuzzy) > 1:
        LOGGER.warning("空间名 %s 模糊命中多个，无法唯一确定 id", key)
    return ""


@mcp.tool(name="search_entities")
def search_entities(
    keywords: list[str] | str,
    space_id: str | None = None,
    root_space_name: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """实体检索：按名称关键词查找图谱节点，不限节点类型；名称精确命中排最前，CONTAINS 模糊兜底。自动带权限过滤。

    【检索范围】三选一，优先级从高到低：
    - space_id：只在该单个知识空间直接包含的单元里检索（CONTAINS_UNIT 一跳）；
    - root_space_name：按【根空间名称】自动展开其全部一跳子空间，在这些子空间内一起检索。
      典型用法：先 locate_knowledge_space 拿到根空间，再用该根空间名检索，
      即"在该场景下的全部子空间里找实体"，范围由工具保证完整；
    - 都不给：全图 KnowledgeUnit 检索。

    返回 {candidates: [{id, name, labels, resource_type, content, space_id, space_name,
    matched_keyword, exact_match}], count, space_scope}。
    用候选的 id 继续调用 search_graph（探邻居）或 get_entity_details（看详情）。
    未命中返回空 candidates：可换更短或更完整的关键词，或去掉空间范围扩大重试。"""
    if isinstance(keywords, str):
        try:
            parsed = json.loads(keywords)
            keywords = parsed if isinstance(parsed, list) else [keywords]
        except json.JSONDecodeError:
            keywords = [keywords]
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not keywords:
        return {"candidates": [], "count": 0, "space_scope": {"mode": "none", "space_ids": []}}
    limit = max(1, min(int(limit), 50))

    # ── 解析检索范围 ──
    scope_ids: list[str] = []
    mode = "global"
    if (space_id or "").strip():
        scope_ids = [str(space_id).strip()]
        mode = "space_id"
    elif (root_space_name or "").strip():
        mode = "root_space_name"
        scope_ids = list(_child_space_ids(root_space_name))
        if not scope_ids:
            # 根下没有子空间（或展开失败）：退化为按名字定位该空间自身，避免静默变成全图检索
            self_id = _space_id_by_name(root_space_name)
            if self_id:
                scope_ids = [self_id]
                mode = "root_space_name:self"
            else:
                mode = "root_space_name:unresolved"

    multi_stmt = (
        "MATCH (sp:KnowledgeSpace)-[:CONTAINS_UNIT]->(n:KnowledgeUnit) "
        "WHERE sp.id IN $space_ids AND n.name CONTAINS $keyword "
        "RETURN n.id AS id, n.name AS name, n.content AS content, labels(n) AS labels, "
        "n.resource_type AS resource_type, sp.id AS space_id, sp.name AS space_name "
        "LIMIT $limit"
    )
    single_stmt = (
        "MATCH (sp:KnowledgeSpace {id:$space_id})-[:CONTAINS_UNIT]->(n:KnowledgeUnit) "
        "WHERE n.name CONTAINS $keyword "
        "RETURN n.id AS id, n.name AS name, n.content AS content, labels(n) AS labels, "
        "n.resource_type AS resource_type, sp.id AS space_id, sp.name AS space_name "
        "LIMIT $limit"
    )
    plain_stmt = (
        "MATCH (n:KnowledgeUnit) "
        "WHERE n.name CONTAINS $keyword "
        "RETURN n.id AS id, n.name AS name, n.content AS content, labels(n) AS labels, "
        "n.resource_type AS resource_type "
        "LIMIT $limit"
    )

    def _rows_for(keyword: str) -> list[dict[str, Any]]:
        global _IN_LIST_UNSUPPORTED
        if not scope_ids:
            return _km().execute_cypher(plain_stmt, {"keyword": keyword, "limit": limit}) or []
        if len(scope_ids) == 1:
            return _km().execute_cypher(
                single_stmt, {"keyword": keyword, "space_id": scope_ids[0], "limit": limit}
            ) or []
        if not _IN_LIST_UNSUPPORTED:
            try:
                rows = _km().execute_cypher(
                    multi_stmt,
                    {"keyword": keyword, "space_ids": scope_ids, "limit": limit},
                ) or []
                # 防"IN 过滤被网关静默忽略"：返回行的空间必须全部落在范围内，否则降级
                if all(r.get("space_id") in scope_ids for r in rows):
                    return rows
                LOGGER.warning("IN $space_ids 过滤疑似被忽略，降级为逐空间查询")
            except Exception as exc:                # noqa: BLE001
                LOGGER.warning("多空间 IN 查询不可用（%s），降级为逐空间查询", exc)
            _IN_LIST_UNSUPPORTED = True
        rows = []
        for sid in scope_ids:
            rows.extend(
                _km().execute_cypher(
                    single_stmt, {"keyword": keyword, "space_id": sid, "limit": limit}
                ) or []
            )
        return rows

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for keyword in keywords:
        for row in _rows_for(keyword):
            qid = row.get("id") or ""
            if not qid or qid in seen:
                continue
            seen.add(qid)
            name = row.get("name") or ""
            candidates.append({
                "id": qid,
                "name": name,
                "labels": _as_list(row.get("labels")),
                "resource_type": row.get("resource_type") or "",
                "content": _truncate(row.get("content")),
                "space_id": row.get("space_id") or "",
                "space_name": row.get("space_name") or "",
                "matched_keyword": keyword,
                "exact_match": bool(keyword) and name.strip() == keyword,
            })
    # 名称精确命中置顶；其余按空间名、名称稳定排序，便于同一场景的候选聚在一起
    candidates.sort(key=lambda c: (not c["exact_match"], c["space_name"], c["name"]))
    return {
        "candidates": candidates,
        "count": len(candidates),
        "space_scope": {"mode": mode, "space_ids": scope_ids},
    }


@mcp.tool(name="search_graph")
def search_graph(
    start_entity_id: str,
    direction: Literal["outgoing", "incoming", "both"] = "both",
    relationship_types: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """图谱邻居探索：从指定节点出发取一跳邻居，不限节点类型与关系类型，自动带权限过滤。

    固定单跳（没有 depth 参数）；多跳需要连续多次调用，把上一次返回的邻居 id 作为新的 start_entity_id。
    只返回 KnowledgeUnit 标签的邻居；知识空间、逻辑属性等结构节点不在此返回，
    节点详情用 get_entity_details。
    返回 {triples: [{head_entity, relation, tail_entity}], found_entities, count}，
    邻居带 name/labels/content/resource_type 与逻辑属性（logical_properties）。起点不存在时报错。"""
    km = _km()
    limit = max(1, min(int(limit), 200))

    rows = km.execute_cypher(
        "MATCH (n {id:$id}) RETURN n.name AS name LIMIT 1",
        {"id": start_entity_id},
    )
    if not rows:
        raise ValueError(f"未找到起点节点: {start_entity_id}")
    start_name = rows[0].get("name") or ""

    # 方向过滤用"有向边模式"而非 startNode()/endNode()——接口受限引擎可能不支持这两个函数。
    # KnowledgeUnit 标签放在节点模式里约束邻居，避免把知识空间、刻度、逻辑属性等结构节点带出。
    if direction == "outgoing":
        pattern = "MATCH (n {id:$id})-[r]->(m:KnowledgeUnit) "
    elif direction == "incoming":
        pattern = "MATCH (n {id:$id})<-[r]-(m:KnowledgeUnit) "
    else:
        pattern = "MATCH (n {id:$id})-[r]-(m:KnowledgeUnit) "

    params: dict[str, Any] = {"id": start_entity_id, "limit": limit}
    if relationship_types:
        pattern += "WHERE type(r) IN $relationship_types "
        params["relationship_types"] = relationship_types

    statement = (
        pattern
        + "RETURN type(r) AS rel, m.id AS id, m.name AS name, labels(m) AS labels, "
        + "m.content AS content, m.resource_type AS resource_type "
        + "LIMIT $limit"
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
                entry: dict[str, Any] = {}
                for k, v in lrow.items():
                    key = k.split(".", 1)[1] if isinstance(k, str) and "." in k else k
                    if key in ("owner_id", "properties_json"):
                        continue  # owner_id 归组用、properties_json 不对外暴露
                    entry[key] = v
                logical_by_owner.setdefault(str(owner), []).append(entry)
        except Exception as exc:
            LOGGER.warning("邻居逻辑属性拉取失败，跳过：%s", exc)

    triples: list[dict[str, Any]] = []
    found_entities: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    head = {"id": start_entity_id, "name": start_name, "labels": [], "properties": {}}
    for row in rows:
        mid = row.get("id") or ""
        content = _truncate(row.get("content"))
        tail = {
            "id": mid,
            "name": row.get("name") or "",
            "labels": _as_list(row.get("labels")),
            "content": content,
            "resource_type": row.get("resource_type") or "",
            "properties": {
                "content": content,
                "resource_type": row.get("resource_type") or "",
                "logical_properties": logical_by_owner.get(str(mid), []),
            },
        }
        triples.append({
            "head_entity": head,
            "relation": row.get("rel") or "",
            "tail_entity": tail,
        })
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            found_entities.append(tail)

    return {
        "start_entity_id": start_entity_id,
        "depth": 1,
        "triples": triples,
        "found_entities": found_entities,
        "count": len(found_entities),
    }


@mcp.tool(name="get_entity_details")
def get_entity_details(entity_id: str) -> dict[str, Any]:
    """实体详情：按 id 取节点完整信息——节点自身字段 + 关联的 KnowledgeProperty 逻辑属性。自动带权限过滤。

    返回 {entity_id, name, labels, properties}；properties 含 content/resource_type 等字段与 logical_properties 列表。
    id 来自 search_entities / search_graph 的返回；未找到时报错。"""
    node = _linker().get_node_full_info(entity_id)
    if not node:
        raise ValueError(f"未找到实体: {entity_id}")
    return {
        "entity_id": entity_id,
        "name": node.get("name", ""),
        "labels": node.get("labels", []),
        "properties": node.get("properties", {}),
    }


# 干系人本体的 MetaClass uid（固定值；如本体 uid 变更则更新此常量）
STAKEHOLDER_META_UID = "ac611c54-8584-4e02-8056-f222eaca9cff"


@mcp.tool(name="get_stakeholders")
def get_stakeholders() -> dict[str, Any]:
    """列出当前知识图谱中的全部干系人实例。

    干系人 = KnowledgeUnit 中 resource_type 指向 Stakeholder 本体（uid 固定）的节点。
    返回 {stakeholders: [{id, name}, ...]}。
    """
    rows = _km().execute_cypher(
        "MATCH (n:KnowledgeUnit) WHERE n.resource_type CONTAINS $uid "
        "RETURN n.id AS id, n.name AS name LIMIT 50",
        {"uid": STAKEHOLDER_META_UID},
    )
    stakeholders = [{"id": r.get("id"), "name": r.get("name")} for r in rows or []]
    return {"stakeholders": stakeholders}


@mcp.tool(name="run_cypher")
def run_cypher(
    statement: str,
    params: dict[str, Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """只读 Cypher 查询：直接对知识图谱执行 Cypher（经知识地图接口，自动带权限过滤）。当 search_entities / search_graph 组合无法满足时使用（聚合统计、多跳模式匹配、复杂过滤等）。

    网关限制（务必遵守）：
    - 只读：禁止 CREATE/MERGE/DELETE/SET/REMOVE/DROP/CALL 等写操作与子查询
    - 不支持 startNode()/endNode()，方向过滤用有向边模式 (a)-[r]->(b)
    - RETURN 整个节点（如 RETURN n）会被网关展开成 "n.字段" 的扁平标量列；需要什么字段就显式写 RETURN n.id AS id, n.name AS name
    - 常用标签：KnowledgeUnit（知识单元）、KnowledgeSpace（知识空间）、KnowledgeProperty（逻辑属性）
    - 常用关系：CONTAINS_UNIT（空间→单元）、HAS_LOGICAL_PROPERTY（单元→逻辑属性）、KNOWLEDGE_RELATION（单元间关系）
    - 标签/关系名不确定时先调 get_graph_schema 查全量清单
    - 语句不带 LIMIT 时自动补 LIMIT 上限

    返回 {rows: [...], count}。"""
    statement = (statement or "").strip().rstrip(";").strip()
    if not statement:
        raise ValueError("Cypher 语句为空")
    if _WRITE_CLAUSE.search(statement):
        raise ValueError("只读查询：禁止 CREATE/MERGE/DELETE/SET/REMOVE/DROP/CALL 等写操作")
    limit = max(1, min(int(limit), 1000))
    if not re.search(r"\bLIMIT\b", statement, re.IGNORECASE):
        statement = f"{statement} LIMIT {limit}"
    rows = _km().execute_cypher(statement, params or {})
    return {"rows": rows, "count": len(rows)}


# 图谱 schema 按用户缓存：不同权限看到的结构/计数不同。key 用身份类请求头，
# 不含会随会话过期的 authorization（否则缓存永远命不中）。
_SCHEMA_CACHE: dict[tuple, dict[str, Any]] = {}


def _schema_cache_key() -> tuple:
    headers = get_user_headers()
    return tuple(sorted((k, v) for k, v in headers.items() if k != "authorization"))


def _load_schema() -> dict[str, Any]:
    km = _km()
    label_rows = km.execute_cypher(
        "MATCH (n) RETURN labels(n) AS labels, count(n) AS cnt ORDER BY cnt DESC", {}
    )
    rel_rows = km.execute_cypher(
        "MATCH ()-[r]->() RETURN type(r) AS rel_type, count(r) AS cnt ORDER BY cnt DESC", {}
    )
    rt_rows = km.execute_cypher(
        "MATCH (n:KnowledgeUnit) WHERE n.resource_type IS NOT NULL "
        "WITH n.resource_type AS rt, count(n) AS cnt, collect(n.name) AS names "
        "RETURN rt, cnt, names[0] AS sample_name ORDER BY cnt DESC LIMIT 100",
        {},
    )
    return {
        "labels": [
            {"labels": _as_list(r.get("labels")), "count": r.get("cnt")}
            for r in label_rows or []
        ],
        "relationship_types": [
            {"rel_type": r.get("rel_type") or "", "count": r.get("cnt")}
            for r in rel_rows or []
        ],
        "resource_types": [
            {
                "resource_type": r.get("rt") or "",
                "count": r.get("cnt"),
                "sample_name": r.get("sample_name") or "",
            }
            for r in rt_rows or []
        ],
    }


@mcp.tool(name="get_graph_schema")
def get_graph_schema() -> dict[str, Any]:
    """图谱结构自描述：写 Cypher 前先看这里，避免瞎猜标签/关系名。返回（均按数量降序）：
    - labels：全部节点标签及数量
    - relationship_types：全部关系类型及数量
    - resource_types：KnowledgeUnit 的本体类型分布，每类带一个样本节点名——resource_type 值是指向本体的 uid（不直观），用 sample_name 识别类型含义

    "哪类节点最多"等统计问题可直接用这里的计数回答，不必再写查询。
    计数按当前用户权限范围统计；结果按用户缓存，图谱结构变更后重启服务刷新。"""
    key = _schema_cache_key()
    cached = _SCHEMA_CACHE.get(key)
    if cached is None:
        cached = _load_schema()
        if len(_SCHEMA_CACHE) >= 32:
            _SCHEMA_CACHE.clear()
        _SCHEMA_CACHE[key] = cached
    return cached
