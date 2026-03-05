import json
from neo4j import GraphDatabase


def normalize_sibling_type_keys(data, type_names, top_level_class):
    """
    在 mapper 之前调用的 JSON 预处理：仅在最外层做「对象与属性同名则合并」。
    - 当顶层是 list 时：对每个元素，若其同时含有 top_level_class 键与其它 type_names 键（兄弟键），
      则合并为「以 top_level_class 对应值为实体，其余类型键作为嵌套属性」。
    - 当顶层是 dict 时：若其同时含有 top_level_class 与其它 type_names 键，同样合并。
    - 不向内部递归：内部嵌套（如 TermDefinition 里的 Characteristic）不做合并，避免循环/深层误合并。
    type_names: 已知节点类型名集合（与 UML 类名一致），如 {"Rule", "Term", "TermDefinition", "Category", ...}
    """
    type_set = set(type_names) if type_names else set()
    if not type_set or top_level_class not in type_set:
        return data

    def merge_one(obj):
        if not isinstance(obj, dict) or top_level_class not in obj or not isinstance(obj.get(top_level_class), dict):
            return obj
        type_keys = [k for k in obj if k in type_set]
        if len(type_keys) <= 1:
            return obj
        merged = dict(obj[top_level_class])
        for k in type_keys:
            if k != top_level_class:
                merged[k] = obj[k]
        return merged

    if isinstance(data, list):
        return [merge_one(item) for item in data]
    if isinstance(data, dict):
        return merge_one(data)
    return data


class SchemaMapper:
    def __init__(self, uri="bolt://localhost:7687", user="neo4j", password="neo4j2025"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.ontology_cache = {}  # 缓存已存在的本体节点 ID: class_name -> neo4j id
        self.ontology_missing_cache = set()  # 缓存不存在的本体类名，避免重复查询

    def close(self):
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
        node_id = properties.get("RuleId") or properties.get("TermId") or \
                  properties.get("id") or properties.get("RuleName") or \
                  str(hash(frozenset(properties.items())))

        # ---  写入节点 ---
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
                properties.get("RuleId")
                or properties.get("TermId")
                or properties.get("id")
                or properties.get("RuleName")
                or str(hash(frozenset(properties.items())))
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

        # 额外构造与本体类的“虚拟边”信息，模拟 Neo4j 中的 INSTANCE_OF 关系。
        # 为避免对每个节点都访问 Neo4j，这里分两步：
        # 1) 收集所有出现过的 label（类名）
        # 2) 对每个类名最多查询一次，并利用 ontology_cache / ontology_missing_cache
        ontology_relations = []
        # 步骤 1：收集所有 label
        all_labels = {node.get("label") for node in nodes.values() if node.get("label")}
        existing_labels = set()
        for label in all_labels:
            cid = self._get_ontology_id(label)
            if cid is not None:
                existing_labels.add(label)

        # 步骤 2：仅对存在于本体中的类名，生成 INSTANCE_OF 结构描述
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
        # 若有「兄弟键」格式（如模型输出 {Term, TermDefinition, Category} 并列），
        # 先做顶层合并再入库，仅最外层按 top_level_class 合并，内部不递归
        # type_names = {"Rule", "Term", "TermDefinition", "Category", "Glossary", "Characteristic", ...}
        # raw_json = normalize_sibling_type_keys(raw_json, type_names, top_level_class="Rule")
        mapper.map_to_graph(raw_json, top_level_class="Rule")
        print("入库成功！")
    except Exception as e:
        print(f"出错: {e}")
    finally:
        mapper.close()