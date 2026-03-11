"""Neo4j 客户端，用于三元组存储和冲突检测"""

from typing import List, Tuple, Dict, Any, Optional
from neo4j import GraphDatabase, Session
import logging

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Neo4j 数据库客户端，支持三元组的 CRUD 和冲突检测"""
    
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self._ensure_constraints()
    
    def close(self):
        """关闭数据库连接"""
        self.driver.close()
    
    def _ensure_constraints(self):
        """确保必要的索引和约束存在"""
        with self.driver.session() as session:
            # 为主语创建索引
            session.run("CREATE INDEX subject_idx IF NOT EXISTS FOR (n:Subject) ON (n.id)")
            # 为对象创建索引
            session.run("CREATE INDEX object_idx IF NOT EXISTS FOR (n:Object) ON (n.id)")
    
    def insert_triple(self, subject: str, predicate: str, obj: str) -> bool:
        """
        插入单个三元组
        
        Args:
            subject: 主语
            predicate: 谓词
            obj: 宾语
            
        Returns:
            是否成功插入
        """
        with self.driver.session() as session:
            result = session.execute_write(
                self._create_triple, subject, predicate, obj
            )
            return result
    
    @staticmethod
    def _create_triple(tx, subject: str, predicate: str, obj: str):
        # 使用 APOC 或动态 Cypher 构造 (注意：原生 Cypher 不支持参数化 Relationship Type)
        # 方案 A：继续保持属性存储（最稳妥，无需安装 APOC）
        query = """
        MERGE (s:Subject {id: $subject})
        MERGE (o:Object {id: $obj})
        MERGE (s)-[r:RELATION {type: $predicate}]->(o)
        RETURN r
        """
        # 方案 B：如果想让 Neo4j 浏览器里直接看到关系名，改用下面的写法（通过字符串替换）
        # query = f"MERGE (s:Subject {{id: $subject}}) MERGE (o:Object {{id: $obj}}) MERGE (s)-[r:{predicate}]->(o) RETURN r"
        
        result = tx.run(query, subject=subject, predicate=predicate, obj=obj)
        return result.single() is not None
    def delete_triple(self, subject: str, predicate: str, obj: str) -> bool:
        """
        删除单个三元组
        
        Args:
            subject: 主语
            predicate: 谓词
            obj: 宾语
            
        Returns:
            是否成功删除
        """
        with self.driver.session() as session:
            result = session.execute_write(
                self._remove_triple, subject, predicate, obj
            )
            return result
    
    @staticmethod
    def _remove_triple(tx, subject: str, predicate: str, obj: str):
        """事务：删除三元组"""
        query = """
        MATCH (s:Subject {id: $subject})-[r:RELATION {type: $predicate}]->(o:Object {id: $obj})
        DELETE r
        RETURN count(r) as deleted
        """
        result = tx.run(query, subject=subject, predicate=predicate, obj=obj)
        record = result.single()
        return record["deleted"] > 0 if record else False
    
    def check_conflicts(self, subject: str, predicate: str, obj: str) -> List[Dict[str, str]]:
        """
        检查三元组是否与数据库中的数据冲突
        
        冲突规则：
        1. 同一主语+谓词已存在不同宾语（函数属性冲突）
        2. 违反自定义约束规则
        
        Args:
            subject: 主语
            predicate: 谓词
            obj: 宾语
            
        Returns:
            冲突列表，每个冲突包含 {type, existing_triple, message}
        """
        conflicts = []
        
        with self.driver.session() as session:
            # 检查函数属性冲突（同一主语+谓词有不同宾语）
            existing = session.run("""
                MATCH (s:Subject {id: $subject})-[r:RELATION {type: $predicate}]->(o:Object)
                WHERE o.id <> $obj
                RETURN o.id as existing_obj
            """, subject=subject, predicate=predicate, obj=obj)
            
            for record in existing:
                conflicts.append({
                    "type": "functional_property_conflict",
                    "existing_triple": (subject, predicate, record["existing_obj"]),
                    "new_triple": (subject, predicate, obj),
                    "message": f"主语 {subject} 的属性 {predicate} 已有值 {record['existing_obj']}"
                })
        
        return conflicts
    
    def get_related_triples(self, subject: str, max_depth: int = 2) -> List[Tuple[str, str, str]]:
        """
        获取与指定主语相关的所有三元组（用于冲突分析）
        
        Args:
            subject: 主语
            max_depth: 最大遍历深度
            
        Returns:
            相关三元组列表
        """
        with self.driver.session() as session:
            result = session.run("""
                MATCH path = (s:Subject {id: $subject})-[r:RELATION*1..%d]-(n)
                UNWIND relationships(path) as rel
                RETURN startNode(rel).id as s, rel.type as p, endNode(rel).id as o
            """ % max_depth, subject=subject)
            
            triples = []
            for record in result:
                triples.append((record["s"], record["p"], record["o"]))
            
            return list(set(triples))  # 去重
    
    def batch_insert(self, triples: List[Tuple[str, str, str]]) -> Dict[str, Any]:
        """
        批量插入三元组
        
        Args:
            triples: 三元组列表 [(s, p, o), ...]
            
        Returns:
            插入结果统计
        """
        success_count = 0
        failed = []
        
        with self.driver.session() as session:
            for s, p, o in triples:
                try:
                    if session.execute_write(self._create_triple, s, p, o):
                        success_count += 1
                    else:
                        failed.append((s, p, o))
                except Exception as e:
                    logger.error(f"插入失败 ({s}, {p}, {o}): {e}")
                    failed.append((s, p, o))
        
        return {
            "success": success_count,
            "failed": len(failed),
            "failed_triples": failed
        }
    
    def batch_delete(self, triples: List[Tuple[str, str, str]]) -> Dict[str, Any]:
        """
        批量删除三元组
        
        Args:
            triples: 三元组列表 [(s, p, o), ...]
            
        Returns:
            删除结果统计
        """
        success_count = 0
        failed = []
        
        with self.driver.session() as session:
            for s, p, o in triples:
                try:
                    if session.execute_write(self._remove_triple, s, p, o):
                        success_count += 1
                    else:
                        failed.append((s, p, o))
                except Exception as e:
                    logger.error(f"删除失败 ({s}, {p}, {o}): {e}")
                    failed.append((s, p, o))
        
        return {
            "success": success_count,
            "failed": len(failed),
            "failed_triples": failed
        }
    
    def query(self, cypher: str, params: Optional[Dict] = None, parameters: Optional[Dict] = None) -> List[Dict]:
        """
        执行 Cypher 查询
        
        Args:
            cypher: Cypher 查询语句
            params: 查询参数 (兼容旧调用)
            parameters: 查询参数 (兼容新调用，对应 traversal.py)
            
        Returns:
            查询结果列表，每个 record 转换为标准的 Python dict
        """
        # 🎯 核心逻辑：合并两个可能的参数来源
        final_params = parameters if parameters is not None else (params or {})
        
        with self.driver.session() as session:
            # 使用 ** 解包参数
            result = session.run(cypher, **final_params)
            
            # 🎯 核心逻辑：确保返回的是纯 dict 列表，
            # 这样在 delta.py 中 row['s'] 才能正常工作
            return [record.data() for record in result]
    
    def count_triples(self) -> int:
        """统计数据库中的三元组总数"""
        with self.driver.session() as session:
            result = session.run("MATCH ()-[r:RELATION]->() RETURN count(r) as count")
            record = result.single()
            return record["count"] if record else 0
    
    def clear_all(self):
        """清空所有数据（谨慎使用）"""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
            logger.warning("已清空所有数据")
