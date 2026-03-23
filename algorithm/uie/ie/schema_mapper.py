import json
import hashlib
from typing import Any, Optional

from neo4j import GraphDatabase


def _infer_top_level_class(data: Any) -> Optional[str]:
    """从 raw 数据推断 top_level_class。训练数据格式为 {MainObject: [...]}，键即主对象类名。"""
    if isinstance(data, dict) and len(data) == 1:
        key, val = next(iter(data.items()))
        if isinstance(val, list):
            return key
    return None


def json_to_graph_structure(
    data: Any,
    top_level_class: Optional[str] = None,
    neo4j_uri: Optional[str] = None,
    neo4j_user: str = "neo4j",
    neo4j_password: str = "neo4j2025",
) -> dict:
    """
    将 JSON 抽取结果拆分为「节点 + 边」的图结构。

    top_level_class: 可选，不传时从 data 的顶层键自动推断（格式 {MainObject: [...]}）。
    neo4j_uri: 不为 None 时连接 Neo4j 查询 :Class 节点，填充 ontology_relations（实例-INSTANCE_OF-本体类）。
    """
    if top_level_class is None:
        top_level_class = _infer_top_level_class(data)
    if neo4j_uri:
        try:
            mapper = SchemaMapper(uri=neo4j_uri, user=neo4j_user, password=neo4j_password)
            result = mapper.build_graph_structure(data, top_level_class=top_level_class)
            mapper.close()
            return result
        except Exception:
            pass
    mapper = SchemaMapper(uri=None)
    try:
        return mapper.build_graph_structure(data, top_level_class=top_level_class)
    finally:
        mapper.close()


def store_graph_to_neo4j(
    graph_struct: dict,
    uri: str = "bolt://localhost:7687",
    user: str = "neo4j",
    password: str = "neo4j2025",
) -> None:
    """
    将 json_to_graph_structure 生成的图结构写入 Neo4j。
    """
    mapper = SchemaMapper(uri=uri, user=user, password=password)
    try:
        mapper.store_graph_structure(graph_struct)
    finally:
        mapper.close()


__all__ = ["SchemaMapper", "json_to_graph_structure", "store_graph_to_neo4j"]


class SchemaMapper:
    def __init__(self, uri="bolt://localhost:7687", user="neo4j", password="neo4j2025"):
        if uri is None:
            self.driver = None  # 仅用于 build_graph_structure，不连接 Neo4j
        else:
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.ontology_cache = {}  # 缓存已存在的本体节点 ID: class_name -> neo4j id
        self.ontology_missing_cache = set()  # 缓存不存在的本体类名，避免重复查询

    def close(self):
        if self.driver is not None:
            self.driver.close()

    def _get_ontology_id(self, class_name):
        """查找本体节点 :Class {name: 'xxx'} 的 ID，带本地缓存。匹配时忽略大小写。"""
        key = (class_name or "").lower()
        if key in self.ontology_missing_cache:
            return None

        if key in self.ontology_cache:
            return self.ontology_cache[key]
        with self.driver.session() as session:
            res = session.run(
                "MATCH (c:Class) WHERE toLower(c.name) = toLower($name) RETURN id(c) AS cid",
                name=class_name,
            )
            record = res.single()
            if record:
                self.ontology_cache[key] = record["cid"]
                return record["cid"]
            self.ontology_missing_cache.add(key)
        return None

    @staticmethod
    def _pick_name_like_value(properties: dict[str, Any]) -> Optional[str]:
        """
        优先选取字段名包含 name/名称 的字符串值（不区分大小写）作为稳定 UID 来源。
        """
        for k, v in properties.items():
            if not isinstance(v, str):
                continue
            key = str(k).lower()
            if "name" in key or "名称" in str(k):
                val = v.strip()
                if val:
                    return val
        return None

    @staticmethod
    def _stable_props_hash(label: str, properties: dict[str, Any]) -> str:
        """
        对全属性做稳定哈希：按键排序的 JSON 序列化后计算 sha256。
        """
        payload = {"label": label or "Element", "properties": properties or {}}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _build_node_uid(self, label: str, properties: dict[str, Any]) -> str:
        """
        UID 规则：
        1) 优先用字段名含 name/名称 的值（大小写不敏感）；
        2) 否则使用 label + 全属性稳定哈希。
        """
        name_val = self._pick_name_like_value(properties)
        if name_val:
            return f"{label or 'Element'}::{name_val.lower()}"
        return f"{label or 'Element'}::{self._stable_props_hash(label, properties)}"

    def map_to_graph(self, data, parent_id=None, rel_name=None, top_level_class=None):
        if isinstance(data, list):
            if len(data) > 0 and isinstance(data[0], str):
                # 如果是字符串列表，说明它是父节点的属性，不应在这里处理
                return 
            for item in data:
                self.map_to_graph(item, parent_id, rel_name, top_level_class)
            return

        if not isinstance(data, dict):
            return

        # ---  属性与子对象分离 ---
        properties = {}
        nested_objects = []
        for k, v in data.items():
            if isinstance(v, list):
                # 检查列表内部：是字符串(属性)还是字典(子对象)
                if len(v) > 0 and isinstance(v[0], str):
                    properties[k] = v  # 作为属性处理
                else:
                    nested_objects.append((k, v)) # 作为关联对象处理
            elif isinstance(v, dict):
                nested_objects.append((k, v))
            else:
                properties[k] = v

        if len(properties) == 0 and len(nested_objects) == 1:
            child_key, child_val = nested_objects[0]
            self.map_to_graph(child_val, parent_id, child_key, top_level_class)
            return

        # --- 确定 ID 和 标签 ---
        current_label = rel_name if rel_name else (top_level_class if top_level_class else "Element")
        node_id = self._build_node_uid(current_label, properties)

        # ---  写入节点 ---
        if self.driver is None:
            raise RuntimeError("SchemaMapper 未连接 Neo4j，仅支持 build_graph_structure")
        with self.driver.session() as session:

            session.run("MERGE (n:Element {uid: $uid}) SET n += $props", 
                        uid=node_id, props=properties)
            session.run(f"MATCH (n:Element {{uid: $uid}}) SET n:{current_label}", uid=node_id)
            
            cid = self._get_ontology_id(current_label)
            if cid is not None:
                session.run("MATCH (c:Class) WHERE id(c) = $cid MATCH (i:Element {uid: $uid}) MERGE (i)-[:INSTANCE_OF]->(c)", cid=cid, uid=node_id)
            if parent_id and rel_name:
                edge_type = f"HAS_{rel_name.upper()}"
                session.run(f"MATCH (p:Element {{uid: $p_id}}), (c:Element {{uid: $c_id}}) MERGE (p)-[:{edge_type}]->(c)", p_id=parent_id, c_id=node_id)

        # --- 递归处理真正的子对象 ---
        for child_key, child_val in nested_objects:
            self.map_to_graph(child_val, parent_id=node_id, rel_name=child_key)

    # ---------------- 新增：只拆解为节点/边结构，不直接入库 ----------------

    def build_graph_structure(self, data, top_level_class=None):
        """
        将 JSON 抽取结果拆解为「节点 + 边」的中间结构，而不写入 Neo4j。

        返回：
        {
          "nodes": [
            {
              "uid": "RT-1",
              "label": "RuleType",
              "properties": { ...原属性... }
            },
            ...
          ],
          "relationships": [
            {
              "from_uid": "RT-1",
              "to_uid": "RT-1-Child-1",
              "type": "HAS_RULETYPEBODY"
            },
            ...
          ]
        }

        其中 uid 与 label 的计算规则与 map_to_graph 完全一致，方便之后写入。
        """

        nodes = {}
        relationships = []

        def _walk(obj, parent_uid=None, rel_name=None, level_top=top_level_class):
            # 列表：递归处理每个元素（跳过纯字符串列表）
            if isinstance(obj, list):
                if len(obj) > 0 and isinstance(obj[0], str):
                    return
                for item in obj:
                    _walk(item, parent_uid, rel_name, level_top)
                return

            if not isinstance(obj, dict):
                return

            # 属性与子对象分离（同 map_to_graph）
            properties = {}
            nested_objects = []
            for k, v in obj.items():
                if isinstance(v, list):
                    if len(v) > 0 and isinstance(v[0], str):
                        properties[k] = v
                    else:
                        nested_objects.append((k, v))
                elif isinstance(v, dict):
                    nested_objects.append((k, v))
                else:
                    properties[k] = v

            # 只有一个子对象且无自身属性时，直接下沉
            if len(properties) == 0 and len(nested_objects) == 1:
                child_key, child_val = nested_objects[0]
                _walk(child_val, parent_uid, child_key, level_top)
                return

            # 确定当前节点 label 与 uid，与 map_to_graph 逻辑保持一致
            current_label = rel_name if rel_name else (level_top if level_top else "Element")
            node_id = (
                self._build_node_uid(current_label, properties)
            )

            if node_id not in nodes:
                nodes[node_id] = {
                    "uid": node_id,
                    "label": current_label,
                    "properties": dict(properties),
                }
            else:
                # 若已存在，合并属性（后者覆盖前者）
                nodes[node_id]["properties"].update(properties)

            if parent_uid and rel_name:
                edge_type = f"HAS_{rel_name.upper()}"
                relationships.append(
                    {
                        "from_uid": parent_uid,
                        "to_uid": node_id,
                        "type": edge_type,
                    }
                )

            for child_key, child_val in nested_objects:
                _walk(child_val, parent_uid=node_id, rel_name=child_key, level_top=level_top)

        _walk(data, parent_uid=None, rel_name=None, level_top=top_level_class)

        # 额外构造与本体类的“虚拟边”信息（仅当已连接 Neo4j 时查询本体）
        ontology_relations = []
        if self.driver is not None:
            all_labels = {node.get("label") for node in nodes.values() if node.get("label")}
            existing_labels = set()
            for label in all_labels:
                cid = self._get_ontology_id(label)
                if cid is not None:
                    existing_labels.add(label)
            for node in nodes.values():
                label = node.get("label")
                uid = node.get("uid")
                if not label or not uid or label not in existing_labels:
                    continue
                ontology_relations.append(
                    {
                        "from_uid": uid,
                        "class_name": label,
                        "type": "INSTANCE_OF",
                    }
                )

        return {
            "nodes": list(nodes.values()),
            "relationships": relationships,
            "ontology_relations": ontology_relations,
        }

    def store_graph_structure(self, graph_struct, top_level_class=None):
        """
        将 build_graph_structure 生成的中间结构写入 Neo4j。

        预期 graph_struct 结构同 build_graph_structure 的返回。
        top_level_class 仅在需要时可用于补充语义；当前实现主要依赖 node["label"]。
        """
        nodes = graph_struct.get("nodes") or []
        relationships = graph_struct.get("relationships") or []

        with self.driver.session() as session:
            # 先写节点
            for node in nodes:
                uid = node.get("uid")
                label = node.get("label") or "Element"
                props = node.get("properties") or {}

                session.run(
                    "MERGE (n:Element {uid: $uid}) SET n += $props",
                    uid=uid,
                    props=props,
                )
                session.run(
                    f"MATCH (n:Element {{uid: $uid}}) SET n:{label}",
                    uid=uid,
                )

                cid = self._get_ontology_id(label)
                if cid is not None:
                    session.run(
                        "MATCH (c:Class) WHERE id(c) = $cid "
                        "MATCH (i:Element {uid: $uid}) "
                        "MERGE (i)-[:INSTANCE_OF]->(c)",
                        cid=cid,
                        uid=uid,
                    )

            # 再写关系
            for rel in relationships:
                from_uid = rel.get("from_uid")
                to_uid = rel.get("to_uid")
                edge_type = rel.get("type") or "RELATED"
                session.run(
                    f"MATCH (p:Element {{uid: $p_id}}), (c:Element {{uid: $c_id}}) "
                    f"MERGE (p)-[:{edge_type}]->(c)",
                    p_id=from_uid,
                    c_id=to_uid,
                )

if __name__ == "__main__":

    mapper = SchemaMapper()
    
    raw_json =  [
            {
                "RuleId": "RT-1",
                "RuleName": "轮对轮径小于772mm->更新车轮更换（更新）轴箱轴承",
                "Terminology": [
                    "A1型电动车组",
                    "轮对轮径",
                    "车轮",
                    "轴箱轴承"
                ],
                "RuleTypeBody": {
                    "ImplicationRuleType": {
                        "antecedent": "轮对轮径小于772mm",
                        "consequent": "更新车轮、更换（更新）轴箱轴承",
                        "connectionSymbol": "->"
                    }
                }
            },
            {
                "RuleId": "RT-2",
                "RuleName": "轮对轮径在772≤Φ≤790mm且轮缘厚小于28mm AND 轴箱轴承走行公里达到送检周期，齿轮箱大、小轴承走行公里小于160万公里->更新车轮且更换轴箱轴承",
                "Terminology": [
                    "A1型电动车组",
                    "轮对轮径",
                    "车轮",
                    "轴箱轴承"
                ],
                "RuleTypeBody": {
                    "CompositeRuleType": {
                        "CombinationType": "AND",
                        "SubRules": [
                            {
                                "ImplicationRuleType": {
                                    "antecedent": "轮对轮径在772≤Φ≤790mm且轮缘厚小于28mm",
                                    "consequent": "更换相应部件",
                                    "connectionSymbol": "->"
                                }
                            },
                            {
                                "ImplicationRuleType": {
                                    "antecedent": "轴箱轴承走行公里达到送检周期，齿轮箱大、小轴承走行公里小于160万公里",
                                    "consequent": "更新车轮且更换轴箱轴承",
                                    "connectionSymbol": "->"
                                }
                            }
                        ]
                    }
                }
            }
        ]
    
    try:
        mapper.map_to_graph(raw_json, top_level_class="Rule")
        print("入库成功！")
    except Exception as e:
        print(f"出错: {e}")
    finally:
        mapper.close()