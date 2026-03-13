from neo4j import GraphDatabase
import json
import logging
from datetime import datetime
from typing import List, Dict, Any, Set, Tuple

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Neo4jClient:
    """Neo4j知识图谱客户端"""
    def __init__(self, uri: str = "bolt://localhost:7687",
                 username: str = "neo4j",
                 password: str = "password"):
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self._test_connection()

    def _test_connection(self):
        """测试连接"""
        try:
            with self.driver.session() as session:
                result = session.run("RETURN 'Neo4j连接成功' as message")
                logger.info(result.single()["message"])
        except Exception as e:
            logger.error(f"Neo4j连接失败: {e}")
            raise

    def close(self):
        """关闭连接"""
        self.driver.close()

    def query_all(self, method: str, *args):
        """统一查询接口，兼容原有代码"""
        if method == "label2qid":
            return self.label2qid(args[0])
        elif method == "qid2label":
            return self.qid2label(args[0])
        elif method == "get_all_relations_of_an_entity":
            return self.get_all_relations_of_an_entity(args[0])
        elif method == "get_tail_entities_given_head_and_relation":
            return self.get_tail_entities_given_head_and_relation(args[0], args[1])
        elif method == "get_tail_values_given_head_and_relation":
            return self.get_tail_values_given_head_and_relation(args[0], args[1])
        else:
            return {"Not Found!"}

    def label2qid(self, label: str) -> Set[str]:
        """通过中文标签查找实体ID"""
        query = """
        MATCH (n)
        WHERE n.name CONTAINS $label OR n.name = $label
        RETURN n.name as name, labels(n) as labels, id(n) as id
        LIMIT 10
        """

        with self.driver.session() as session:
            result = session.run(query, label=label)
            qids = set()

            for record in result:
                # 使用Neo4j内部ID作为qid，前缀为"NEO_"
                qid = f"NEO_{record['id']}"
                qids.add(qid)

            return qids if qids else {"Not Found!"}

    def qid2label(self, qid: str) -> Set[str]:
        """通过ID查找实体标签"""
        if not qid.startswith("NEO_"):
            return {"Not Found!"}

        entity_id = int(qid.replace("NEO_", ""))
        query = """
        MATCH (n)
        WHERE id(n) = $id
        RETURN n.name as name
        """

        with self.driver.session() as session:
            result = session.run(query, id=entity_id)
            labels = set()

            for record in result:
                labels.add(record["name"])

            return labels if labels else {"Not Found!"}

    def get_all_relations_of_an_entity(self, entity_qid: str) -> Dict[str, List[Dict]]:
        """获取实体的所有关系（出度和入度）"""
        if not entity_qid.startswith("NEO_"):
            return {"head": [], "tail": []}

        entity_id = int(entity_qid.replace("NEO_", ""))

        # 全面的关系查询
        out_query = """
        MATCH (start)-[r]->(end)
        WHERE id(start) = $id
        RETURN 
            type(r) as relation_type,
            end.name as target_name,
            labels(end) as target_labels,
            id(end) as target_id,
            'out' as direction
        """

        in_query = """
        MATCH (start)-[r]->(end)
        WHERE id(end) = $id
        RETURN 
            type(r) as relation_type,
            start.name as source_name,
            labels(start) as source_labels,
            id(start) as source_id,
            'in' as direction
        """

        with self.driver.session() as session:
            head_relations = []
            tail_relations = []

            # 查询出度关系
            out_result = session.run(out_query, id=entity_id)
            for record in out_result:
                head_relations.append({
                    'qid': record['relation_type'],
                    'label': record['relation_type'],
                    'direction': 'out'
                })

            # 查询入度关系
            in_result = session.run(in_query, id=entity_id)
            for record in in_result:
                tail_relations.append({
                    'qid': record['relation_type'],
                    'label': record['relation_type'],
                    'direction': 'in'
                })

            # 去重
            seen_head = set()
            unique_head = []
            for rel in head_relations:
                key = rel['label']
                if key not in seen_head:
                    seen_head.add(key)
                    unique_head.append(rel)

            seen_tail = set()
            unique_tail = []
            for rel in tail_relations:
                key = rel['label']
                if key not in seen_tail:
                    seen_tail.add(key)
                    unique_tail.append(rel)

            return {"head": unique_head, "tail": unique_tail}

    def get_tail_entities_given_head_and_relation(self, head_qid: str, relation: str) -> Dict[str, List[Dict]]:
        """根据头和关系查找尾实体"""
        if not head_qid.startswith("NEO_"):
            print(f"非Neo4j格式的QID: {head_qid}")
            return {"head": [], "tail": []}

        head_id = int(head_qid.replace("NEO_", ""))

        #正确处理关系类型（去掉空格，转为大写）
        relation_clean = relation.strip().upper().replace(" ", "_")
        # 先尝试出度关系查询
        out_query = """
        MATCH (start)-[r]->(end)
        WHERE id(start) = $head_id AND type(r) = $relation
        RETURN 
            end.name as name,
            labels(end) as labels,
            id(end) as id
        LIMIT 20
        """

        with self.driver.session() as session:
            tails = []

            # 尝试查询
            try:
                result = session.run(out_query, head_id=head_id, relation=relation_clean)

                for record in result:
                    tail_qid = f"NEO_{record['id']}"
                    tail_name = record['name'] if record['name'] else "Unname_Entity"
                    tails.append({
                        'qid': tail_qid,
                        'label': tail_name
                    })
                if not tails:
                    # 尝试入度关系查询
                    in_query = """
                    MATCH (start)-[r]->(end)
                    WHERE id(end) = $head_id AND type(r) = $relation
                    RETURN 
                        start.name as name,
                        labels(start) as labels,
                        id(start) as id
                    LIMIT 20
                    """

                    result = session.run(in_query, head_id=head_id, relation=relation_clean)
                    for record in result:
                        tail_qid = f"NEO_{record['id']}"
                        tail_name = record['name'] if record['name'] else "Unname_Entity"
                        tails.append({
                            'qid': tail_qid,
                            'label': tail_name
                        })
                        print(f"   通过入度找到实体: {tail_name} -> {tail_qid}")
            except Exception as e:
                print(f"查询执行异常: {e}")
            return {"head": [], "tail": tails}

    def get_tail_values_given_head_and_relation(self, head_qid: str, relation: str) -> Set[str]:
        """根据头和关系查找尾值（字符串值）"""
        return set()

    def execute_cypher(self, cypher_query: str, **params) -> List[Dict]:
        """执行Cypher查询"""
        with self.driver.session() as session:
            result = session.run(cypher_query, **params)
            return [record.data() for record in result]

    def search_similar_entities(self, keyword: str, limit: int = 10) -> List[Dict]:
        """搜索相似实体"""
        query = """
        MATCH (n)
        WHERE n.name CONTAINS $keyword OR 
              any(label in labels(n) WHERE label CONTAINS $keyword)
        RETURN 
            n.name as name,
            labels(n) as labels,
            id(n) as id,
            properties(n) as properties
        LIMIT $limit
        """

        return self.execute_cypher(query, keyword=keyword, limit=limit)

    def get_two_hop_relations(self, start_qid: str) -> List[Dict]:
        """获取两跳内的关系"""
        if not start_qid.startswith("NEO_"):
            return []

        start_id = int(start_qid.replace("NEO_", ""))

        query = """
        MATCH path = (start)-[*1..2]-(connected)
        WHERE id(start) = $start_id
        RETURN 
            [node in nodes(path) | {name: node.name, labels: labels(node), id: id(node)}] as nodes,
            [rel in relationships(path) | {type: type(rel), properties: properties(rel)}] as relationships
        LIMIT 20
        """

        return self.execute_cypher(query, start_id=start_id)


class MultiServerWikidataQueryClient:
    """兼容原接口的包装类，使用Neo4j客户端"""
    def __init__(self, neo4j_uri="bolt://localhost:7687",
                 username="neo4j", password="password"):
        self.client = Neo4jClient(neo4j_uri, username, password)
        logger.info("Neo4j知识图谱客户端初始化完成")

    def query_all(self, method: str, *args):
        return self.client.query_all(method, *args)