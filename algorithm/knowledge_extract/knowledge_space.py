"""知识空间初始化、刻度查询与抽取结果入库逻辑。"""
import logging
from typing import Any

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
