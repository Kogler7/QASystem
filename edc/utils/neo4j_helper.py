from neo4j import GraphDatabase
import logging
from typing import List, Dict, Any, Optional
import random

class OpenKGHelper:
    """
    开源知识图谱数据库操作类
    """
    
    def __init__(self, uri: str = "bolt://host.docker.internal:7687",
                 user: str = "neo4j",
                 password: str = "neo4j2025"):

        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.logger = logging.getLogger(f"{__name__}.OpenKGHelper")
    
    def close(self):

        self.driver.close()
    
    def query_by_head_entity(self, head_entity: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        查询给定头实体的所有三元组
        
        Args:
            head_entity: 头实体名称
            limit: 返回结果数量限制
            
        Returns:
            三元组列表
        """
        def _query(tx, entity, limit_count):
            query = """
            MATCH (subject {name: $entity})-[relation]->(object)
            RETURN subject.name as subject, CASE WHEN relation.name IS NOT NULL THEN relation.name ELSE type(relation) END as predicate, object.name as object
            LIMIT $limit
            """
            result = tx.run(query, entity=entity, limit=limit_count)
            return [{
                "subject": record["subject"], 
                "predicate": record["predicate"], 
                "object": record["object"]
            } for record in result]
        
        try:
            with self.driver.session() as session:
                triples = session.execute_read(lambda tx: _query(tx, head_entity, limit))
                self.logger.info(f"Found {len(triples)} triples for head entity: {head_entity}")
                return triples
        except Exception as e:
            self.logger.error(f"Error querying triples for head entity {head_entity}: {e}")
            return []
    
    def get_random_triples(self, count: int = 10) -> List[Dict[str, Any]]:
        """
        随机获取三元组
        """
        def _query(tx, triple_count):

            query = """
            MATCH (s)-[r]->(o)
            WHERE s.name IS NOT NULL AND o.name IS NOT NULL
            RETURN s.name as subject, r.name as predicate, o.name as object
            LIMIT $limit
            """
            
            result = tx.run(query, limit=triple_count * 3)
            records = [{
                "subject": record["subject"], 
                "predicate": record["predicate"], 
                "object": record["object"]
            } for record in result]
            
            if len(records) <= triple_count:
                random.shuffle(records)
                return records
            else:
                return random.sample(records, triple_count)
        
        try:
            with self.driver.session() as session:
                triples = session.execute_read(lambda tx: _query(tx, count))
                self.logger.info(f"Retrieved {len(triples)} random triples")
                return triples
        except Exception as e:
            self.logger.error(f"Error retrieving random triples: {e}")
            return []
    
    def search_triples(self, 
                      subject: Optional[str] = None,
                      predicate: Optional[str] = None,
                      object: Optional[str] = None,
                      limit: int = 100) -> List[Dict[str, Any]]:
        """
        搜索三元组
        
        Args:
            subject: 主题实体（可选）
            predicate: 关系类型（可选）
            object: 对象实体（可选）
            limit: 返回结果数量限制
            
        Returns:
            匹配的三元组列表
        """
        def _query(tx, sub, pred, obj, limit_count):
            where_clauses = []
            parameters = {"limit": limit_count}
            
            if sub:
                where_clauses.append("s.name CONTAINS $subject")
                parameters["subject"] = sub
            if pred:
                where_clauses.append("r.name CONTAINS $predicate")
                parameters["predicate"] = pred
            if obj:
                where_clauses.append("o.name CONTAINS $object")
                parameters["object"] = obj
            
            where_statement = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
            
            query = f"""
            MATCH (s)-[r]->(o)
            {where_statement}
            RETURN s.name as subject, CASE WHEN r.name IS NOT NULL THEN r.name ELSE e.name END as predicate, o.name as object
            LIMIT $limit
            """
            
            result = tx.run(query, **parameters)
            return [{
                "subject": record["subject"], 
                "predicate": record["predicate"], 
                "object": record["object"]
            } for record in result]
        
        try:
            with self.driver.session() as session:
                triples = session.execute_read(lambda tx: _query(tx, subject, predicate, object, limit))
                self.logger.info(f"Found {len(triples)} triples in search")
                return triples
        except Exception as e:
            self.logger.error(f"Error searching triples: {e}")
            return []


class ManufacturingKGHelper:
    
    def __init__(self, uri: str = "bolt://host.docker.internal:7688",
                 user: str = "neo4j",
                 password: str = "123456789"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.logger = logging.getLogger(f"{__name__}.ManufacturingKGHelper")
    
    def close(self):
        self.driver.close()
    
    def query_triples(self, 
                     subject: Optional[str] = None,
                     predicate: Optional[str] = None,
                     object: Optional[str] = None,
                     limit: int = 100) -> List[Dict[str, Any]]:
        """
        查询三元组
        
        Args:
            subject: 主题实体（可选）
            predicate: 关系类型（可选）
            object: 对象实体（可选）
            limit: 返回结果数量限制
            
        Returns:
            匹配的三元组列表
        """
        def _query(tx, sub, pred, obj, limit_count):
            where_clauses = []
            parameters = {"limit": limit_count}
            
            if sub:
                where_clauses.append("s.name = $subject")
                parameters["subject"] = sub
            if pred:
                where_clauses.append("type(r) = $predicate")
                parameters["predicate"] = pred
            if obj:
                where_clauses.append("o.name = $object")
                parameters["object"] = obj
            
            where_statement = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
            
            query = f"""
            MATCH (s)-[r]->(o)
            {where_statement}
            RETURN s.name as subject, CASE WHEN r.type IS NOT NULL THEN r.type ELSE type(r) END as predicate, o.name as object
            LIMIT $limit
            """
            
            result = tx.run(query, **parameters)
            return [{
                "subject": record["subject"], 
                "predicate": record["predicate"], 
                "object": record["object"]
            } for record in result]
        
        try:
            with self.driver.session() as session:
                triples = session.execute_read(lambda tx: _query(tx, subject, predicate, object, limit))
                self.logger.info(f"Found {len(triples)} triples")
                return triples
        except Exception as e:
            self.logger.error(f"Error querying triples: {e}")
            return []

    def store_triple(self, 
                    subject: str, 
                    predicate: str, 
                    object: str,
                    subject_type: str = "Entity",
                    object_type: str = "Entity",
                    additional_properties: Optional[Dict[str, Any]] = None) -> bool:

        def _query(tx, sub, pred, obj, sub_type, obj_type, props):

            props = props or {}  # 必须修复

            query = f"""
            MERGE (s:{sub_type} {{name: $subject}})
            MERGE (o:{obj_type} {{name: $object}})
            MERGE (s)-[r:{pred}]->(o)
            """

            if props:
                set_props = [f"r.{key} = ${key}" for key in props.keys()]
                query += " SET " + ", ".join(set_props)

            query += " RETURN r"  # 保证可 consume

            parameters = {"subject": sub, "object": obj, **props}
            result = tx.run(query, **parameters)
            summary = result.consume()

            return (summary.counters.nodes_created > 0 or
                    summary.counters.relationships_created > 0)

        try:
            with self.driver.session() as session:
                success = session.execute_write(lambda tx: _query(tx, subject, predicate, object, subject_type, object_type, additional_properties))
                return True
        except Exception as e:
            self.logger.error(f"Error storing triple: {e}")
            return False
        
    def _triples_list_to_dict(self, triples_list):
        output = []
        for tri in triples_list:
            if not tri or len(tri) < 3:
                continue

            s, p, o = tri[0], tri[1], tri[2]
            output.append({
                "subject": s,
                "predicate": p,
                "object": o,
                "subject_type": "Entity",
                "object_type": "Entity",
                "props": {},
            })
        return output


    def batch_store_triples_from_list(self, triples_list):
        """
        triples_list: List[List[str]]
        """
        triples_dict = self._triples_list_to_dict(triples_list)
        return self.batch_store_triples(triples_dict)

    def batch_store_triples(self, triples):
        """
        triples: List[Dict]  每个元素格式：
        {
            "subject": str,
            "predicate": str,
            "object": str,
            "subject_type": str,
            "object_type": str,
            "props": dict
        }
        """

        query = """
        UNWIND $rows AS row
        MERGE (s:`%s` {name: row.subject})
        MERGE (o:`%s` {name: row.object})
        MERGE (s)-[r:RELATION]->(o)
        SET r.type = row.predicate
        SET r += row.props
        """  # subject_type 和 object_type 建议固定为 Entity

        # 规范化
        rows = []
        for t in triples:
            rows.append({
                "subject": t["subject"],
                "object": t["object"],
                "predicate": t["predicate"],
                "props": t.get("props", {})
            })

        try:
            with self.driver.session() as session:
                session.execute_write(lambda tx: tx.run(query, rows=rows))
                return True
        except Exception as e:
            self.logger.error(f"Batch insert error: {e}")
            return False
        