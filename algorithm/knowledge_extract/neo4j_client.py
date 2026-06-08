"""Neo4j 操作客户端，供知识空间和知识抽取复用。"""
import logging
from neo4j import GraphDatabase

from .config import get_neo4j_config

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Neo4j 连接与基础操作封装。"""

    def __init__(self, uri: str = None, user: str = None, password: str = None):
        cfg = get_neo4j_config()
        self._uri = uri or cfg["uri"]
        self._user = user or cfg["user"]
        self._password = password or cfg["password"]
        self._driver = None

    @property
    def driver(self):
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self._uri, auth=(self._user, self._password)
            )
        return self._driver

    def close(self):
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def run(self, query: str, parameters: dict = None) -> list:
        """执行 Cypher 查询，返回记录列表。"""
        with self.driver.session() as session:
            result = session.run(query, parameters or {})
            return [record.data() for record in result]

    def run_single(self, query: str, parameters: dict = None) -> dict | None:
        """执行 Cypher 查询，返回单条记录。"""
        with self.driver.session() as session:
            result = session.run(query, parameters or {})
            record = result.single()
            return record.data() if record else None

    def node_exists(self, label: str, properties: dict) -> bool:
        """检查节点是否存在。"""
        clauses = " AND ".join(f"n.{k} = ${k}" for k in properties)
        query = f"MATCH (n:{label}) WHERE {clauses} RETURN n LIMIT 1"
        return self.run_single(query, properties) is not None

    def create_node(self, label: str, properties: dict) -> dict:
        """创建节点并返回其属性。"""
        props_str = ", ".join(f"{k}: ${k}" for k in properties)
        query = f"CREATE (n:{label} {{ {props_str} }}) RETURN n"
        records = self.run(query, properties)
        return records[0]["n"] if records else {}

    def create_relationship(
        self,
        from_label: str,
        from_props: dict,
        to_label: str,
        to_props: dict,
        rel_type: str,
    ) -> bool:
        """在两个节点之间创建关系（MERGE 语义：存在则复用）。"""
        from_match = " AND ".join(f"a.{k} = $from_{k}" for k in from_props)
        to_match = " AND ".join(f"b.{k} = $to_{k}" for k in to_props)
        query = (
            f"MATCH (a:{from_label}) WHERE {from_match} "
            f"MATCH (b:{to_label}) WHERE {to_match} "
            f"MERGE (a)-[:{rel_type}]->(b) "
            f"RETURN a, b"
        )
        params = {}
        for k, v in from_props.items():
            params[f"from_{k}"] = v
        for k, v in to_props.items():
            params[f"to_{k}"] = v
        try:
            records = self.run(query, params)
            return len(records) > 0
        except Exception as e:
            logger.error("创建关系失败: %s", e)
            return False
