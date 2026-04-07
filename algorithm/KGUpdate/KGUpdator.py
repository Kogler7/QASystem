import os
import time
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from neo4j import GraphDatabase
from pathlib import Path
from dotenv import load_dotenv

# 加载 backend/.env - 使用相对路径
env_path = Path(__file__).parent.parent.parent / 'backend' / '.env'
if env_path.exists():
    load_dotenv(env_path)
    print(f"已加载环境变量: {env_path}")
else:
    print(f"环境变量文件不存在: {env_path}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class KGUpdator:
    """
    知识图谱更新器
    """

    def __init__(self,
                 neo4j_uri: Optional[str] = None,
                 neo4j_username: Optional[str] = None,
                 neo4j_password: Optional[str] = None,
                 database: Optional[str] = None,
                 confidence_threshold: Optional[float] = None):
        """
        初始化
        Args:
            neo4j_uri: Neo4j连接URI
            neo4j_username: 用户名
            neo4j_password: 密码
            database: 数据库名
            confidence_threshold: 置信度阈值
        """
        logger.info("初始化知识图谱更新器...")
        
        # 打印环境变量值（调试用）
        logger.info(f"环境变量 NEO4J_URI: {os.getenv('NEO4J_URI')}")
        logger.info(f"环境变量 NEO4J_USER: {os.getenv('NEO4J_USER')}")
        logger.info(f"环境变量 NEO4J_DATABASE: {os.getenv('NEO4J_DATABASE')}")
        
        # 从环境变量或参数获取配置
        self.neo4j_uri = neo4j_uri or os.getenv('NEO4J_URI', 'bolt://localhost:7687')
        self.neo4j_username = neo4j_username or os.getenv('NEO4J_USER', 'neo4j')
        self.neo4j_password = neo4j_password or os.getenv('NEO4J_PASSWORD', 'neo4j2025')
        self.database = database or os.getenv('NEO4J_DATABASE', 'neo4j')
        self.confidence_threshold = confidence_threshold if confidence_threshold is not None else float(os.getenv('KG_CONFIDENCE_THRESHOLD', '0.7'))
        
        logger.info(f"使用的 Neo4j URI: {self.neo4j_uri}")
        logger.info(f"使用的用户名: {self.neo4j_username}")
        logger.info(f"Database: {self.database}")
        logger.info(f"Confidence threshold: {self.confidence_threshold}")
        
        try:
            self.driver = GraphDatabase.driver(
                self.neo4j_uri,
                auth=(self.neo4j_username, self.neo4j_password),
                max_connection_pool_size=20,
                connection_timeout=30
            )
            # 测试连接
            with self.driver.session(database=self.database) as session:
                result = session.run("RETURN 1 as test")
                result.single()
            logger.info("Neo4j 连接成功")
        except Exception as e:
            logger.error(f"Neo4j 连接失败: {e}")
            raise

    def add_relationships_with_full_properties(self, 
                                            relationships: List[Dict[str, Any]],
                                            create_missing_nodes: bool = True) -> Dict[str, Any]:
        """
        批量添加关系，同时保留节点和关系的所有属性
        
        Args:
            relationships: 关系列表，每个元素包含:
                - subject: 主语名称
                - predicate: 关系类型
                - object: 宾语名称
                - confidence: 置信度
                - source_node: 源节点的完整属性（可选）
                - target_node: 目标节点的完整属性（可选）
                - relationship_type: 关系类型（可选）
            create_missing_nodes: 是否创建不存在的节点
        """
        logger.info(f"开始入库 {len(relationships)} 个关系")
        start_time = time.time()
        
        stats = {
            "total": len(relationships),
            "added": 0,
            "updated": 0,
            "failed": 0
        }
        details = []
        
        for rel in relationships:
            subject = rel.get('subject')
            predicate = rel.get('predicate')
            obj = rel.get('object')
            confidence = rel.get('confidence', self.confidence_threshold)
            source_node = rel.get('source_node', {})
            target_node = rel.get('target_node', {})
            
            try:
                with self.driver.session(database=self.database) as session:
                    result = session.execute_write(
                        self._add_relationship_with_properties_tx,
                        subject, predicate, obj, confidence,
                        source_node, target_node, create_missing_nodes
                    )
                
                if result["success"]:
                    if result["created"]:
                        stats["added"] += 1
                    else:
                        stats["updated"] += 1
                    details.append(result)
                else:
                    stats["failed"] += 1
                    details.append(result)
                    
            except Exception as e:
                stats["failed"] += 1
                logger.error(f"入库失败: {e}")
                details.append({"error": str(e)})
        
        elapsed = time.time() - start_time
        stats["elapsed_time"] = round(elapsed, 2)
        
        return {
            "success": stats["failed"] == 0,
            "statistics": stats,
            "details": details
        }

    @staticmethod
    def _add_relationship_with_properties_tx(tx, subject, predicate, obj, confidence,
                                            source_node, target_node, create_missing_nodes):
        """事务：添加关系并保留所有属性"""
        
        # 创建或获取源节点
        source_query = """
        MERGE (s:Entity {name: $name})
        """
        if source_node.get('properties'):
            # 如果有额外属性，设置它们
            props = source_node.get('properties', {})
            set_clause = ', '.join([f"s.{k} = ${k}" for k in props.keys() if k != 'name'])
            if set_clause:
                source_query += f" SET {set_clause}"
        
        tx.run(source_query, {"name": subject, **source_node.get('properties', {})})
        
        # 创建或获取目标节点
        target_query = """
        MERGE (o:Entity {name: $name})
        """
        if target_node.get('properties'):
            props = target_node.get('properties', {})
            set_clause = ', '.join([f"o.{k} = ${k}" for k in props.keys() if k != 'name'])
            if set_clause:
                target_query += f" SET {set_clause}"
        
        tx.run(target_query, {"name": obj, **target_node.get('properties', {})})
        
        # 创建或更新关系
        rel_query = f"""
        MATCH (s:Entity {{name: $subject}})
        MATCH (o:Entity {{name: $object}})
        MERGE (s)-[r:`{predicate}`]->(o)
        SET r.confidence = $confidence,
            r.last_updated = $timestamp
        """
        if not create_missing_nodes:
            rel_query = f"""
            MATCH (s:Entity {{name: $subject}})
            MATCH (o:Entity {{name: $object}})
            MERGE (s)-[r:`{predicate}`]->(o)
            SET r.confidence = $confidence,
                r.last_updated = $timestamp
            """
        
        tx.run(rel_query, {
            "subject": subject,
            "object": obj,
            "confidence": confidence,
            "timestamp": datetime.now().isoformat()
        })
        
        return {"success": True, "created": True}

    def add_triples(self,
                    triples: List[Dict[str, Any]],
                    context: Optional[str] = None) -> Dict[str, Any]:
        """
        批量添加三元组
        Args:
            triples: 每个字典必须包含 subject, predicate, object, confidence
            context: 保留参数，无用
        Returns:
            包含统计信息的字典
        """
        logger.info(f"开始添加 {len(triples)} 个三元组")
        start_time = time.time()

        stats = {
            "total": len(triples),
            "added": 0,
            "updated": 0,
            "rejected": 0,
            "conflicts": 0,
            "failed": 0
        }
        processing_details = []
        conflict_details = []

        for i, triple in enumerate(triples):
            try:
                subject = triple.get("subject")
                predicate = triple.get("predicate")
                obj = triple.get("object")
                input_conf = triple.get("confidence")

                # 必填字段校验
                if not all([subject, predicate, obj]):
                    logger.warning(f"三元组缺少主体/关系/客体，跳过: {triple}")
                    stats["failed"] += 1
                    continue
                if input_conf is None:
                    logger.warning(f"三元组缺少置信度字段，跳过: {triple}")
                    stats["failed"] += 1
                    continue

                # 置信度格式校验并保留两位小数
                try:
                    input_conf = float(input_conf)
                    input_conf = round(max(0.0, min(1.0, input_conf)), 2)
                except (ValueError, TypeError):
                    logger.warning(f"置信度格式错误，跳过: {triple}")
                    stats["failed"] += 1
                    continue

                logger.info(f"处理第 {i+1}/{len(triples)}: ({subject})-[{predicate}]->({obj}) 置信度={input_conf:.2f}")

                # 执行事务处理
                process_result = self._process_single_triple(
                    subject, predicate, obj, input_conf
                )

                # 更新统计
                action = process_result["action"]
                if action == "added":
                    stats["added"] += 1
                elif action == "updated":
                    stats["updated"] += 1
                elif action == "rejected":
                    stats["rejected"] += 1
                elif action == "conflict":
                    stats["conflicts"] += 1
                    if "conflict_detail" in process_result:
                        conflict_details.append(process_result["conflict_detail"])

                # 记录详情
                detail = {
                    "triple": {
                        "subject": subject,
                        "predicate": predicate,
                        "object": obj
                    },
                    "final_confidence": input_conf,
                    "action": action,
                    "reason": process_result.get("reason", ""),
                    "timestamp": datetime.now().isoformat()
                }
                processing_details.append(detail)

            except Exception as e:
                stats["failed"] += 1
                logger.error(f"处理三元组失败: {triple}, 错误: {e}")
                processing_details.append({
                    "triple": triple,
                    "action": "failed",          
                    "error": str(e),
                    "timestamp": datetime.now().isoformat()
                })

        elapsed = time.time() - start_time
        stats["elapsed_time"] = round(elapsed, 2)
        stats["avg_time_per_triple"] = round(elapsed / len(triples), 3) if triples else 0

        # 置信度分布
        high = medium = low = 0
        for d in processing_details:
            conf = d.get("final_confidence", 0)
            if conf >= 0.85:
                high += 1
            elif conf >= self.confidence_threshold:
                medium += 1
            else:
                low += 1
        stats.update({"high_confidence": high, "medium_confidence": medium, "low_confidence": low})

        success = stats["failed"] == 0

        return {
            "success": success,
            "message": self._generate_summary_message(stats),
            "statistics": stats,
            "details": processing_details,
            "conflicts": conflict_details[:20],
            "confidence_distribution": {"high": high, "medium": medium, "low": low}
        }

    def _process_single_triple(self,
                               subject: str,
                               predicate: str,
                               obj: str,
                               confidence: float) -> Dict[str, Any]:
        """在事务中处理单个三元组"""
        with self.driver.session(database=self.database) as session:
            return session.execute_write(
                self._process_triple_tx,
                subject, predicate, obj, confidence
            )

    def _process_triple_tx(self,
                           tx,
                           subject: str,
                           predicate: str,
                           obj: str,
                           new_confidence: float) -> Dict[str, Any]:
        """事务体：检查存在性并执行添加/更新/拒绝"""
        result = {"action": "pending", "reason": ""}

        # 查询是否已存在
        check_query = """
        MATCH (s:Entity {name: $subject})-[r]->(o:Entity {name: $object})
        WHERE type(r) = $predicate
        RETURN r.confidence as existing_conf
        """
        check_result = tx.run(check_query, {
            "subject": subject,
            "predicate": predicate,
            "object": obj
        })
        existing = check_result.single()

        if existing is None:
            # 不存在
            if new_confidence >= self.confidence_threshold:
                add_query = f"""
                MERGE (s:Entity {{name: $subject}})
                MERGE (o:Entity {{name: $object}})
                MERGE (s)-[r:`{predicate}`]->(o)
                SET r.confidence = $confidence,
                    r.created_at = $timestamp,
                    r.last_updated = $timestamp,
                    r.source = 'direct_input'
                """
                tx.run(add_query, {
                    "subject": subject,
                    "object": obj,
                    "confidence": new_confidence,
                    "timestamp": datetime.now().isoformat()
                })
                result["action"] = "added"
                result["reason"] = f"新增关系，置信度{new_confidence:.2f}"
            else:
                result["action"] = "rejected"
                result["reason"] = f"置信度{new_confidence:.2f}低于阈值{self.confidence_threshold}"
        else:
            # 已存在
            existing_conf = existing["existing_conf"] or 0.5
            # 决策逻辑
            threshold = self.confidence_threshold
            if existing_conf >= threshold and new_confidence >= threshold:
                if new_confidence > existing_conf:
                    # 更新
                    update_query = f"""
                    MATCH (s:Entity {{name: $subject}})-[r:`{predicate}`]->(o:Entity {{name: $object}})
                    SET r.confidence = $new_confidence,
                        r.last_updated = $timestamp,
                        r.previous_confidence = $old_confidence
                    """
                    tx.run(update_query, {
                        "subject": subject,
                        "object": obj,
                        "new_confidence": new_confidence,
                        "old_confidence": existing_conf,
                        "timestamp": datetime.now().isoformat()
                    })
                    result["action"] = "updated"
                    result["reason"] = f"更新关系，置信度从{existing_conf:.2f}升至{new_confidence:.2f}"
                else:
                    result["action"] = "rejected"
                    result["reason"] = f"新置信度{new_confidence:.2f}不高于现有{existing_conf:.2f}，保持原关系"
            elif new_confidence >= threshold:
                # 只有新数据达标，更新
                update_query = f"""
                MATCH (s:Entity {{name: $subject}})-[r:`{predicate}`]->(o:Entity {{name: $object}})
                SET r.confidence = $new_confidence,
                    r.last_updated = $timestamp,
                    r.previous_confidence = $old_confidence
                """
                tx.run(update_query, {
                    "subject": subject,
                    "object": obj,
                    "new_confidence": new_confidence,
                    "old_confidence": existing_conf,
                    "timestamp": datetime.now().isoformat()
                })
                result["action"] = "updated"
                result["reason"] = f"新置信度{new_confidence:.2f}达标，更新关系"
            elif existing_conf >= threshold:
                # 只有现有达标，拒绝
                result["action"] = "rejected"
                result["reason"] = f"现有关系置信度{existing_conf:.2f}达标，新置信度{new_confidence:.2f}不达标，保持原关系"
            else:
                # 都不达标，记录冲突
                result["action"] = "conflict"
                result["reason"] = "新旧置信度均低于阈值"
                result["conflict_detail"] = {
                    "triple": {"subject": subject, "predicate": predicate, "object": obj},
                    "existing_confidence": round(existing_conf, 2),
                    "proposed_confidence": new_confidence,
                    "threshold": threshold,
                    "timestamp": datetime.now().isoformat()
                }
        return result

    def delete_triples(self,
                   triples: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        批量删除三元组
        Args:
            triples: 三元组列表，每个字典包含 subject, predicate, object
        Returns:
            包含统计信息的字典
        """
        logger.info(f"开始删除 {len(triples)} 个三元组")
        start_time = time.time()
        stats = {"total": len(triples), "deleted": 0, "not_found": 0, "failed": 0}
        details = []

        for triple in triples:
            subject = triple.get("subject")
            predicate = triple.get("predicate")
            obj = triple.get("object")
            if not all([subject, predicate, obj]):
                logger.warning(f"格式错误，跳过: {triple}")
                stats["failed"] += 1
                continue

            try:
                with self.driver.session(database=self.database) as session:
                    res = session.execute_write(
                        self._delete_triple_tx, subject, predicate, obj
                    )
                if res["success"]:
                    if res["found"]:
                        stats["deleted"] += 1
                    else:
                        stats["not_found"] += 1
                else:
                    stats["failed"] += 1
                details.append({
                    "triple": triple,
                    "success": res["success"],
                    "found": res.get("found", False),
                    "message": res.get("message", ""),
                    "confidence": round(res["confidence"], 2) if res.get("confidence") else None
                })
            except Exception as e:
                stats["failed"] += 1
                logger.error(f"删除失败: {e}")
                details.append({"triple": triple, "success": False, "error": str(e)})

        elapsed = time.time() - start_time
        stats["elapsed_time"] = round(elapsed, 2)
        return {
            "success": stats["failed"] == 0,
            "message": f"删除完成: 总计{stats['total']}个, 成功删除{stats['deleted']}个, 未找到{stats['not_found']}个",
            "statistics": stats,
            "details": details
        }

    @staticmethod
    def _delete_triple_tx(tx, subject, predicate, obj):
        """事务：直接删除三元组"""
        # 检查是否存在
        check_query = f"""
        MATCH (s:Entity {{name: $subject}})-[r:`{predicate}`]->(o:Entity {{name: $object}})
        RETURN r.confidence as confidence
        """
        record = tx.run(check_query, {"subject": subject, "object": obj}).single()
        if not record:
            return {"success": True, "found": False, "message": "三元组不存在"}

        # 直接删除
        delete_query = f"""
        MATCH (s:Entity {{name: $subject}})-[r:`{predicate}`]->(o:Entity {{name: $object}})
        DELETE r
        RETURN count(r) as deleted
        """
        result = tx.run(delete_query, {"subject": subject, "object": obj}).single()
        deleted = result["deleted"] if result else 0
        if deleted > 0:
            return {
                "success": True,
                "found": True,
                "message": "删除成功",
                "confidence": record["confidence"]
            }
        else:
            return {
                "success": False,
                "found": True,
                "message": "删除失败"
            }

    def get_statistics(self) -> Dict[str, Any]:
        """获取知识图谱统计信息"""
        try:
            with self.driver.session(database=self.database) as session:
                # 节点和关系总数
                result = session.run("""
                MATCH (n)
                WITH count(n) as node_count
                MATCH ()-[r]->()
                RETURN node_count, count(r) as relationship_count
                """).single()
                # 置信度分布：用 toFloat 统一类型，避免 confidence 存成字符串时 <0.7 未被计入 low
                conf = session.run("""
                MATCH ()-[r]->()
                WHERE r.confidence IS NOT NULL
                WITH r, toFloat(r.confidence) as conf
                WHERE conf IS NOT NULL
                RETURN 
                    count(CASE WHEN conf >= 0.85 THEN 1 END) as high,
                    count(CASE WHEN conf >= $threshold AND conf < 0.85 THEN 1 END) as medium,
                    count(CASE WHEN conf < $threshold THEN 1 END) as low,
                    count(r) as total
                """, {"threshold": self.confidence_threshold}).single()
                return {
                    "success": True,
                    "statistics": {
                        "node_count": result["node_count"],
                        "relationship_count": result["relationship_count"],
                        "confidence_distribution": {
                            "high": conf["high"] if conf else 0,
                            "medium": conf["medium"] if conf else 0,
                            "low": conf["low"] if conf else 0,
                            "total_with_confidence": conf["total"] if conf else 0
                        }
                    }
                }
        except Exception as e:
            logger.error(f"获取统计失败: {e}")
            return {"success": False, "error": str(e)}

    def close(self):
        """关闭驱动"""
        if self.driver:
            self.driver.close()
            logger.info("知识图谱更新器已关闭")

    def _generate_summary_message(self, stats: Dict[str, Any]) -> str:
        return (f"处理完成: 总计{stats['total']}个, 新增{stats['added']}个, "
                f"更新{stats['updated']}个, 拒绝{stats['rejected']}个, "
                f"冲突{stats['conflicts']}个, 失败{stats['failed']}个, "
                f"耗时{stats['elapsed_time']}秒")


def create_kg_updator(neo4j_uri: Optional[str] = None,
                      neo4j_username: Optional[str] = None,
                      neo4j_password: Optional[str] = None,
                      database: Optional[str] = None,
                      confidence_threshold: Optional[float] = None) -> KGUpdator:
    """创建知识图谱更新器"""
    return KGUpdator(
        neo4j_uri=neo4j_uri,
        neo4j_username=neo4j_username,
        neo4j_password=neo4j_password,
        database=database,
        confidence_threshold=confidence_threshold
    )