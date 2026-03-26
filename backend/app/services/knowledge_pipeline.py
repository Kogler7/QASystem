"""
知识沉淀流水线服务：整合文档预处理、知识抽取和知识图谱更新。
"""
import logging
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..modules.component.document_preproc import service as doc_preproc_service
from ..modules.component.knowledge_extract import service as knowledge_extract_service
from ..modules.component.kg_update import service as kg_update_service

logger = logging.getLogger(__name__)


async def process_document_pipeline(
    file_path: str,
    main_object: str = "文档",
    use_templates: bool = True,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    处理文档并更新知识图谱的完整流水线。
    
    步骤：
    1. 文档预处理（转换为Markdown）
    2. 知识抽取（从文本中提取实体和关系）
    3. 知识图谱更新（将提取的知识添加到图谱）
    
    Args:
        file_path: 文档文件路径
        main_object: 主对象类型
        use_templates: 是否使用模板
        llm_base_url: LLM基础URL
        llm_model: LLM模型名称
        llm_api_key: LLM API密钥
    
    Returns:
        处理结果
    """
    try:
        # 1. 文档预处理
        logger.info(f"开始文档预处理: {file_path}")
        preproc_result = await asyncio.to_thread(
            doc_preproc_service.convert_document,
            input_path=file_path
        )
        
        if not preproc_result.get("success", True):
            return {
                "success": False,
                "step": "preproc",
                "error": preproc_result.get("error", "文档预处理失败")
            }

        # 预处理模块的输出字段可能随算法调整（content/text/markdown 等）
        content = (
            preproc_result.get("content")
            or preproc_result.get("text")
            or preproc_result.get("markdown")
            or ""
        )
        if not str(content).strip():
            return {
                "success": False,
                "step": "preproc",
                "error": "文档预处理后内容为空"
            }
        
        # 2. 知识抽取
        logger.info("开始知识抽取")
        extract_result = await asyncio.to_thread(
            knowledge_extract_service.extract,
            main_object=main_object,
            text=content,
            use_templates=use_templates,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_api_key=llm_api_key
        )
        
        graph_struct = extract_result.get("graph", {})
        if not graph_struct or (isinstance(graph_struct, dict) and not graph_struct.get("nodes")):
            return {
                "success": False,
                "step": "extract",
                "error": "知识抽取结果为空"
            }

        # 3. 从 KGUpdate 的 schema/graph 提取关系并计算置信度
        # 注意：这里的 graph 结构来自 uie 抽取，字段是 nodes/relationships（而不是 entities/relations）。
        confidence_threshold = 0.7
        logger.info("计算关系置信度并筛选高置信度")
        kg_compute_result = await asyncio.to_thread(
            kg_update_service.process_schema_output,
            {"raw": extract_result.get("raw", {}), "graph": graph_struct},
            confidence_threshold,
        )

        if not kg_compute_result or not kg_compute_result.get("success"):
            return {
                "success": False,
                "step": "kg_compute",
                "error": kg_compute_result.get("message") if isinstance(kg_compute_result, dict) else "KG 置信度计算失败",
            }

        # 4. 将高置信度关系入库到知识图谱
        logger.info("入库高置信度关系到知识图谱")
        triples = kg_compute_result.get("relations_high", [])
        kg_result = await asyncio.to_thread(
            kg_update_service.add_relations_from_computed,
            kg_compute_result.get("relations_high", []),
            kg_compute_result.get("full_relations", []),
            kg_compute_result.get("predictions", []),
            confidence_threshold,
        )
        
        return {
            "success": True,
            "preproc_result": preproc_result,
            "extract_result": extract_result,
            "triples": triples,
            "kg_result": kg_result,
            "kg_compute_result": kg_compute_result,
            "message": "知识沉淀流程完成"
        }
    
    except Exception as e:
        logger.error(f"知识沉淀流水线失败: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


def _convert_graph_to_triples(graph_struct: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    将抽取的图结构转换为知识图谱三元组格式。
    
    Args:
        graph_struct: 抽取的图结构
    
    Returns:
        三元组列表
    """
    triples = []
    
    if not graph_struct:
        return triples
    
    # 处理实体和关系
    entities = graph_struct.get("entities", [])
    relations = graph_struct.get("relations", [])
    
    # 构建实体映射
    entity_map = {entity.get("id"): entity.get("name") for entity in entities}
    
    # 转换关系为三元组
    for relation in relations:
        subject_id = relation.get("subject_id")
        object_id = relation.get("object_id")
        predicate = relation.get("predicate")
        
        if subject_id and object_id and predicate:
            subject = entity_map.get(subject_id)
            object_ = entity_map.get(object_id)
            
            if subject and object_:
                triples.append({
                    "subject": subject,
                    "predicate": predicate,
                    "object": object_,
                    "confidence": 0.8  # 默认置信度
                })
    
    return triples


def health_check() -> Dict[str, Any]:
    """
    检查知识沉淀流水线的健康状态。
    
    Returns:
        健康状态
    """
    try:
        # 检查文档预处理服务
        preproc_health = doc_preproc_service.health()
        
        # 检查知识抽取服务
        extract_health = knowledge_extract_service.health()
        
        # 检查知识图谱更新服务
        kg_health = kg_update_service.health()
        
        return {
            "status": "ok" if all(
                preproc_health.get("status") == "ok",
                extract_health.get("status") == "ok",
                kg_health.get("status") == "ok"
            ) else "unavailable",
            "services": {
                "document_preproc": preproc_health,
                "knowledge_extract": extract_health,
                "kg_update": kg_health
            }
        }
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return {
            "status": "unavailable",
            "error": str(e)
        }
