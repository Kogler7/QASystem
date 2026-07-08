"""
知识沉淀流水线服务：整合文档预处理、知识抽取和知识图谱更新。
"""
import logging
import asyncio
import os
import sys
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends
from langchain_core.documents import Document

from ..modules.component.document_preproc import service as doc_preproc_service
from ..modules.component.knowledge_extract import service as knowledge_extract_service
from ..modules.component.kg_update import service as kg_update_service
from ..modules.component.nl2cypher import service as nl2cypher_service


_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_NL2CYPHER_ROOT = _ROOT / "algorithm" / "NL_to_cypher"
if _NL2CYPHER_ROOT.exists() and str(_NL2CYPHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_NL2CYPHER_ROOT))

_ALGO_ROOT = _ROOT / "algorithm"
if str(_ALGO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALGO_ROOT))

from preproc_src.main_processor import DocumentProcessor

logger = logging.getLogger(__name__)


async def process_document_pipeline(
    file_path: str,
    main_object: str = "Term",
    use_templates: bool = True,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    处理文档并更新知识图谱的完整流水线。
    
    步骤：
    1. 文档预处理（转换为Markdown）
    2. 文本切分（将 Markdown/文本切为 chunks）
    3. 知识抽取（逐 chunk 抽取并合并）
    4. 图谱更新（计算置信度并入库高置信度关系）
    
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

        # 2. 文本切分：预处理 Markdown → nl2cypher split_by_markdown（语义块）→ knowledge_extract 细化超长块（递归切分）
        logger.info("开始文本切分（Markdown 主切分 + LLM 安全细化）")

        def _split_for_pipeline(full_text: str) -> tuple[list[str], str]:
            raw = nl2cypher_service.split_by_markdown(full_text).get("chunks") or []
            strategy = "split_by_markdown"
            if not raw:
                raw = nl2cypher_service.split_recursively(full_text).get("chunks") or []
                strategy = "split_recursively_fallback"
            refined = knowledge_extract_service.refine_chunks_for_llm(raw)
            return refined, strategy

        try:
            chunks, split_strategy = await asyncio.to_thread(_split_for_pipeline, str(content))
        except Exception as e:
            logger.error("文本切分失败: %s", e, exc_info=True)
            return {
                "success": False,
                "step": "split",
                "error": f"文本切分失败: {e}",
            }
        if not chunks:
            return {
                "success": False,
                "step": "split",
                "error": "文本切分结果为空",
            }
        chunked_payload = {"chunks": chunks}

        # 3. 知识抽取（逐 chunk 抽取并合并）
        # 复用 algorithm/uie 的 chunked_json 合并逻辑，避免 nodes/relationships 合并出错。
        logger.info("开始知识抽取: chunks=%d", len(chunks))


        extract_result = await asyncio.to_thread(
            knowledge_extract_service.extract_from_chunked_json_file,
            json.dumps(chunked_payload, ensure_ascii=False).encode("utf-8"),
            main_object,
            use_templates,
            llm_base_url,
            llm_model,
            llm_api_key,
        )
        
        graph_struct = extract_result.get("graph", {})
        if not graph_struct or (isinstance(graph_struct, dict) and not graph_struct.get("nodes")):
            return {
                "success": False,
                "step": "extract",
                "error": "知识抽取结果为空"
            }

        # 4. 从 schema/graph 提取关系并计算置信度
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

        # 5. 将高置信度关系入库到知识图谱
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
            "split_result": {
                "chunk_count": len(chunks),
                "splitter": "nl2cypher.split_by_markdown + knowledge_extract.refine_chunks_for_llm",
                "split_strategy": split_strategy,
            },
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
            "step": "pipeline",
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
    
    # UIE graph 结构：nodes(uid,label,properties) + relationships(from_uid,to_uid,type,properties)
    nodes = graph_struct.get("nodes") or []
    rels = graph_struct.get("relationships") or []
    uid_to_name: dict[Any, str] = {}
    for n in nodes:
        uid = (n or {}).get("uid")
        props = (n or {}).get("properties") or {}
        name = props.get("name") or props.get("title") or props.get("id")
        if uid is not None and isinstance(name, str) and name.strip():
            uid_to_name[uid] = name.strip()

    for r in rels:
        subj = uid_to_name.get((r or {}).get("from_uid"))
        obj = uid_to_name.get((r or {}).get("to_uid"))
        pred = (r or {}).get("type")
        if not (subj and obj and pred):
            continue
        triples.append(
            {
                "subject": subj,
                "predicate": pred,
                "object": obj,
                "confidence": 0.8,
            }
        )
    
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

        # 文本切分依赖 nl2cypher 加载 algorithm/NL_to_cypher/splitter
        nl2cypher_health = nl2cypher_service.health()
        
        return {
            "status": "ok"
            if all(
                [
                    preproc_health.get("status") == "ok",
                    extract_health.get("status") == "ok",
                    kg_health.get("status") == "ok",
                    nl2cypher_health.get("status") == "ok",
                ]
            )
            else "unavailable",
            "services": {
                "document_preproc": preproc_health,
                "knowledge_extract": extract_health,
                "kg_update": kg_health,
                "nl2cypher": nl2cypher_health,
            }
        }
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return {
            "status": "unavailable",
            "error": str(e)
        }

async def knowledge_extract_service(
    file: UploadFile= File(...),
    lang: str = "ch",
    parser_engine: Optional[str] = None,
    chunk_character: Optional[str] ="\n",
    chunk_size: int = 500,
    chunk_type: int = 0,
    similarity: float = 0.6,
    save_to_db: bool = False,
    ontology: str ="",
) -> dict[str, Any]:
    """
    知识提取服务
    
    Args:
        file: 用户上传的原始文件
        lang: 文档语言
        parser_engine: 解析引擎
        chunk_character: 切分符号
        chunk_size: 切片大小
        chunk_type: 切片类型
        similarity: 语义切分相似度阈值
        save_to_db: 是否存入Neo4j
        ontology:本体约束,
    
    Returns:
        知识提取结果
    """

    temp_file_path = None
    file_name_stem = Path(file.filename).stem

    try:
        import vector
    except Exception as e:
        logger.warning("加载依赖失败: %s", e)
        raise
    try:
        # ==================== 1. 保存上传文件 ====================
        suffix = Path(file.filename).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            temp_file_path = tmp.name
        
        logger.info(f"文件已保存: {temp_file_path}, 大小: {len(content)} bytes")
        
        # ==================== 2. 文档解析 ====================
        logger.info(f"开始文档解析，引擎: {parser_engine or '自动选择'}")
        processor = DocumentProcessor()
        parse_result = processor.process_file(
            temp_file_path,
            lang=lang,
            model_name=parser_engine,
        )
        
        if not parse_result.get("success"):
            return {
                "code": 1001,
                "info": f"文档解析失败：{parse_result.get('error', '未知错误')}",
                "data": None
            }

        parse_content = parse_result.get("content", "")
        logger.info(f"文档解析完成，MD长度: {len(parse_content)} 字符")


        # =============文本切片（输入str,输出list[str]）================
        doc=[Document(
            page_content=parse_content
        )]
        if(chunk_type==0):#语义切分 输入文本切片大小、相似度阈值
            chunks=vector.semantic_chunking(parse_content,chunk_size,similarity)
        elif(chunk_type==1):#符号切分 输入文本切片大小、切分符号
            chunks=vector.split_by_character(doc,chunk_size,chunk_character)
            chunks = [chunk.page_content for chunk in chunks]
        elif(chunk_type==2):#递归切分 输入文本切片大小、切分符号
            try:
                # 尝试解析为 JSON 列表
                parsed = json.loads(chunk_character)
                if isinstance(parsed, list):
                    chunk_character = parsed
                else:
                    chunk_character = ["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]
            except (json.JSONDecodeError, ValueError):
                chunk_character = ["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]
            chunks=vector.split_recursively(doc,chunk_size,chunk_character)
            chunks = [chunk.page_content for chunk in chunks]
        elif(chunk_type==3):#md切分 输入文本切片大小
            chunks=vector.split_by_markdown(doc,chunk_size)
            chunks = [chunk.page_content for chunk in chunks]
        # print(chunks)#测试完毕
        #=============知识抽取(输入list[str]、ontology,输出list[str]即json列表)================
        extract_content=["""{
          "graph": {
            "index":"0",
            "nodes": [
              {
                "uid": "businessobject::招采公开项目",
                "label": "BusinessObject",
                "properties": {
                  "name": "招采公开项目",
                  "id": "PARENT-BIM-003",
                  "type": "业务实体",
                  "domain": "采购管理域",
                  "description": "专门记录采用公开采购方式的定标金额与比例，用于监测采购公开透明程度",
                  "businessProcessDescription": "流程：判断采购方式为公开采购 → 发布公开采购公告 → 组织公开采购项目 → 开评定标",
                  "keywords": ["采购策略", "采购方式", "电子采购", "代理商采购"],
                  "stakeholderNames": ["采购管理部门"],
                  "relatedMetrics": ["ICM-1.3", "ICM-1.4"],
                  "parentId": "PARENT-BIM-001",
                  "relatedTable": "ads_dzcg_gongkai_amount"
                }
              },
              {
                "uid": "businessobject::确定采购策略与采购方式",
                "label": "BusinessObject",
                "properties": {
                  "name": "确定采购策略与采购方式",
                  "id": "CHILD-BIM-A05",
                  "type": "业务活动",
                  "domain": "采购管理域",
                  "description": "确定采用电子采购、代理商采购或其他方式",
                  "businessStage": "采购策略与方式判断",
                  "businessProcess": "采购管理域主业务流程",
                  "parentId": "PARENT-BIM-003",
                  "intentType": "看构成 / 做对比 / 做探查",
                  "intentGoal": "识别采购方式、分析采购路径分支",
                  "intentTargetObject": "采购方式",
                  "businessObjects": ["采购策略", "采购方式"]
                }
              },
              {
                "uid": "businessobject::采购方式是否为电子采购",
                "label": "BusinessObject",
                "properties": {
                  "name": "采购方式是否为电子采购",
                  "id": "CHILD-BIM-D02",
                  "type": "决策节点",
                  "domain": "采购管理域",
                  "description": "判断是否采用电子采购方式",
                  "businessStage": "采购策略与方式判断",
                  "parentId": "PARENT-BIM-003"
                }
              },
              {
                "uid": "stakeholder::采购管理部门",
                "label": "Stakeholder",
                "properties": {
                  "name": "采购管理部门",
                  "stakeholderId": "STK-采购管理部门",
                  "role": "采购管理"
                }
              },
              {
                "uid": "metric::公开项目定标金额",
                "label": "Metric",
                "properties": {
                  "name": "公开项目定标金额",
                  "code": "ICM-1.3",
                  "definition": "采用公开采购方式的定标金额",
                  "category": "衍生指标",
                  "unit": "亿元",
                  "sourceTable": "ads_dzcg_gongkai_amount",
                  "sourceField": "gongkai_amt",
                  "dimensions": ["时间"]
                }
              }
            ],
            "relationships": [
              {
                "from_uid": "businessobject::确定采购策略与采购方式",
                "to_uid": "businessobject::招采公开项目",
                "type": "BELONGS_TO_PARENT"
              },
              {
                "from_uid": "businessobject::采购方式是否为电子采购",
                "to_uid": "businessobject::招采公开项目",
                "type": "BELONGS_TO_PARENT"
              },
              {
                "from_uid": "businessobject::招采公开项目",
                "to_uid": "stakeholder::采购管理部门",
                "type": "HAS_STAKEHOLDER"
              },
              {
                "from_uid": "businessobject::招采公开项目",
                "to_uid": "metric::公开项目定标金额",
                "type": "BELONGS_TO_ENTITY"
              }
            ]
          }
        }""","""{
          "graph": {
            "index":"1",
            "nodes": [
              {
                "uid": "businessobject::招采公开项目",
                "label": "BusinessObject",
                "properties": {
                  "name": "招采公开项目",
                  "id": "PARENT-BIM-003",
                  "type": "业务实体",
                  "domain": "采购管理域",
                  "description": "专门记录采用公开采购方式的定标金额与比例，用于监测采购公开透明程度",
                  "businessProcessDescription": "流程：判断采购方式为公开采购 → 发布公开采购公告 → 组织公开采购项目 → 开评定标",
                  "keywords": ["采购策略", "采购方式", "电子采购", "代理商采购"],
                  "stakeholderNames": ["采购管理部门"],
                  "relatedMetrics": ["ICM-1.3", "ICM-1.4"],
                  "parentId": "PARENT-BIM-001",
                  "relatedTable": "ads_dzcg_gongkai_amount"
                }
              },
              {
                "uid": "businessobject::确定采购策略与采购方式",
                "label": "BusinessObject",
                "properties": {
                  "name": "确定采购策略与采购方式",
                  "id": "CHILD-BIM-A05",
                  "type": "业务活动",
                  "domain": "采购管理域",
                  "description": "确定采用电子采购、代理商采购或其他方式",
                  "businessStage": "采购策略与方式判断",
                  "businessProcess": "采购管理域主业务流程",
                  "parentId": "PARENT-BIM-003",
                  "intentType": "看构成 / 做对比 / 做探查",
                  "intentGoal": "识别采购方式、分析采购路径分支",
                  "intentTargetObject": "采购方式",
                  "businessObjects": ["采购策略", "采购方式"]
                }
              },
              {
                "uid": "businessobject::采购方式是否为电子采购",
                "label": "BusinessObject",
                "properties": {
                  "name": "采购方式是否为电子采购",
                  "id": "CHILD-BIM-D02",
                  "type": "决策节点",
                  "domain": "采购管理域",
                  "description": "判断是否采用电子采购方式",
                  "businessStage": "采购策略与方式判断",
                  "parentId": "PARENT-BIM-003"
                }
              },
              {
                "uid": "stakeholder::采购管理部门",
                "label": "Stakeholder",
                "properties": {
                  "name": "采购管理部门",
                  "stakeholderId": "STK-采购管理部门",
                  "role": "采购管理"
                }
              },
              {
                "uid": "metric::公开项目定标金额",
                "label": "Metric",
                "properties": {
                  "name": "公开项目定标金额",
                  "code": "ICM-1.3",
                  "definition": "采用公开采购方式的定标金额",
                  "category": "衍生指标",
                  "unit": "亿元",
                  "sourceTable": "ads_dzcg_gongkai_amount",
                  "sourceField": "gongkai_amt",
                  "dimensions": ["时间"]
                }
              }
            ],
            "relationships": [
              {
                "from_uid": "businessobject::确定采购策略与采购方式",
                "to_uid": "businessobject::招采公开项目",
                "type": "BELONGS_TO_PARENT"
              },
              {
                "from_uid": "businessobject::采购方式是否为电子采购",
                "to_uid": "businessobject::招采公开项目",
                "type": "BELONGS_TO_PARENT"
              },
              {
                "from_uid": "businessobject::招采公开项目",
                "to_uid": "stakeholder::采购管理部门",
                "type": "HAS_STAKEHOLDER"
              },
              {
                "from_uid": "businessobject::招采公开项目",
                "to_uid": "metric::公开项目定标金额",
                "type": "BELONGS_TO_ENTITY"
              }
            ]
          }
        }"""]
        #=============抽取结果向量化（输入list[str],输出list[list[float]]==========
        vectors=vector.batch_text_to_vectors(extract_content)
        # print(vectors)  测试完毕

        # # ==================== 6. 构建 graphs 响应 ====================
        # graphs = build_graphs_response(graph)
        
        # # ==================== 7. 生成 understanding ====================
        # understanding = generate_understanding(graph)
        
        #以下皆为模拟输出数据 需要替换
        graphs =[
        {
          "graph": {
            "index":"0",
            "nodes": [
              {
                "uid": "businessobject::招采公开项目",
                "label": "BusinessObject",
                "properties": {
                  "name": "招采公开项目",
                  "id": "PARENT-BIM-003",
                  "type": "业务实体",
                  "domain": "采购管理域",
                  "description": "专门记录采用公开采购方式的定标金额与比例，用于监测采购公开透明程度",
                  "businessProcessDescription": "流程：判断采购方式为公开采购 → 发布公开采购公告 → 组织公开采购项目 → 开评定标",
                  "keywords": ["采购策略", "采购方式", "电子采购", "代理商采购"],
                  "stakeholderNames": ["采购管理部门"],
                  "relatedMetrics": ["ICM-1.3", "ICM-1.4"],
                  "parentId": "PARENT-BIM-001",
                  "relatedTable": "ads_dzcg_gongkai_amount"
                }
              },
              {
                "uid": "businessobject::确定采购策略与采购方式",
                "label": "BusinessObject",
                "properties": {
                  "name": "确定采购策略与采购方式",
                  "id": "CHILD-BIM-A05",
                  "type": "业务活动",
                  "domain": "采购管理域",
                  "description": "确定采用电子采购、代理商采购或其他方式",
                  "businessStage": "采购策略与方式判断",
                  "businessProcess": "采购管理域主业务流程",
                  "parentId": "PARENT-BIM-003",
                  "intentType": "看构成 / 做对比 / 做探查",
                  "intentGoal": "识别采购方式、分析采购路径分支",
                  "intentTargetObject": "采购方式",
                  "businessObjects": ["采购策略", "采购方式"]
                }
              },
              {
                "uid": "businessobject::采购方式是否为电子采购",
                "label": "BusinessObject",
                "properties": {
                  "name": "采购方式是否为电子采购",
                  "id": "CHILD-BIM-D02",
                  "type": "决策节点",
                  "domain": "采购管理域",
                  "description": "判断是否采用电子采购方式",
                  "businessStage": "采购策略与方式判断",
                  "parentId": "PARENT-BIM-003"
                }
              },
              {
                "uid": "stakeholder::采购管理部门",
                "label": "Stakeholder",
                "properties": {
                  "name": "采购管理部门",
                  "stakeholderId": "STK-采购管理部门",
                  "role": "采购管理"
                }
              },
              {
                "uid": "metric::公开项目定标金额",
                "label": "Metric",
                "properties": {
                  "name": "公开项目定标金额",
                  "code": "ICM-1.3",
                  "definition": "采用公开采购方式的定标金额",
                  "category": "衍生指标",
                  "unit": "亿元",
                  "sourceTable": "ads_dzcg_gongkai_amount",
                  "sourceField": "gongkai_amt",
                  "dimensions": ["时间"]
                }
              }
            ],
            "relationships": [
              {
                "from_uid": "businessobject::确定采购策略与采购方式",
                "to_uid": "businessobject::招采公开项目",
                "type": "BELONGS_TO_PARENT"
              },
              {
                "from_uid": "businessobject::采购方式是否为电子采购",
                "to_uid": "businessobject::招采公开项目",
                "type": "BELONGS_TO_PARENT"
              },
              {
                "from_uid": "businessobject::招采公开项目",
                "to_uid": "stakeholder::采购管理部门",
                "type": "HAS_STAKEHOLDER"
              },
              {
                "from_uid": "businessobject::招采公开项目",
                "to_uid": "metric::公开项目定标金额",
                "type": "BELONGS_TO_ENTITY"
              }
            ]
          }
        },
        {
          "graph": {
            "index":"1",
            "nodes": [
              {
                "uid": "businessobject::招采公开项目",
                "label": "BusinessObject",
                "properties": {
                  "name": "招采公开项目",
                  "id": "PARENT-BIM-003",
                  "type": "业务实体",
                  "domain": "采购管理域",
                  "description": "专门记录采用公开采购方式的定标金额与比例，用于监测采购公开透明程度",
                  "businessProcessDescription": "流程：判断采购方式为公开采购 → 发布公开采购公告 → 组织公开采购项目 → 开评定标",
                  "keywords": ["采购策略", "采购方式", "电子采购", "代理商采购"],
                  "stakeholderNames": ["采购管理部门"],
                  "relatedMetrics": ["ICM-1.3", "ICM-1.4"],
                  "parentId": "PARENT-BIM-001",
                  "relatedTable": "ads_dzcg_gongkai_amount"
                }
              },
              {
                "uid": "businessobject::确定采购策略与采购方式",
                "label": "BusinessObject",
                "properties": {
                  "name": "确定采购策略与采购方式",
                  "id": "CHILD-BIM-A05",
                  "type": "业务活动",
                  "domain": "采购管理域",
                  "description": "确定采用电子采购、代理商采购或其他方式",
                  "businessStage": "采购策略与方式判断",
                  "businessProcess": "采购管理域主业务流程",
                  "parentId": "PARENT-BIM-003",
                  "intentType": "看构成 / 做对比 / 做探查",
                  "intentGoal": "识别采购方式、分析采购路径分支",
                  "intentTargetObject": "采购方式",
                  "businessObjects": ["采购策略", "采购方式"]
                }
              },
              {
                "uid": "businessobject::采购方式是否为电子采购",
                "label": "BusinessObject",
                "properties": {
                  "name": "采购方式是否为电子采购",
                  "id": "CHILD-BIM-D02",
                  "type": "决策节点",
                  "domain": "采购管理域",
                  "description": "判断是否采用电子采购方式",
                  "businessStage": "采购策略与方式判断",
                  "parentId": "PARENT-BIM-003"
                }
              },
              {
                "uid": "stakeholder::采购管理部门",
                "label": "Stakeholder",
                "properties": {
                  "name": "采购管理部门",
                  "stakeholderId": "STK-采购管理部门",
                  "role": "采购管理"
                }
              },
              {
                "uid": "metric::公开项目定标金额",
                "label": "Metric",
                "properties": {
                  "name": "公开项目定标金额",
                  "code": "ICM-1.3",
                  "definition": "采用公开采购方式的定标金额",
                  "category": "衍生指标",
                  "unit": "亿元",
                  "sourceTable": "ads_dzcg_gongkai_amount",
                  "sourceField": "gongkai_amt",
                  "dimensions": ["时间"]
                }
              }
            ],
            "relationships": [
              {
                "from_uid": "businessobject::确定采购策略与采购方式",
                "to_uid": "businessobject::招采公开项目",
                "type": "BELONGS_TO_PARENT"
              },
              {
                "from_uid": "businessobject::采购方式是否为电子采购",
                "to_uid": "businessobject::招采公开项目",
                "type": "BELONGS_TO_PARENT"
              },
              {
                "from_uid": "businessobject::招采公开项目",
                "to_uid": "stakeholder::采购管理部门",
                "type": "HAS_STAKEHOLDER"
              },
              {
                "from_uid": "businessobject::招采公开项目",
                "to_uid": "metric::公开项目定标金额",
                "type": "BELONGS_TO_ENTITY"
              }
            ]
          }
        }
      ]

        return {
            "code": 0,
            "info": "知识提取完成",
            "data": {
                "name": "base_name",
                "understanding": f"这是知识理解描述...",
                "origin_url": "origin_url",
                "vector_url": "vector_url",
                "extract_url": "extract_url",
                "graphs": graphs
            }
        }
        
    except Exception as e:
        return {
            "code": 500,
            "info": f"知识提取失败: {str(e)}",
            "data": None
        }
    finally:
        # 清理临时文件
        if temp_file_path and Path(temp_file_path).exists():
            try:
                os.unlink(temp_file_path)
            except Exception:
                pass


# ==================== 辅助函数 ====================

def build_graphs_response(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """构建接口文档定义的 graphs 格式"""
    if not graph:
        return []
    
    if isinstance(graph, list):
        result = []
        for idx, g in enumerate(graph):
            if g.get("nodes") or g.get("relationships"):
                result.append({
                    "graph": {
                        "index": str(idx),
                        "nodes": g.get("nodes", []),
                        "relationships": g.get("relationships", [])
                    }
                })
        return result
    
    if graph.get("nodes") and graph.get("relationships"):
        return [
            {
                "graph": {
                    "index": "0",
                    "nodes": graph.get("nodes", []),
                    "relationships": graph.get("relationships", [])
                }
            }
        ]
    return []


def generate_understanding(graph: Dict[str, Any]) -> str:
    """生成知识的自然语言描述"""
    if not graph:
        return "未抽取到知识节点"
    
    nodes = graph.get("nodes", [])
    relationships = graph.get("relationships", [])
    
    if not nodes:
        return "未抽取到知识节点"
    
    labels = {}
    for node in nodes:
        label = node.get("label", "Unknown")
        labels[label] = labels.get(label, 0) + 1
    
    label_desc = "、".join([f"{count} 个 {label}" for label, count in labels.items()])
    
    return (f"本知识描述了业务域中的核心实体与关系，"
            f"共抽取 {len(nodes)} 个节点（{label_desc}），"
            f"{len(relationships)} 条关系，形成完整的知识图谱结构。")

