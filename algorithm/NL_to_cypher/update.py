# -*- coding: utf-8 -*-
"""
知识更新服务 - Neo4j + Milvus 同步脚本
向量化使用 transformers 直接加载 bge-small-zh-v1.5，不依赖 sentence-transformers
"""
from qasystem_config.configuration import getenv as config_getenv
import json
import os
import uuid
import sys
import requests
import urllib.request
from pathlib import Path
from functools import lru_cache
from threading import RLock

from neo4j import GraphDatabase
from pymilvus import (
    connections, Collection, CollectionSchema, FieldSchema,
    DataType, utility
)

# ============ 路径修复 ============
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
)

DEFAULT_MODEL_PATH = "/home/swang/project/algorithm/NL_to_cypher/models/bge-small-zh-v1.5"
ROOT_SPACE_ID = "eea62662-b2cb-4c9b-9a75-548d1ac08d35"
CLEF_URL = "http://172.24.0.6:8188/v1/systemone"
PROXIES = {"http": None, "https": None}

def _binding_device() -> str | None:
    """Return 'cuda' only when this process is explicitly bound to specific GPUs."""
    visible = config_getenv("CUDA_VISIBLE_DEVICES", "").strip()
    if visible and visible.lower() not in {"", "none", "no", "-1", "all"}:
        return "cuda"
    return None


@lru_cache(maxsize=1)
def get_sentence_transformer():
    """用 transformers 直接加载 bge 模型，完全不经过 sentence-transformers。"""
    model_path = DEFAULT_MODEL_PATH

    if not os.path.isdir(model_path):
        raise FileNotFoundError(
            f"模型目录不存在: {model_path}\n"
            f"请确认 bge-small-zh-v1.5 模型已下载到该路径"
        )

    config_file = os.path.join(model_path, "config.json")
    if not os.path.isfile(config_file):
        raise FileNotFoundError(
            f"模型目录 {model_path} 缺少 config.json，不是一个有效的模型目录"
        )

    from transformers import AutoModel, AutoTokenizer
    import torch

    device = _binding_device() or "cpu"
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModel.from_pretrained(model_path, local_files_only=True).to(device)

    class _BGEModel:
        def encode(self, texts, convert_to_numpy=True, **kwargs):
            if isinstance(texts, str):
                texts = [texts]

            encoded = tokenizer(
                texts, padding=True, truncation=True,
                max_length=512, return_tensors="pt"
            ).to(device)

            with torch.no_grad():
                outputs = model(**encoded)
                # bge 用 CLS token embedding
                embeddings = outputs.last_hidden_state[:, 0, :]
                # L2 归一化（bge 模型要求）
                embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)

            if convert_to_numpy:
                return embeddings.cpu().numpy()
            return embeddings

    return _BGEModel()


@lru_cache(maxsize=1)
def get_encode_lock() -> RLock:
    return RLock()


def encode(values):
    """对文本或文本列表进行向量化，使用默认本地模型路径。"""
    model = get_sentence_transformer()
    with get_encode_lock():
        return model.encode(values, convert_to_numpy=True)


def text_to_vector(text: str) -> list[float]:
    """Encode one text."""
    value = (text or "").strip()
    if not value:
        raise ValueError("文本不能为空")
    vector = encode(value)
    return vector.tolist()


def batch_text_to_vectors(texts: list[str]) -> list[list[float]]:
    """Encode multiple texts with one batched call."""
    values = [str(text) for text in texts]
    if not values:
        raise ValueError("文本列表不能为空")
    vectors = encode(values)
    return vectors.tolist()



# ============ Neo4j 连接配置 ============
NEO4J_URI = "bolt://10.126.62.88:17687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "neo4j2025"

# ============ Milvus 连接配置 ============
MILVUS_HOST = "10.126.62.88"
MILVUS_PORT = "19530"
MILVUS_ALIAS = "default"
MILVUS_COLLECTION = "knowledge_nodes"
# =======================================


# ====================================================================
#  工具函数
# ====================================================================

def clear_neo4j(uri: str, user: str, password: str) -> dict:
    """清空 Neo4j 中所有关系和节点。"""
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            rel_cnt = session.run("MATCH ()-[r]->() DELETE r RETURN count(r) AS cnt").single()["cnt"]
            node_cnt = session.run("MATCH (n) DELETE n RETURN count(n) AS cnt").single()["cnt"]
        return {"nodes_deleted": node_cnt, "relationships_deleted": rel_cnt}
    finally:
        driver.close()


def clear_milvus_collection(host: str, port: str, alias: str, collection_name: str) -> None:
    """清空指定 Milvus 集合（直接 drop 删除）。"""
    connections.connect(alias=alias, host=host, port=port)
    try:
        if utility.has_collection(collection_name):
            utility.drop_collection(collection_name)
    finally:
        connections.disconnect(alias)


def import_official_data_to_neo4j(
    uri: str,
    user: str,
    password: str,
    base_dir: str = "/home/swang/project/test_data/update/official",
) -> dict:
    """
    从 base_dir 下的 official_01 ~ official_05 目录中读取
    extract/golden.json，将数据导入 Neo4j。
    """
    driver = GraphDatabase.driver(uri, auth=(user, password))
    official_dirs = [f"official_{i:02d}" for i in range(1, 6)]

    ROOT_SPACE_ID = "eea62662-b2cb-4c9b-9a75-548d1ac08d35"

    stats = {"nodes_imported": 0, "rels_imported": 0, "errors": []}

    try:
        with driver.session() as session:
            for dir_name in official_dirs:
                json_path = os.path.join(base_dir, dir_name, "extract", "golden.json")
                if not os.path.exists(json_path):
                    msg = f"{json_path} 不存在，跳过"
                    print(f"  [WARN] {msg}")
                    stats["errors"].append(msg)
                    continue

                print(f"  处理 {json_path} ...")
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # ---- 导入节点 ----
                for entity in data.get("entities", []):
                    entity_type = entity["type"]
                    entity_name = entity["name"]
                    props = entity.get("properties", {})

                    all_props = {"name": entity_name}
                    for k, v in props.items():
                        if k not in all_props:
                            all_props[k] = v

                    all_props["id"] = str(uuid.uuid4())
                    all_props["root_space_id"] = ROOT_SPACE_ID

                    set_clauses = ", ".join([f"n.`{k}` = ${k}" for k in all_props.keys()])
                    query = (
                        f"CREATE (n:`{entity_type}` {{name: $name}}) "
                        f"SET {set_clauses}"
                    )

                    try:
                        session.run(query, **all_props)
                        stats["nodes_imported"] += 1
                    except Exception as e:
                        err = f"节点导入失败 [{entity_type}: {entity_name}]: {e}"
                        print(f"  [ERR] {err}")
                        stats["errors"].append(err)

                # ---- 导入关系 ----
                for rel in data.get("relations", []):
                    from_type = rel["from_type"]
                    from_name = rel["from_name"]
                    rel_type = rel["rel_type"]
                    to_type = rel["to_type"]
                    to_name = rel["to_name"]

                    query = (
                        f"MATCH (a:`{from_type}` {{name: $from_name}}) "
                        f"MATCH (b:`{to_type}` {{name: $to_name}}) "
                        f"CREATE (a)-[r:`{rel_type}`]->(b)"
                    )

                    try:
                        session.run(query, from_name=from_name, to_name=to_name)
                        stats["rels_imported"] += 1
                    except Exception as e:
                        err = (
                            f"关系导入失败 "
                            f"[{from_type}:{from_name}] -[{rel_type}]-> "
                            f"[{to_type}:{to_name}]: {e}"
                        )
                        print(f"  [ERR] {err}")
                        stats["errors"].append(err)

        print(
            f"[OK] 导入完成: "
            f"{stats['nodes_imported']} 节点, "
            f"{stats['rels_imported']} 关系"
        )
        if stats["errors"]:
            print(f"  {len(stats['errors'])} 个错误，详见返回列表")

    finally:
        driver.close()

    return stats


def test_neo4j(uri: str, user: str, password: str) -> bool:
    """测试 Neo4j 连接并读取知识图谱统计信息"""
    driver = None
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()
        print("[OK] Neo4j 连接成功，服务端可正常握手")

        with driver.session() as session:
            node_count = session.run("MATCH (n) RETURN count(n) AS cnt").single()["cnt"]
            print(f"\n{'='*50}")
            print(f"  知识图谱节点总数: {node_count}")

            rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").single()["cnt"]
            print(f"  知识图谱关系总数: {rel_count}")

            label_result = session.run(
                "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt "
                "ORDER BY cnt DESC"
            )
            label_stats = [(r["label"], r["cnt"]) for r in label_result]
            if label_stats:
                print(f"\n  {'标签':<25s} {'数量':>8s}")
                print(f"  {'-'*25} {'-'*8}")
                for label, cnt in label_stats:
                    label_name = label if label else "(无标签)"
                    print(f"  {label_name:<25s} {cnt:>8d}")

            limit = min(10, node_count)
            print(f"\n{'='*50}")
            print(f"  返回前 {limit} 个节点详情:")
            print(f"{'='*50}")

            if node_count > 0:
                nodes_result = session.run("MATCH (n) RETURN n LIMIT 10")
                for i, record in enumerate(nodes_result, 1):
                    node = record["n"]
                    print(f"\n  --- 节点 {i} ---")
                    print(f"  标签: {list(node.labels)}")
                    props = dict(node)
                    for k, v in props.items():
                        v_str = str(v)
                        if len(v_str) > 80:
                            v_str = v_str[:77] + "..."
                        print(f"  {k}: {v_str}")
            else:
                print("\n  (知识图谱为空，无节点可返回)")

        return True
    except Exception as e:
        print(f"[FAIL] Neo4j 连接失败: {type(e).__name__}: {e}")
        return False
    finally:
        if driver:
            driver.close()


def test_milvus(host: str, port: str, alias: str, collection_name: str) -> bool:
    """测试 Milvus 连接，修正了 load 和 dim 显示问题。"""
    connected = False
    try:
        connections.connect(alias=alias, host=host, port=port)
        connected = True
        print("[OK] Milvus 连接成功")

        if not utility.has_collection(collection_name):
            print(f"[WARN] Collection '{collection_name}' 不存在")
            return False

        coll = Collection(collection_name)
        coll.flush()

        approx = coll.num_entities

        # ---- 修正 1：query 前必须 load ----
        try:
            coll.load()
            exact = coll.query(expr="", output_fields=["count(*)"])[0]["count(*)"]
            print(f"  Collection '{collection_name}' 精确数据条数: {exact}")
        except Exception as qe:
            print(f"  [INFO] 精确统计跳过: {qe}")
        finally:
            coll.release()  # 用完释放内存

        # ---- 修正 2：正确取 vector 维度 ----
        vector_field = next((f for f in coll.schema.fields if f.dtype == DataType.FLOAT_VECTOR), None)
        if vector_field:
            print(f"  维度 (dim): {vector_field.params.get('dim')}")
        else:
            print("  维度 (dim): 未找到向量字段")

        print(f"  索引: {[idx.index_name for idx in coll.indexes]}")

        return True
    except Exception as e:
        print(f"[FAIL] Milvus: {type(e).__name__}: {e}")
        return False
    finally:
        if connected:
            connections.disconnect(alias)


def _fetch_node_from_neo4j(
    uri: str, user: str, password: str,
    node_id: str, label: str
) -> dict | None:
    """根据 node_id 和 label 从 Neo4j 查节点详情。"""
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (n:`LabelPlaceholder`) WHERE n.id = $nid "
                "RETURN n.name AS name, properties(n) AS props "
                "LIMIT 1",
                nid=node_id
            )
            # 注意：Neo4j label 不能直接参数化，用 f-string 拼（label 来自我们自己的数据，不是用户输入）
            query = (
                f"MATCH (n:`{label}`) WHERE n.id = $nid "
                f"RETURN n.name AS name, properties(n) AS props LIMIT 1"
            )
            result = session.run(query, nid=node_id)
            record = result.single()
            if record:
                return {
                    "name": record["name"],
                    "properties": dict(record["props"]),
                }
            return None
    except Exception as e:
        print(f"  [WARN] Neo4j 查询失败 (node_id={node_id}): {e}")
        return None

# ============ Milvus 集合 schema 定义 ============

def _ensure_milvus_collection(
    host: str, port: str, alias: str, collection_name: str
) -> Collection:
    """确保 Milvus 集合存在，不存在则按 schema 创建。"""
    connections.connect(alias=alias, host=host, port=port)

    if utility.has_collection(collection_name):
        return Collection(collection_name)

    fields = [
        FieldSchema(
            name="pk",
            dtype=DataType.INT64,
            is_primary=True,
            auto_id=True,
            description="主键，自动生成"
        ),
        FieldSchema(
            name="vector",
            dtype=DataType.FLOAT_VECTOR,
            dim=512,
            description="文本嵌入向量（bge-small-zh-v1.5）"
        ),
        FieldSchema(
            name="label",
            dtype=DataType.VARCHAR,
            max_length=256,
            description="节点 label"
        ),
        FieldSchema(
            name="node_id",
            dtype=DataType.VARCHAR,
            max_length=256,
            description="Neo4j 节点 id 属性"
        ),
        FieldSchema(
            name="root_space_id",
            dtype=DataType.VARCHAR,
            max_length=256,
            description="空间标识"
        ),
        FieldSchema(
            name="content",
            dtype=DataType.VARCHAR,
            max_length=8192,
            description="拼合后的原文文本"
        ),
    ]

    schema = CollectionSchema(
        fields=fields,
        description="知识图谱节点向量索引，按 label 过滤后做相似度匹配"
    )

    coll = Collection(collection_name, schema)

    coll.create_index(
        field_name="vector",
        index_params={
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 256}
        }
    )

    coll.create_index(
        field_name="label",
        index_params={"index_type": "INVERTED"}
    )

    print(f"[OK] 集合 '{collection_name}' 已创建并建好索引")
    return coll


# ============ Neo4j → Milvus 同步函数 ============

def sync_neo4j_to_milvus(
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    milvus_host: str,
    milvus_port: str,
    milvus_alias: str,
    milvus_collection: str,
    root_space_id: str | None = None,
    batch_size: int = 64,
) -> dict:
    """
    从 Neo4j 拉取带 root_space_id 的节点 → 拼合文本 → 向量化 → 写入 Milvus。
    """
    # ---- 1. 从 Neo4j 拉取节点 ----
    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    nodes_data = []

    try:
        with driver.session() as session:
            if root_space_id:
                result = session.run(
                    "MATCH (n) WHERE n.root_space_id = $sid "
                    "RETURN labels(n)[0] AS label, n.id AS node_id, "
                    "n.root_space_id AS rsid, properties(n) AS props",
                    sid=root_space_id
                )
            else:
                result = session.run(
                    "MATCH (n) WHERE n.root_space_id IS NOT NULL "
                    "RETURN labels(n)[0] AS label, n.id AS node_id, "
                    "n.root_space_id AS rsid, properties(n) AS props"
                )

            for record in result:
                label = record["label"]
                node_id = record["node_id"]
                rsid = record["rsid"]
                props = dict(record["props"])

                if not node_id:
                    continue

                # 拼合文本（id / root_space_id 不进语义）
                parts = [f"类型: {label}"]
                if props.get("name"):
                    parts.append(f"名称: {props['name']}")
                for k, v in sorted(props.items()):
                    if k in ("name", "id", "root_space_id"):
                        continue
                    v_str = str(v).strip()
                    if v_str:
                        parts.append(f"{k}: {v_str}")

                content = "\n".join(parts)
                nodes_data.append({
                    "label": label,
                    "node_id": str(node_id),
                    "root_space_id": str(rsid),
                    "content": content,
                })

    finally:
        driver.close()

    if not nodes_data:
        print("[WARN] Neo4j 中未找到带 root_space_id 的节点")
        return {"nodes_synced": 0, "errors": []}

    print(f"  从 Neo4j 拉取 {len(nodes_data)} 个节点，开始向量化...")

    # ---- 2. 批量向量化 ----
    texts = [nd["content"] for nd in nodes_data]
    all_vectors = []

    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i:i + batch_size]
        try:
            batch_vectors = batch_text_to_vectors(batch_texts)
            all_vectors.extend(batch_vectors)
        except Exception as e:
            print(f"  [ERR] 批次 {i//batch_size} 向量化失败: {e}")
            all_vectors.extend([[0.0] * 512] * len(batch_texts))

    # ---- 3. 写入 Milvus ----
    connections.connect(alias=milvus_alias, host=milvus_host, port=milvus_port)
    try:
        coll = _ensure_milvus_collection(
            milvus_host, milvus_port, milvus_alias, milvus_collection
        )

        entities = [
            all_vectors,
            [nd["label"] for nd in nodes_data],
            [nd["node_id"] for nd in nodes_data],
            [nd["root_space_id"] for nd in nodes_data],
            [nd["content"] for nd in nodes_data],
        ]

        coll.insert(entities)
        coll.flush()

        print(f"[OK] 写入 Milvus 完成: {len(nodes_data)} 条")

    finally:
        connections.disconnect(milvus_alias)

    return {"nodes_synced": len(nodes_data), "errors": []}
# ============ 新节点相似匹配 ============

def match_new_nodes(
    new_entities: list[dict],
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    milvus_host: str,
    milvus_port: str,
    milvus_alias: str,
    milvus_collection: str,
    root_space_id: str,
    similarity_threshold: float = 0.85,
    top_k: int = 5,
    batch_size: int = 64,
) -> list[dict]:
    """
    将新抽取的节点与知识图谱中已有节点做相似度匹配。

    Args:
        new_entities: 新节点列表，格式 [{"type": "Label", "name": "xxx", "properties": {...}}, ...]
        similarity_threshold: 余弦相似度阈值，默认 0.85
        top_k: 每个新节点最多返回几个候选

    Returns:
        JSON-serializable list of candidate pairs
    """
    if not new_entities:
        return []

    # ---- 1. 拼文本 + 向量化 ----
    texts = []
    valid_entities = []

    for entity in new_entities:
        entity_type = entity.get("type", "")
        props = entity.get("properties", {})

        parts = [f"类型: {entity_type}"]
        if entity.get("name"):
            parts.append(f"名称: {entity['name']}")
        for k, v in sorted(props.items()):
            if k in ("name", "id", "root_space_id"):
                continue
            v_str = str(v).strip()
            if v_str:
                parts.append(f"{k}: {v_str}")

        content = "\n".join(parts)
        texts.append(content)
        valid_entities.append({
            "label": entity_type,
            "name": entity.get("name", ""),
            "properties": props,
            "content": content,
        })

    print(f"  新节点 {len(texts)} 个，开始向量化...")

    all_vectors = []
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i:i + batch_size]
        try:
            batch_vectors = batch_text_to_vectors(batch_texts)
            all_vectors.extend(batch_vectors)
        except Exception as e:
            print(f"  [ERR] 批次 {i//batch_size} 向量化失败: {e}")
            all_vectors.extend([[0.0] * 512] * len(batch_texts))

    # ---- 2. 连接 Milvus 搜索 ----
    connections.connect(alias=milvus_alias, host=milvus_host, port=milvus_port)
    coll = Collection(milvus_collection)
    coll.load()

    candidate_pairs = []

    try:
        for idx, (entity, vector) in enumerate(zip(valid_entities, all_vectors)):
            label = entity["label"]

            # Milvus expr: 双条件过滤（注意字符串用双引号，&& 连接）
            expr = f'label == "{label}" && root_space_id == "{root_space_id}"'

            try:
                search_params = {
                    "metric_type": "COSINE",
                    "params": {"ef": 64},
                }

                results = coll.search(
                    data=[vector],
                    anns_field="vector",
                    param=search_params,
                    limit=top_k,
                    expr=expr,
                    output_fields=["node_id", "label", "content", "root_space_id"],
                )

                for hit in results[0]:
                    distance = hit.distance  # COSINE: 1.0 = 完全匹配

                    if distance < similarity_threshold:
                        continue

                    matched_node_id = hit.entity.get("node_id")

                    # ---- 3. 去 Neo4j 查原节点详情 ----
                    original_node = _fetch_node_from_neo4j(
                        neo4j_uri, neo4j_user, neo4j_password,
                        matched_node_id, label
                    )

                    pair = {
                        "source_node": {
                            "label": entity["label"],
                            "name": entity["name"],
                            "properties": entity["properties"],
                        },
                        "matched_node": {
                            "node_id": matched_node_id,
                            "label": hit.entity.get("label"),
                            "name": original_node.get("name", "") if original_node else "",
                            "properties": original_node.get("properties", {}) if original_node else {},
                            "content_preview": (hit.entity.get("content", "")[:200] if hit.entity.get("content") else ""),
                        },
                        "similarity": round(distance, 4),
                    }

                    candidate_pairs.append(pair)

            except Exception as e:
                print(f"  [ERR] 搜索失败 (节点 {idx}, label={label}): {e}")

    finally:
        coll.release()
        connections.disconnect(milvus_alias)

    print(f"[OK] 匹配完成: {len(candidate_pairs)} 个候选对 (阈值={similarity_threshold})")
    return candidate_pairs

def load_candidates(filepath):
    data = json.loads(Path(filepath).read_text(encoding="utf-8"))
    return data["candidates"]


def node_full_text(node):
    """完整输出节点信息"""
    lines = []
    lines.append(f"类型: {node.get('label', '')}")
    lines.append(f"名称: {node.get('name', '')}")
    props = node.get("properties", {})
    for k, v in props.items():
        if v is not None and v != "":
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def evaluate_match(source_node, matched_node, similarity):
    """用 noul 评估单个候选匹配的合理性"""
    source_text = node_full_text(source_node)
    matched_text = node_full_text(matched_node)

    state = (
        f"知识图谱更新任务：判断新抽取的节点是否应该与图谱中已有节点合并或关联。\n\n"
        f"【新节点】\n{source_text}\n\n"
        f"【图谱已有节点】\n{matched_text}\n\n"
        f"向量相似度: {similarity:.4f}"
    )

    resp = requests.post(
        CLEF_URL,
        json={
            "state": state,
            "questions": {
                "should_merge": {
                    "type": "noul",
                    "instructions": "新节点与图谱已有节点是否为同一实体/概念？如果应该合并（同一事物）则给高分，如果只是相关但不应合并则给低分。",
                },
                "match_quality": {
                    "type": "score",
                    "criteria": [
                        "完全不匹配（不同实体）",
                        "部分相关但不应合并",
                        "高度相似应合并",
                        "确定是同一实体",
                    ],
                },
            }
        },
        headers={"content-type": "application/json"},
        proxies=PROXIES,
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()

    answers = result["answers"]
    return {
        "should_merge_prob": answers["should_merge"]["noul"],
        "match_quality_score": answers["match_quality"]["score"],
        "match_quality_confidence": answers["match_quality"]["confidence"],
        "match_quality_legend": answers["match_quality"].get("legend", {}),
    }

class KnowledgeUpdater:
    """封装所有对 Neo4j 和 Milvus 的更新操作，保证 id 唯一性和向量同步。"""
    SKIP_PROPS = {"id", "root_space_id"}

    def __init__(self, neo4j_uri, neo4j_user, neo4j_password,
                 milvus_host, milvus_port, milvus_alias, milvus_collection,
                 root_space_id):
        self.neo4j_uri = neo4j_uri
        self.neo4j_auth = (neo4j_user, neo4j_password)
        self.milvus_host = milvus_host
        self.milvus_port = milvus_port
        self.milvus_alias = milvus_alias
        self.milvus_collection = milvus_collection
        self.root_space_id = root_space_id

    def _neo_driver(self):
        return GraphDatabase.driver(self.neo4j_uri, auth=self.neo4j_auth)

    def _node_content(self, label, name, props):
        """拼合文本，与 sync 逻辑一致"""
        parts = [f"类型: {label}"]
        if name:
            parts.append(f"名称: {name}")
        for k, v in sorted(props.items()):
            if k in self.SKIP_PROPS:
                continue
            s = str(v).strip()
            if s:
                parts.append(f"{k}: {s}")
        return "\n".join(parts)

    # ---------- 基础查询 ----------
    def get_node(self, node_id):
        with self._neo_driver() as d:
            with d.session() as s:
                rec = s.run("MATCH (n) WHERE n.id=$nid RETURN labels(n)[0] AS label, n.name AS name, properties(n) AS props LIMIT 1", nid=node_id).single()
                if not rec:
                    return None
                return {"label": rec["label"], "name": rec["name"], "properties": dict(rec["props"])}

    def find_node_by_name_label(self, label, name):
        with self._neo_driver() as d:
            with d.session() as s:
                rec = s.run(f"MATCH (n:`{label}`) WHERE n.name=$name RETURN n.id AS id LIMIT 1", name=name).single()
                return rec["id"] if rec else None

    # ---------- 向量库更新（删旧插新） ----------
    def _milvus_delete_by_node_id(self, node_id):
        connections.connect(alias=self.milvus_alias, host=self.milvus_host, port=self.milvus_port)
        coll = Collection(self.milvus_collection)
        expr = f'node_id == "{node_id}" && root_space_id == "{self.root_space_id}"'
        try:
            res = coll.query(expr=expr, output_fields=["pk"])
            if res:
                coll.delete(expr=f"pk in [{','.join(str(r['pk']) for r in res)}]")
        except Exception:
            pass

    def upsert_vector(self, node_id, label, content, vector):
        connections.connect(alias=self.milvus_alias, host=self.milvus_host, port=self.milvus_port)
        coll = _ensure_milvus_collection(self.milvus_host, self.milvus_port, self.milvus_alias, self.milvus_collection)
        self._milvus_delete_by_node_id(node_id)
        coll.insert([[vector], [label], [node_id], [self.root_space_id], [content]])
        coll.flush()

    def recompute_vector(self, node_id):
        node = self.get_node(node_id)
        if not node:
            return
        content = self._node_content(node["label"], node["name"], node["properties"])
        vec = text_to_vector(content)
        self.upsert_vector(node_id, node["label"], content, vec)

    # ---------- 关系去重建立 ----------
    def ensure_relation(self, from_id, to_id, rel_type, rel_props=None):
        rel_props = rel_props or {}
        with self._neo_driver() as d:
            with d.session() as s:
                exists = s.run(
                    f"MATCH (a)-[r:`{rel_type}`]->(b) WHERE a.id=$fid AND b.id=$tid RETURN count(r) AS cnt",
                    fid=from_id, tid=to_id
                ).single()["cnt"]
                if exists:
                    return False
                set_clause = ", ".join([f"r.`{k}`=$v_{k}" for k in rel_props]) if rel_props else ""
                query = f"MATCH (a),(b) WHERE a.id=$fid AND b.id=$tid CREATE (a)-[r:`{rel_type}`]->(b)"
                if set_clause:
                    query += f" SET {set_clause}"
                params = {f"v_{k}": v for k, v in rel_props.items()}
                params["fid"], params["tid"] = from_id, to_id
                s.run(query, **params)
                return True

    # ---------- 核心操作分支 ----------
    def CREATE(self, entity: dict, relations: list = None) -> str:
        """新建节点，写 Neo4j + Milvus，返回新 node_id"""
        label = entity["type"]
        name = entity.get("name", "")
        props = entity.get("properties", {})
        node_id = str(uuid.uuid4())
        all_props = {"id": node_id, "name": name, "root_space_id": self.root_space_id}
        for k, v in props.items():
            if k not in all_props:
                all_props[k] = v
        set_clause = ", ".join([f"n.`{k}`=$p_{k}" for k in all_props])
        with self._neo_driver() as d:
            with d.session() as s:
                s.run(f"CREATE (n:`{label}` {{id:$p_id, name:$p_name, root_space_id:$p_root}}) SET {set_clause}",
                      p_id=node_id, p_name=name, p_root=self.root_space_id, **{f"p_{k}": v for k, v in all_props.items()})
        # 向量
        content = self._node_content(label, name, props)
        vec = text_to_vector(content)
        self.upsert_vector(node_id, label, content, vec)
        # 关系
        for rel in (relations or []):
            to_id = rel.get("to_id") or self.find_node_by_name_label(rel["to_type"], rel["to_name"]) or self.CREATE({"type": rel["to_type"], "name": rel["to_name"], "properties": rel.get("to_properties", {})})
            self.ensure_relation(node_id, to_id, rel.get("rel_type", "相关"), rel.get("rel_props", {}))
        return node_id

    def MERGE(self, old_node_id: str, new_entity: dict, merged_properties: dict = None, relations: list = None) -> str:
        """合并：属性合并（默认保留旧节点），关系合并，重算向量"""
        old = self.get_node(old_node_id)
        if not old:
            raise ValueError(f"MERGE 失败：旧节点 {old_node_id} 不存在")
        # 属性合并逻辑
        if merged_properties is not None:
            final_props = merged_properties
        else:
            old_props_clean = {k: v for k, v in old["properties"].items() if k not in self.SKIP_PROPS}
            new_props = {k: v for k, v in new_entity.get("properties", {}).items() if k not in self.SKIP_PROPS}
            final_props = {**new_props, **old_props_clean}  # 旧值优先
        # 写回 Neo4j
        set_items = [f"n.`{k}`=$v_{k}" for k in final_props]
        with self._neo_driver() as d:
            with d.session() as s:
                s.run(f"MATCH (n) WHERE n.id=$nid SET {', '.join(set_items)}", nid=old_node_id, **{f"v_{k}": v for k, v in final_props.items()})
        # 关系合并
        for rel in (relations or []):
            to_id = rel.get("to_id") or self.find_node_by_name_label(rel["to_type"], rel["to_name"])
            if to_id:
                self.ensure_relation(old_node_id, to_id, rel.get("rel_type", "相关"), rel.get("rel_props", {}))
        # 重算向量
        self.recompute_vector(old_node_id)
        return old_node_id

    def UPDATE(self, old_node_id: str, new_entity: dict, relations: list = None) -> str:
        """覆盖：用新节点属性覆盖（保留 id/root_space_id），关系保留并追加"""
        old = self.get_node(old_node_id)
        if not old:
            raise ValueError(f"UPDATE 失败：旧节点 {old_node_id} 不存在")
        new_props = {k: v for k, v in new_entity.get("properties", {}).items() if k not in self.SKIP_PROPS}
        set_items = [f"n.`{k}`=$v_{k}" for k in new_props]
        with self._neo_driver() as d:
            with d.session() as s:
                s.run(f"MATCH (n) WHERE n.id=$nid SET {', '.join(set_items)}", nid=old_node_id, **{f"v_{k}": v for k, v in new_props.items()})
        for rel in (relations or []):
            to_id = rel.get("to_id") or self.find_node_by_name_label(rel["to_type"], rel["to_name"])
            if to_id:
                self.ensure_relation(old_node_id, to_id, rel.get("rel_type", "相关"), rel.get("rel_props", {}))
        self.recompute_vector(old_node_id)
        return old_node_id

    def LINK(self, from_entity: dict, to_entity: dict, rel_type: str = "相关", rel_props: dict = None,
             from_node_id: str = None, to_node_id: str = None) -> tuple:
        """都保留并连接指定关系，本质是特殊的 CREATE 或 纯连边"""
        if from_node_id:
            f_id = from_node_id
        else:
            f_id = self.find_node_by_name_label(from_entity["type"], from_entity["name"])
            if not f_id:
                f_id = self.CREATE(from_entity)
        if to_node_id:
            t_id = to_node_id
        else:
            t_id = self.find_node_by_name_label(to_entity["type"], to_entity["name"])
            if not t_id:
                t_id = self.CREATE(to_entity)
        self.ensure_relation(f_id, t_id, rel_type, rel_props or {})
        return f_id, t_id


# ====================================================================
#  决策与 Pipeline
# ====================================================================

def decide_operations(new_entities: list, candidate_pairs: list, evaluations: list = None,
                      merge_threshold: float = 0.8, update_threshold: float = 0.95) -> list:
    """
    将 match_new_nodes 的候选对和评估结果转为具体操作决策。
    返回 decisions 列表，每个元素包含 new_entity, operation, old_node_id, merged_properties。
    """
    decisions = []
    evaluated_map = {}
    if evaluations:
        for ev in evaluations:
            key = (ev.get("source_name"), ev.get("matched_name"))
            evaluated_map[key] = ev

    for ent in new_entities:
        ent_name = ent.get("name", "")
        ent_type = ent.get("type", "")
        # 找对应候选
        best_candidate = None
        best_eval = None
        for cand in candidate_pairs:
            src = cand.get("source_node", {})
            if src.get("name") == ent_name and src.get("label") == ent_type:
                best_candidate = cand
                key = (src.get("name"), cand.get("matched_node", {}).get("name"))
                best_eval = evaluated_map.get(key)
                break

        decision = {
            "new_entity": ent,
            "operation": "CREATE",
            "old_node_id": None,
            "merged_properties": None,
            "relations": ent.get("relations", [])  # 从 golden.json 或 update.json 里带的关系
        }

        if best_candidate and best_eval:
            sim = best_candidate.get("similarity", 0)
            merge_prob = best_eval.get("should_merge_prob", 0)
            matched_node = best_candidate["matched_node"]
            old_id = matched_node.get("node_id")

            if merge_prob >= merge_threshold and sim >= update_threshold:
                decision["operation"] = "UPDATE"
                decision["old_node_id"] = old_id
            elif merge_prob >= merge_threshold:
                decision["operation"] = "MERGE"
                decision["old_node_id"] = old_id
                # 简单规则合并属性（旧优先）
                old_props = matched_node.get("properties", {})
                new_props = ent.get("properties", {})
                merged = {**new_props, **old_props}
                decision["merged_properties"] = merged
            elif sim >= 0.7:  # 只是相关，不合并
                decision["operation"] = "LINK"
                decision["old_node_id"] = old_id  # 作为 to_node 连接
                decision["link_rel_type"] = "相关"

        decisions.append(decision)
    return decisions


def run_update_pipeline(update_json_path: str, candidates_json_path: str = None,
                        root_space_id: str = ROOT_SPACE_ID):
    """
    完整流水线：
    1. 加载新实体
    2. 匹配（若未提供 candidates 则现场跑）
    3. 评估（若未提供评估结果则现场跑）
    4. 决策
    5. 执行更新
    """
    print("\n" + "="*60)
    print("  启动知识更新 Pipeline")
    print("="*60)

    # 1. 加载新节点
    with open(update_json_path, "r", encoding="utf-8") as f:
        update_data = json.load(f)
    new_entities = update_data.get("entities", [])
    print(f"[1] 加载新节点: {len(new_entities)} 个")

    # 2. 匹配
    if candidates_json_path and os.path.exists(candidates_json_path):
        print(f"[2] 从 {candidates_json_path} 加载候选")
        candidates = load_candidates(candidates_json_path)
    else:
        print(f"[2] 现场执行向量匹配...")
        candidates = match_new_nodes(
            new_entities=new_entities,
            neo4j_uri=NEO4J_URI, neo4j_user=NEO4J_USER, neo4j_password=NEO4J_PASSWORD,
            milvus_host=MILVUS_HOST, milvus_port=MILVUS_PORT, milvus_alias=MILVUS_ALIAS,
            milvus_collection=MILVUS_COLLECTION, root_space_id=root_space_id
        )

    # 3. 评估
    print(f"[3] 评估候选对...")
    evaluations = []
    for cand in candidates:
        try:
            ev = evaluate_match(cand["source_node"], cand["matched_node"], cand["similarity"])
            ev["source_name"] = cand["source_node"].get("name", "")
            ev["matched_name"] = cand["matched_node"].get("name", "")
            evaluations.append(ev)
        except Exception as e:
            print(f"  评估失败: {e}")

    # 4. 决策
    print(f"[4] 决策操作分支...")
    decisions = decide_operations(new_entities, candidates, evaluations)
    for d in decisions:
        print(f"  - {d['new_entity'].get('name')}: {d['operation']} (old_id={d['old_node_id']})")

    # 5. 执行
    print(f"[5] 执行知识更新...")
    updater = KnowledgeUpdater(
        neo4j_uri=NEO4J_URI, neo4j_user=NEO4J_USER, neo4j_password=NEO4J_PASSWORD,
        milvus_host=MILVUS_HOST, milvus_port=MILVUS_PORT, milvus_alias=MILVUS_ALIAS,
        milvus_collection=MILVUS_COLLECTION, root_space_id=root_space_id
    )

    results = []
    for dec in decisions:
        op = dec["operation"]
        ent = dec["new_entity"]
        try:
            if op == "CREATE":
                nid = updater.CREATE(ent, dec.get("relations"))
                results.append({"op": "CREATE", "name": ent.get("name"), "new_id": nid})
            elif op == "MERGE":
                nid = updater.MERGE(dec["old_node_id"], ent, dec.get("merged_properties"), dec.get("relations"))
                results.append({"op": "MERGE", "name": ent.get("name"), "node_id": nid})
            elif op == "UPDATE":
                nid = updater.UPDATE(dec["old_node_id"], ent, dec.get("relations"))
                results.append({"op": "UPDATE", "name": ent.get("name"), "node_id": nid})
            elif op == "LINK":
                # 将旧节点作为 to_entity 连接
                to_ent = {
                    "type": dec["new_entity"]["type"],
                    "name": dec["new_entity"]["name"],
                    "properties": dec["new_entity"].get("properties", {})
                }
                f, t = updater.LINK(dec["new_entity"], to_ent, rel_type=dec.get("link_rel_type", "相关"), to_node_id=dec["old_node_id"])
                results.append({"op": "LINK", "name": ent.get("name"), "from_id": f, "to_id": t})
        except Exception as e:
            results.append({"op": op, "name": ent.get("name"), "error": str(e)})
            print(f"  [ERR] {op} {ent.get('name')}: {e}")

    print(f"\n[OK] Pipeline 完成，结果已输出。")
    return results


if __name__ == "__main__":
    
    # clear_neo4j(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    # clear_milvus_collection(MILVUS_HOST, MILVUS_PORT, MILVUS_ALIAS, MILVUS_COLLECTION)
    # import_official_data_to_neo4j(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    # test_neo4j(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    # sync_neo4j_to_milvus(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, MILVUS_HOST, MILVUS_PORT, MILVUS_ALIAS, MILVUS_COLLECTION)
    # test_milvus(MILVUS_HOST, MILVUS_PORT, MILVUS_ALIAS, MILVUS_COLLECTION)


    UPDATE_JSON = "/home/swang/project/test_data/update/official/official_01/extract/update.json"
    CANDIDATES_JSON = "/home/swang/project/algorithm/NL_to_cypher/output/candidates.json"
    run_update_pipeline(UPDATE_JSON, CANDIDATES_JSON)
