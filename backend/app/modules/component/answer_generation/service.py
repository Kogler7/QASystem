"""
答案生成组件：进程内调用 algorithm/ans（Neo4j 知识图谱问答）。
使用前需安装 algorithm/ans/QA 依赖并配置 Neo4j、LLM 等。
"""
import logging
import sys
import json
from pathlib import Path
from typing import Any, List, Dict

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_ANS_ROOT = _ROOT / "algorithm" / "ans"
_ANS_QA = _ANS_ROOT / "QA"
if _ANS_ROOT.exists():
    if str(_ANS_ROOT) not in sys.path:
        sys.path.insert(0, str(_ANS_ROOT))
    if _ANS_QA.exists() and str(_ANS_QA) not in sys.path:
        sys.path.insert(0, str(_ANS_QA))


def _register_tog_alias():
    try:
        import QA
        sys.modules["ToG"] = QA
    except Exception:
        pass


_qa_service = None
_neo4j_driver = None


def _get_qa_service():
    global _qa_service
    if _qa_service is None:
        try:
            _register_tog_alias()
            from QA.answer_solve import Neo4jQAService
            _qa_service = Neo4jQAService()
        except Exception as e:
            logger.warning("加载答案生成模块失败: %s", e)
            raise
    return _qa_service


def _load_neo4j_config() -> Dict[str, Any]:
    config_path = _ROOT / "algorithm" / "ans" / "QA" / ".env.example"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        if key.strip() == "NEO4J_PASSWORD":
                            return {
                                "uri": "bolt://localhost:7687",
                                "user": "neo4j",
                                "password": value.strip(),
                            }
        except Exception as e:
            logger.warning("读取Neo4j配置失败: %s", e)
    return {
        "uri": "bolt://localhost:7687",
        "user": "neo4j",
        "password": "neo4j2025",
    }


def _get_neo4j_driver():
    global _neo4j_driver
    if _neo4j_driver is None:
        try:
            from neo4j import GraphDatabase
            config = _load_neo4j_config()
            _neo4j_driver = GraphDatabase.driver(
                config["uri"],
                auth=(config["user"], config["password"])
            )
        except Exception as e:
            logger.warning("加载Neo4j驱动失败: %s", e)
            raise
    return _neo4j_driver


def close_neo4j_driver():
    global _neo4j_driver
    if _neo4j_driver is not None:
        try:
            _neo4j_driver.close()
            _neo4j_driver = None
        except Exception as e:
            logger.warning("关闭Neo4j驱动失败: %s", e)


def query_graph_by_nodes(node_labels: List[str], limit: int = 20) -> Dict[str, Any]:
    """根据节点标签查询知识图谱"""
    logger.info("查询知识图谱，输入节点标签: %s", node_labels)
    if not node_labels:
        logger.info("没有输入节点标签，返回空结果")
        return {"nodes": [], "relationships": []}
    
    try:
        driver = _get_neo4j_driver()
        with driver.session() as session:
            query = """
            MATCH path = (start)-[r*1..2]-(end)
            WHERE any(label IN labels(start) WHERE label IN $node_labels) 
               OR any(label IN labels(end) WHERE label IN $node_labels)
               OR start.name IN $node_labels
               OR end.name IN $node_labels
            RETURN 
                [node IN nodes(path) | {
                    id: id(node),
                    name: node.name,
                    labels: labels(node),
                    properties: properties(node)
                }] as nodes,
                [rel IN relationships(path) | {
                    id: id(rel),
                    from: id(startNode(rel)),
                    to: id(endNode(rel)),
                    type: type(rel),
                    properties: properties(rel)
                }] as relationships
            LIMIT $limit
            """
            
            result = session.run(query, node_labels=node_labels, limit=limit)
            
            merged_nodes = {}
            merged_rels_dict = {}
            
            for record in result:
                logger.info("处理一条记录: %s", record)
                for node in record["nodes"]:
                    node_id = node["id"]
                    if node_id not in merged_nodes:
                        merged_nodes[node_id] = node
                
                for rel in record["relationships"]:
                    rel_key = f"{rel['from']}-{rel['type']}-{rel['to']}"
                    if rel_key not in merged_rels_dict:
                        merged_rels_dict[rel_key] = rel
            
            nodes_list = list(merged_nodes.values())
            rels_list = list(merged_rels_dict.values())
            
            logger.info("查询结果: 节点数=%d, 关系数=%d", len(nodes_list), len(rels_list))
            
            return {
                "nodes": nodes_list,
                "relationships": rels_list
            }
    except Exception as e:
        logger.error("查询知识图谱失败: %s", e)
        raise


def health() -> dict[str, Any]:
    try:
        _get_qa_service()
        return {"status": "ok", "service": "answer-generation"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"答案生成模块不可用: {e}。请确认已安装 algorithm/ans/QA 依赖并配置 Neo4j、LLM。",
        }


def ask(question: str, detailed: bool = False) -> dict[str, Any]:
    svc = _get_qa_service()
    result = svc.get_solver_detail(question)
    
    # 提取答案
    answer = "无答案"
    try:
        if result.get("solver_result"):
            方案模型列表 = result["solver_result"].get("方案模型", [])
            if 方案模型列表:
                outputs = 方案模型列表[0].get("输出", [])
                if outputs:
                    answer = outputs[0]
    except Exception:
        pass
    
    # 提取原始数据
    solver_result = result.get("solver_result", {})
    
    return {
        "success": result.get("success", False),
        "question": result.get("question", question),
        "answer": answer,
        "timestamp": result.get("timestamp", ""),
        "raw_details": solver_result if result.get("success") else None,
        "error": result.get("error"),
    }


def ask_with_visualization(question: str) -> dict[str, Any]:
    svc = _get_qa_service()
    result = svc.answer_question_with_visualization(question, visualize=True)
    
    visualization_html = None
    raw_details = None
    
    if result.get("success"):
        solver_result = result.get("solver_result", {})
        raw_details = solver_result
        
        answer = "无答案"
        try:
            方案模型列表 = solver_result.get("方案模型", [])
            if 方案模型列表:
                outputs = 方案模型列表[0].get("输出", [])
                if outputs:
                    answer = outputs[0]
        except Exception:
            pass
        
        visualization_path = result.get("visualization")
        if visualization_path and isinstance(visualization_path, str):
            try:
                viz_file = _ROOT / visualization_path
                if viz_file.exists():
                    with open(viz_file, 'r', encoding='utf-8') as f:
                        visualization_html = f.read()
            except Exception as e:
                logger.warning(f"读取可视化文件失败: {e}")
    else:
        answer = result.get("answer", "无答案")
    
    return {
        "success": result.get("success", False),
        "question": result.get("question", question),
        "answer": answer if result.get("success") else result.get("answer", "无答案"),
        "timestamp": result.get("timestamp", ""),
        "raw_details": raw_details,
        "visualization": visualization_html,
        "visualization_type": "html",
        "error": result.get("error"),
    }
