"""知识空间初始化、刻度查询与抽取结果入库逻辑。"""
import itertools
import logging
from typing import Any
from uuid import uuid4

from .neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


def init_knowledge_space(
    dimension_scales: dict[str, list[str]],
    neo4j_client: Neo4jClient = None,
) -> dict[str, Any]:
    """
    初始化知识空间：
    1. 检查维度是否存在
    2. 检查/创建刻度节点（重复检查）
    3. 创建 KnowledgeSpace 节点，命名规则：维度.刻度+维度.刻度+...
    4. 将 KnowledgeSpace 与刻度节点以 BELONGS_TO 关系相连

    参数：
        dimension_scales: {维度名: [刻度名列表]}，例如：
            {"时间": ["年月"], "空间": ["集团", "子公司"], "抽象层级": ["逻辑"]}
        neo4j_client: 可选，外部传入的 Neo4j 客户端

    返回：
        {"success": true, "name": "...", "message": "..."}
        或 {"success": false, "detail": "..."}
    """
    if not dimension_scales:
        return {"success": False, "detail": "维度-刻度映射不能为空"}

    client = neo4j_client or Neo4jClient()
    own_client = neo4j_client is None

    try:
        # 1. 验证所有维度存在，展平为 (dim, scale) 对列表
        all_pairs: list[tuple[str, str]] = []
        dim_names = list(dimension_scales.keys())

        for dim_name in dim_names:
            dim_name = (dim_name or "").strip()
            if not dim_name:
                return {"success": False, "detail": "维度名称不能为空"}

            dim_node = client.run_single(
                "MATCH (d:Dimension {name: $name}) RETURN d",
                {"name": dim_name},
            )
            if dim_node is None:
                return {"success": False, "detail": f"维度不存在: {dim_name}"}

            scale_list = dimension_scales[dim_names[dim_names.index(dim_name)]]
            # 重新获取正确的 scale_list
            for original_key, scale_list in dimension_scales.items():
                if (original_key or "").strip() == dim_name:
                    break
            else:
                scale_list = []

            for scale_name in scale_list:
                scale_name = (scale_name or "").strip()
                if not scale_name:
                    continue
                all_pairs.append((dim_name, scale_name))

        if not all_pairs:
            return {"success": False, "detail": "至少需要一个维度-刻度对"}

        # 2. 处理刻度节点：检查/创建，并确保与维度相连
        scale_names_set: set[str] = set()
        for dim_name, scale_name in all_pairs:
            scale_names_set.add(scale_name)

            # 检查刻度节点是否存在
            scale_node = client.run_single(
                "MATCH (s:Scale {name: $name}) RETURN s",
                {"name": scale_name},
            )
            if scale_node is None:
                logger.info("创建刻度节点: %s", scale_name)
                client.create_node("Scale", {"name": scale_name})

            # 确保维度与刻度相连
            existing_rel = client.run_single(
                "MATCH (d:Dimension {name: $dimName})-[r:包含|BELONGS_TO]-(s:Scale {name: $scaleName}) RETURN r",
                {"dimName": dim_name, "scaleName": scale_name},
            )
            if existing_rel is None:
                logger.info("创建维度-刻度关系: %s -[:包含]-> %s", dim_name, scale_name)
                client.create_relationship(
                    "Dimension", {"name": dim_name},
                    "Scale", {"name": scale_name},
                    "包含",
                )

        # 3. 构建知识空间名称: 维度.刻度+维度.刻度+...
        ks_name_parts = [f"{d}.{s}" for d, s in all_pairs]
        ks_name = "+".join(ks_name_parts)

        # 4. 检查知识空间是否已存在
        existing_ks = client.run_single(
            "MATCH (ks:KnowledgeSpace {name: $name}) RETURN ks",
            {"name": ks_name},
        )
        if existing_ks is not None:
            return {
                "success": True,
                "name": ks_name,
                "message": f"知识空间已存在: {ks_name}",
                "existed": True,
            }

        # 5. 创建知识空间节点
        client.create_node("KnowledgeSpace", {
            "name": ks_name,
            "spaceType": "custom",
            "spaceDescription": f"自定义知识空间: {ks_name}",
        })
        logger.info("创建知识空间节点: %s", ks_name)

        # 6. 连接 KnowledgeSpace 到各 Scale 节点 (BELONGS_TO)
        for scale_name in scale_names_set:
            client.create_relationship(
                "KnowledgeSpace", {"name": ks_name},
                "Scale", {"name": scale_name},
                "BELONGS_TO",
            )

        return {
            "success": True,
            "name": ks_name,
            "message": f"知识空间已创建: {ks_name}",
            "existed": False,
            "dimensions": dim_names,
            "scales": sorted(scale_names_set),
        }

    except Exception as e:
        logger.exception("初始化知识空间失败")
        return {"success": False, "detail": str(e)}
    finally:
        if own_client:
            client.close()


def get_dimension_scales(
    dimension: str,
    neo4j_client: Neo4jClient = None,
) -> dict[str, Any]:
    """
    根据维度名称返回该维度下的所有刻度。

    参数：
        dimension: 维度名称
        neo4j_client: 可选，外部传入的 Neo4j 客户端

    返回：
        {"success": true, "dimension": "...", "scales": [{...}, ...]}
    """
    dimension = (dimension or "").strip()
    if not dimension:
        return {"success": False, "detail": "维度名称不能为空"}

    client = neo4j_client or Neo4jClient()
    own_client = neo4j_client is None

    try:
        dim_node = client.run_single(
            "MATCH (d:Dimension {name: $name}) RETURN d",
            {"name": dimension},
        )
        if dim_node is None:
            return {
                "success": True,
                "dimension": dimension,
                "scales": [],
                "message": f"维度不存在: {dimension}",
            }

        records = client.run(
            "MATCH (d:Dimension {name: $name})-[r:包含|BELONGS_TO]-(s:Scale) RETURN s",
            {"name": dimension},
        )
        scales = [r["s"] for r in records]

        return {
            "success": True,
            "dimension": dimension,
            "scales": scales,
        }

    except Exception as e:
        logger.exception("查询维度刻度失败")
        return {"success": False, "detail": str(e)}
    finally:
        if own_client:
            client.close()


def store_extraction_to_neo4j(
    graph: dict[str, Any],
    neo4j_client: Neo4jClient = None,
) -> dict[str, Any]:
    """
    将知识抽取结果（graph 结构）存入 Neo4j。
    按 uid 去重：同一 uid 的节点只创建一次。

    参数：
        graph: {"nodes": [...], "relationships": [...]}
        neo4j_client: 可选

    返回：
        {"success": true, "nodes_created": N, "nodes_merged": M, "relations_created": R}
    """
    nodes = graph.get("nodes") or []
    relationships = graph.get("relationships") or []

    if not nodes:
        return {"success": False, "detail": "没有可存储的节点"}

    client = neo4j_client or Neo4jClient()
    own_client = neo4j_client is None

    nodes_created = 0
    nodes_merged = 0
    relations_created = 0
    errors = []

    try:
        # ── 写入节点（按 uid 去重）──
        for node in nodes:
            uid = node.get("uid", "")
            label = node.get("label", "Element")
            props = node.get("properties") or {}

            if not uid:
                continue

            # 安全检查：只允许字母数字下划线中文
            safe_label = "".join(
                c if (c.isalnum() or c == "_" or '一' <= c <= '鿿') else "_"
                for c in str(label)
            ).strip("_") or "Element"

            # 确保标签以字母开头（Neo4j 要求）
            if safe_label[0].isdigit():
                safe_label = f"L_{safe_label}"

            # MERGE: 按 uid 检查是否存在
            existing = client.run_single(
                f"MATCH (n:{safe_label} {{uid: $uid}}) RETURN n",
                {"uid": uid},
            )

            if existing:
                # 已存在：合并属性
                nodes_merged += 1
                set_clauses = []
                set_params: dict[str, Any] = {"uid": uid}
                for k, v in props.items():
                    if v is not None:
                        param_key = f"p_{k}"
                        set_clauses.append(f"n.{k} = ${param_key}")
                        set_params[param_key] = v
                if set_clauses:
                    client.run(
                        f"MATCH (n:{safe_label} {{uid: $uid}}) SET {', '.join(set_clauses)}",
                        set_params,
                    )
            else:
                # 新建节点
                nodes_created += 1
                set_items = ["n.uid = $uid"]
                set_params = {"uid": uid}
                for k, v in props.items():
                    if v is not None:
                        param_key = f"p_{k}"
                        set_items.append(f"n.{k} = ${param_key}")
                        set_params[param_key] = v

                client.run(
                    f"CREATE (n:{safe_label} {{uid: $uid}}) SET {', '.join(set_items)}",
                    set_params,
                )

        # ── 写入关系 ──
        for rel in relationships:
            from_uid = rel.get("from_uid", "")
            to_uid = rel.get("to_uid", "")
            rel_type = rel.get("type", "RELATED")

            if not from_uid or not to_uid:
                continue

            # 安全关系名
            safe_rel_type = "".join(
                c if (c.isalnum() or c == "_" or '一' <= c <= '鿿') else "_"
                for c in str(rel_type)
            ).strip("_") or "RELATED"

            try:
                client.run(
                    f"MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}}) "
                    f"MERGE (a)-[:{safe_rel_type}]->(b)",
                    {"from_uid": from_uid, "to_uid": to_uid},
                )
                relations_created += 1
            except Exception as e:
                errors.append(f"关系创建失败 ({from_uid}-[{rel_type}]->{to_uid}): {e}")

    except Exception as e:
        logger.exception("存储抽取结果到 Neo4j 失败")
        return {"success": False, "detail": str(e), "errors": errors}
    finally:
        if own_client:
            client.close()

    return {
        "success": True,
        "nodes_created": nodes_created,
        "nodes_merged": nodes_merged,
        "relations_created": relations_created,
        "errors": errors,
    }


# ── 知识空间初始化 V2（空间树 + 维度刻度）──


def init_knowledge_map_spaces(
    space_name: str,
    dimension_scales: dict[str, list[str]],
    neo4j_client: Neo4jClient = None,
) -> dict[str, Any]:
    """
    知识空间初始化 V3（笛卡尔积多子空间）：

    1. 验证维度存在
    2. 创建/复用刻度节点并连接维度
    3. 创建根 KnowledgeSpace 节点（spaceName = space_name）
    4. 按维度刻度的笛卡尔积生成多个子空间节点
    5. 每个子空间连接根空间（CONTAINS）和对应刻度（BELONGS_TO）

    参数：
        space_name: 知识空间（根空间）名称
        dimension_scales: {维度名: [刻度名列表]}，例如：
            {"维度1": ["刻度1", "刻度2"], "维度2": ["刻度3", "刻度4"]}
        neo4j_client: 可选 Neo4j 客户端

    示例：
        space_name = "我的知识空间"
        dimension_scales = {"方面维度": ["战略", "执行"], "时间维度": ["年月"]}

        生成子空间：
        - 方面维度.战略+时间维度.年月  (BELONGS_TO 战略, 年月)
        - 方面维度.执行+时间维度.年月  (BELONGS_TO 执行, 年月)

    返回：
        {"success": True, "space_name": "...", "root_workspace_id": "...",
         "sub_spaces": [{...}, ...], "sub_space_count": N, ...}
    """
    space_name = (space_name or "").strip()
    if not space_name:
        return {"success": False, "detail": "空间名称不能为空"}

    if not dimension_scales:
        return {"success": False, "detail": "维度-刻度映射不能为空"}

    client = neo4j_client or Neo4jClient()
    own_client = neo4j_client is None

    try:
        # ── 1. 验证所有维度存在，展平为 (dim, scale) 对列表 ──
        all_pairs: list[tuple[str, str]] = []
        dim_names_ordered: list[str] = []

        for dim_name, scale_list in dimension_scales.items():
            dim_name = (dim_name or "").strip()
            if not dim_name:
                return {"success": False, "detail": "维度名称不能为空"}

            dim_node = client.run_single(
                "MATCH (d:Dimension {name: $name}) RETURN d",
                {"name": dim_name},
            )
            if dim_node is None:
                return {"success": False, "detail": f"维度不存在: {dim_name}"}

            dim_names_ordered.append(dim_name)

            cleaned_scales: list[str] = []
            for scale_name in scale_list:
                scale_name = (scale_name or "").strip()
                if not scale_name:
                    continue
                cleaned_scales.append(scale_name)
                all_pairs.append((dim_name, scale_name))

            if not cleaned_scales:
                return {"success": False, "detail": f"维度 {dim_name} 至少需要一个刻度"}

            # 更新 dimension_scales 为清洗后的列表（用于笛卡尔积）
            dimension_scales[dim_name] = cleaned_scales

        if not all_pairs:
            return {"success": False, "detail": "至少需要一个维度-刻度对"}

        # ── 2. 处理刻度节点：检查/创建，并确保与维度相连 ──
        all_scale_names: set[str] = set()
        for dim_name, scale_name in all_pairs:
            all_scale_names.add(scale_name)

            # 检查刻度节点是否存在（按 name 查询）
            scale_node = client.run_single(
                "MATCH (s:Scale {name: $name}) RETURN s",
                {"name": scale_name},
            )
            if scale_node is None:
                logger.info("创建刻度节点: %s", scale_name)
                client.create_node("Scale", {"name": scale_name, "scale_name": scale_name})

            # 确保维度与刻度相连
            existing_rel = client.run_single(
                "MATCH (d:Dimension {name: $dimName})-[r:包含|BELONGS_TO]-(s:Scale {name: $scaleName}) RETURN r",
                {"dimName": dim_name, "scaleName": scale_name},
            )
            if existing_rel is None:
                logger.info("创建维度-刻度关系: %s -[:包含]-> %s", dim_name, scale_name)
                client.create_relationship(
                    "Dimension", {"name": dim_name},
                    "Scale", {"name": scale_name},
                    "包含",
                )

        # ── 3. 创建/复用根 KnowledgeSpace 节点 ──
        existing_root = client.run_single(
            "MATCH (ks:KnowledgeSpace {spaceName: $name}) "
            "WHERE NOT EXISTS { MATCH (parent:KnowledgeSpace)-[:CONTAINS]->(ks) } "
            "RETURN ks",
            {"name": space_name},
        )
        if existing_root is not None:
            root_workspace_id = existing_root["ks"]["workspace_id"]
            root_existed = True
            logger.info("根知识空间已存在，复用: %s (id=%s)", space_name, root_workspace_id)
        else:
            root_workspace_id = str(uuid4()).replace("-", "")[:16]
            client.create_node("KnowledgeSpace", {
                "workspace_id": root_workspace_id,
                "spaceName": space_name,
                "name": space_name,
            })
            root_existed = False
            logger.info("创建根知识空间: %s (id=%s)", space_name, root_workspace_id)

        # ── 4. 笛卡尔积生成多个子空间节点 ──
        scale_lists = [dimension_scales[d] for d in dim_names_ordered]
        combinations = list(itertools.product(*scale_lists))

        sub_spaces: list[dict[str, Any]] = []
        created_count = 0
        existed_count = 0

        for combo in combinations:
            # 构建子空间名称: 维度.刻度+维度.刻度+...
            parts = [f"{dim_names_ordered[i]}.{combo[i]}" for i in range(len(dim_names_ordered))]
            sub_ks_name = "+".join(parts)

            # 检查子空间是否已存在
            existing_sub = client.run_single(
                "MATCH (ks:KnowledgeSpace {name: $name}) RETURN ks",
                {"name": sub_ks_name},
            )
            if existing_sub is not None:
                sub_workspace_id = existing_sub["ks"].get("workspace_id", "")
                existed_count += 1
                logger.info("子空间已存在，跳过: %s", sub_ks_name)
                sub_spaces.append({"name": sub_ks_name, "workspace_id": sub_workspace_id, "existed": True})
                continue

            # 创建子空间节点
            sub_workspace_id = str(uuid4()).replace("-", "")[:16]
            client.create_node("KnowledgeSpace", {
                "workspace_id": sub_workspace_id,
                "spaceName": sub_ks_name,
                "name": sub_ks_name,
            })
            created_count += 1
            logger.info("创建子空间节点: %s (id=%s)", sub_ks_name, sub_workspace_id)

            # 子空间连接到根空间（CONTAINS）
            client.create_relationship(
                "KnowledgeSpace", {"workspace_id": root_workspace_id},
                "KnowledgeSpace", {"workspace_id": sub_workspace_id},
                "CONTAINS",
            )

            # 子空间连接到对应刻度（BELONGS_TO）
            for i, scale_name in enumerate(combo):
                client.create_relationship(
                    "KnowledgeSpace", {"workspace_id": sub_workspace_id},
                    "Scale", {"name": scale_name},
                    "BELONGS_TO",
                )

            sub_spaces.append({"name": sub_ks_name, "workspace_id": sub_workspace_id, "existed": False})

        # ── 5. 根空间连接到所有刻度（BELONGS_TO）──
        for scale_name in all_scale_names:
            existing_rel = client.run_single(
                "MATCH (ks:KnowledgeSpace {workspace_id: $ws_id})-[r:BELONGS_TO]-(s:Scale {name: $sc_name}) "
                "RETURN r",
                {"ws_id": root_workspace_id, "sc_name": scale_name},
            )
            if existing_rel is None:
                client.create_relationship(
                    "KnowledgeSpace", {"workspace_id": root_workspace_id},
                    "Scale", {"name": scale_name},
                    "BELONGS_TO",
                )

        return {
            "success": True,
            "space_name": space_name,
            "root_workspace_id": root_workspace_id,
            "root_existed": root_existed,
            "sub_spaces": sub_spaces,
            "sub_space_count": len(sub_spaces),
            "sub_space_created": created_count,
            "sub_space_existed": existed_count,
            "dimension_count": len(dim_names_ordered),
            "scale_count": len(all_scale_names),
            "dimensions": dim_names_ordered,
            "scales": sorted(all_scale_names),
            "message": f"知识空间 {space_name} 初始化完成，"
                       f"生成 {len(sub_spaces)} 个子空间（新建 {created_count}，已存在 {existed_count}）",
        }

    except Exception as e:
        logger.exception("初始化知识空间（笛卡尔积）失败")
        return {"success": False, "detail": str(e)}
    finally:
        if own_client:
            client.close()


def init_knowledge_space_tree(
    workspace_tree: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    neo4j_client: Neo4jClient = None,
) -> dict[str, Any]:
    """
    知识空间初始化（V2，树形结构）：

    1. 创建根 KnowledgeSpace 节点（spaceName = 根空间）
    2. 递归创建子空间节点，连接父子关系（CONTAINS）
    3. 创建 Dimension / Scale 节点（去重：Scale 按 scale_name 检查）
    4. 维度连接到根空间（BELONGS_TO），刻度连接到维度（包含）
    5. 创建聚合 KnowledgeSpace 节点，命名规则：维度.刻度+维度.刻度+...

    参数：
        workspace_tree: 空间树草稿，例如：
            [{"space_name":"根空间", "children": [{"space_name":"子空间一"}, ...]}]
        dimensions: 维度刻度列表，例如：
            [{"dimension_id":"dim-001", "dimension_name":"方面维度",
              "scales": [{"scale_id":"sc-001", "scale_name":"战略", "scale_desc":"..."}]}]
        neo4j_client: 可选 Neo4j 客户端

    返回：
        {"success": True, "root_workspace_id": "...", "composite_ks_name": "...", ...}
    """
    if not workspace_tree:
        return {"success": False, "detail": "workspace_tree 不能为空"}

    client = neo4j_client or Neo4jClient()
    own_client = neo4j_client is None

    try:
        # ── 1. 创建/复用空间树（同名根空间不重复创建）──
        root_item = workspace_tree[0]
        root_space_name = (root_item.get("space_name") or "").strip()
        if not root_space_name:
            return {"success": False, "detail": "根空间 space_name 不能为空"}

        # 去重：检查根空间是否已存在
        existing_root = client.run_single(
            "MATCH (ks:KnowledgeSpace {spaceName: $name}) "
            "WHERE NOT EXISTS { MATCH (parent:KnowledgeSpace)-[:CONTAINS]->(ks) } "
            "RETURN ks",
            {"name": root_space_name},
        )
        if existing_root is not None:
            root_workspace_id = existing_root["ks"]["workspace_id"]
            logger.info("根空间已存在，复用: %s (id=%s)", root_space_name, root_workspace_id)
        else:
            root_workspace_id = _create_space_tree(client, root_item, parent_id=None)
            logger.info("空间树创建完成，根空间 id=%s name=%s", root_workspace_id, root_space_name)

        # ── 2. 创建维度与刻度 ──
        all_scale_names: set[str] = set()
        dim_scale_pairs: list[tuple[str, str]] = []

        for dim in (dimensions or []):
            dim_id = (dim.get("dimension_id") or "").strip()
            dim_name = (dim.get("dimension_name") or "").strip()
            if not dim_id or not dim_name:
                continue

            # 检查/创建 Dimension 节点（按 dimension_id 去重）
            existing_dim = client.run_single(
                "MATCH (d:Dimension {dimension_id: $dim_id}) RETURN d",
                {"dim_id": dim_id},
            )
            if existing_dim is None:
                client.create_node("Dimension", {
                    "dimension_id": dim_id,
                    "name": dim_name,
                })
                logger.info("创建维度节点: %s (%s)", dim_name, dim_id)
            else:
                logger.info("维度已存在，跳过: %s (%s)", dim_name, dim_id)

            scales = dim.get("scales") or []
            for sc in scales:
                scale_id = (sc.get("scale_id") or "").strip()
                scale_name = (sc.get("scale_name") or "").strip()
                scale_desc = (sc.get("scale_desc") or "").strip()
                if not scale_id or not scale_name:
                    continue

                # 检查 Scale 是否已存在（按 scale_name 查 Scale 标签）
                existing_scale = client.run_single(
                    "MATCH (s:Scale {scale_name: $sc_name}) RETURN s",
                    {"sc_name": scale_name},
                )
                if existing_scale is None:
                    client.create_node("Scale", {
                        "scale_id": scale_id,
                        "scale_name": scale_name,
                        "name": scale_name,       # 兼容旧版按 name 查询
                        "scale_desc": scale_desc,
                    })
                    logger.info("创建刻度节点: %s (%s)", scale_name, scale_id)
                else:
                    logger.info("刻度已存在，跳过: %s (%s)", scale_name, scale_id)

                # 连接 Scale -> Dimension (包含)
                existing_rel = client.run_single(
                    "MATCH (d:Dimension {dimension_id: $dim_id})-[r:包含]-(s:Scale {scale_name: $sc_name}) "
                    "RETURN r",
                    {"dim_id": dim_id, "sc_name": scale_name},
                )
                if existing_rel is None:
                    client.create_relationship(
                        "Dimension", {"dimension_id": dim_id},
                        "Scale", {"scale_name": scale_name},
                        "包含",
                    )
                    logger.info("连接维度 -> 刻度: %s -[:包含]-> %s", dim_name, scale_name)

                all_scale_names.add(scale_name)
                dim_scale_pairs.append((dim_name, scale_name))

            # 连接 Dimension -> 根 KnowledgeSpace (BELONGS_TO)
            root_node = client.run_single(
                "MATCH (ks:KnowledgeSpace {workspace_id: $ws_id}) RETURN ks",
                {"ws_id": root_workspace_id},
            )
            if root_node:
                dim_belongs_rel = client.run_single(
                    "MATCH (d:Dimension {dimension_id: $dim_id})-[r:BELONGS_TO]-(ks:KnowledgeSpace {workspace_id: $ws_id}) "
                    "RETURN r",
                    {"dim_id": dim_id, "ws_id": root_workspace_id},
                )
                if dim_belongs_rel is None:
                    client.create_relationship(
                        "Dimension", {"dimension_id": dim_id},
                        "KnowledgeSpace", {"workspace_id": root_workspace_id},
                        "BELONGS_TO",
                    )
                    logger.info("连接维度 -> 根空间: %s -[:BELONGS_TO]-> %s", dim_name, root_space_name)

        # ── 3. 创建聚合 KnowledgeSpace 节点（维度.刻度+维度.刻度+...）──
        ks_name_parts = [f"{d}.{s}" for d, s in dim_scale_pairs]
        composite_ks_name = "+".join(ks_name_parts) if ks_name_parts else root_space_name

        existing_ks = client.run_single(
            "MATCH (ks:KnowledgeSpace {name: $name}) RETURN ks",
            {"name": composite_ks_name},
        )
        if existing_ks is None:
            client.create_node("KnowledgeSpace", {
                "name": composite_ks_name,
            })
            logger.info("创建聚合知识空间节点: %s", composite_ks_name)

            # 聚合空间连接到各刻度
            for sc_name in all_scale_names:
                client.create_relationship(
                    "KnowledgeSpace", {"name": composite_ks_name},
                    "Scale", {"scale_name": sc_name},
                    "BELONGS_TO",
                )
        else:
            logger.info("聚合知识空间已存在: %s", composite_ks_name)

        return {
            "success": True,
            "root_workspace_id": root_workspace_id,
            "composite_ks_name": composite_ks_name,
            "message": f"知识空间初始化完成，根空间 id: {root_workspace_id}",
            "space_count": _count_spaces(client),
            "dimension_count": len(dim_scale_pairs),
            "scale_count": len(all_scale_names),
        }

    except Exception as e:
        logger.exception("初始化知识空间树失败")
        return {"success": False, "detail": str(e)}
    finally:
        if own_client:
            client.close()


def _create_space_tree(
    client: Neo4jClient,
    item: dict[str, Any],
    parent_id: str | None,
) -> str:
    """
    递归创建空间树节点并建立 CONTAINS 关系。
    同名空间在同一个父节点下不重复创建，返回已有/新创建的 workspace_id。

    使用应用层 UUID 作为业务主键（而非 Neo4j 内部 id）的原因：
    - Neo4j 5.x 中 id() 已弃用，elementId() 虽可用但格式不透明
    - 内部 ID 在节点删除后可能回收，备份恢复后可能变化
    - 应用层 UUID 跨数据库可移植、不会碰撞
    """
    space_name = (item.get("space_name") or "未命名").strip()

    # 去重：检查同一父节点下是否已有同名子空间
    if parent_id:
        existing = client.run_single(
            "MATCH (parent:KnowledgeSpace {workspace_id: $pid})"
            "-[:CONTAINS]->(child:KnowledgeSpace {spaceName: $name}) "
            "RETURN child",
            {"pid": parent_id, "name": space_name},
        )
        if existing is not None:
            wid = existing["child"]["workspace_id"]
            logger.info("子空间已存在，复用: %s (id=%s)", space_name, wid)
            # 递归处理子节点（子空间已存在但可能新增了更深层的孩子）
            for child_item in (item.get("children") or []):
                _create_space_tree(client, child_item, parent_id=wid)
            return wid

    workspace_id = str(uuid4()).replace("-", "")[:16]

    props = {
        "workspace_id": workspace_id,
        "spaceName": space_name,
    }
    client.create_node("KnowledgeSpace", props)
    logger.info("创建空间节点: %s (id=%s)", space_name, workspace_id)

    # 连接父节点
    if parent_id:
        client.create_relationship(
            "KnowledgeSpace", {"workspace_id": parent_id},
            "KnowledgeSpace", {"workspace_id": workspace_id},
            "CONTAINS",
        )

    # 递归创建子空间
    children = item.get("children") or []
    for child in children:
        _create_space_tree(client, child, parent_id=workspace_id)

    return workspace_id


def _count_spaces(client: Neo4jClient) -> int:
    """统计知识空间节点总数（包括聚合节点）。"""
    records = client.run("MATCH (n:KnowledgeSpace) RETURN count(n) AS cnt")
    if records:
        return records[0].get("cnt", 0)
    return 0
