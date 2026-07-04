"""
知识抽取组件 API：进程内调用 algorithm/uie 算法。
符合 CONTRIBUTING：prefix 为 /api/component/knowledge-extract
"""
import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ....deps.auth import require_admin
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/component/knowledge-extract",
    tags=["component", "knowledge-extract"],
    dependencies=[Depends(require_admin)],
)


class ExtractRequestBody(BaseModel):
    main_object: str
    text: str
    use_templates: bool = True
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None


class StoreGraphRequestBody(BaseModel):
    graph: dict
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j2025"


class InitKnowledgeSpaceBody(BaseModel):
    dimension_scales: dict[str, list[str]]


class InitKnowledgeMapSpacesBody(BaseModel):
    space_name: str
    dimension_scales: dict[str, list[str]]


class DimensionScalesBody(BaseModel):
    dimension: str


class ExtractV2Body(BaseModel):
    ontology: str
    text: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


class StoreExtractionBody(BaseModel):
    graph: dict


@router.get("/health")
async def extract_health() -> dict[str, Any]:
    return service.health()


@router.get("/uml-main-objects")
async def api_uml_main_objects() -> dict[str, Any]:
    try:
        items = await asyncio.to_thread(service.list_uml_main_objects)
        return {"items": items}
    except Exception as e:
        logger.exception("读取 UML 主对象列表失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/extract")
async def api_extract(body: ExtractRequestBody) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            service.extract,
            main_object=body.main_object,
            text=body.text,
            use_templates=body.use_templates,
            llm_base_url=body.llm_base_url,
            llm_model=body.llm_model,
            llm_api_key=body.llm_api_key,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("知识抽取失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/parse-chunked-json")
async def api_parse_chunked_json(
    file: UploadFile = File(...),
    main_object: str = Form("Term"),
    use_templates: bool = Form(True),
    llm_base_url: Optional[str] = Form(None),
    llm_model: Optional[str] = Form(None),
    llm_api_key: Optional[str] = Form(None),
) -> dict[str, Any]:
    """
    上传切块后的 JSON 文件（UTF-8），解析出文本片段后对每段执行知识抽取，合并为与 /extract 一致的 {raw, graph}。
    """
    if not file.filename or not str(file.filename).strip():
        raise HTTPException(status_code=400, detail="请选择 JSON 文件")
    try:
        raw_bytes = await file.read()
        result = await asyncio.to_thread(
            service.extract_from_chunked_json_file,
            raw_bytes,
            main_object.strip(),
            use_templates,
            llm_base_url,
            llm_model,
            llm_api_key,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("切块 JSON 解析抽取失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/store-graph")
async def api_store_graph(body: StoreGraphRequestBody) -> dict[str, Any]:
    try:
        await asyncio.to_thread(
            service.store_graph,
            graph_struct=body.graph,
            neo4j_uri=body.neo4j_uri,
            neo4j_user=body.neo4j_user,
            neo4j_password=body.neo4j_password,
        )
        return {"status": "ok", "message": "已写入 Neo4j"}
    except Exception as e:
        logger.exception("存储 Neo4j 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/upload-uml")
async def api_upload_uml(
    file: UploadFile = File(...),
    new_main_object: str = Form(...),
) -> dict[str, Any]:
    """
    上传 UML（.puml），文件保存到 algorithm/uie/ie/UML（含归档副本），并解析 class/属性/关系为图结构。
    """
    if not file.filename or not str(file.filename).strip():
        raise HTTPException(status_code=400, detail="请选择 UML 文件")
    if not file.filename.lower().endswith(".puml"):
        raise HTTPException(status_code=400, detail="仅支持 .puml 文件")
    if not (new_main_object or "").strip():
        raise HTTPException(status_code=400, detail="请填写新增主对象名称")
    try:
        raw_bytes = await file.read()
        result = await asyncio.to_thread(
            service.upload_uml_and_parse,
            raw_bytes,
            file.filename,
            (new_main_object or "").strip() or None,
        )
        return result
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail="UML 文件必须是 UTF-8 编码") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("UML 上传解析失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── 知识空间初始化 ──


@router.post("/init-knowledge-space")
async def api_init_knowledge_space(body: InitKnowledgeSpaceBody) -> dict[str, Any]:
    """
    初始化知识空间：
    - 输入 {维度: [刻度列表]}，例如 {"时间":["年月"], "空间":["集团","子公司"]}
    - 检查维度是否存在、创建/复用刻度节点
    - 创建 KnowledgeSpace 节点（命名：维度.刻度+维度.刻度+...）
    - 连接 KnowledgeSpace BELONGS_TO 刻度
    """
    try:
        result = await asyncio.to_thread(
            service.init_knowledge_space,
            dimension_scales=body.dimension_scales,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("detail", "初始化失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("初始化知识空间失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/storage/knowledge-map/spaces")
async def api_init_knowledge_map_spaces(body: InitKnowledgeMapSpacesBody) -> dict[str, Any]:
    """
    知识空间初始化 V3（笛卡尔积多子空间）：
    - POST /api/component/knowledge-extract/storage/knowledge-map/spaces
    - 输入 space_name（根空间名称）+ dimension_scales（维度刻度映射）
    - 按维度刻度的笛卡尔积生成多个子空间节点
    - 每个子空间连接根空间（CONTAINS）和对应刻度（BELONGS_TO）
    - 返回 root_workspace_id 和子空间列表
    """
    try:
        result = await asyncio.to_thread(
            service.init_knowledge_map_spaces,
            space_name=body.space_name,
            dimension_scales=body.dimension_scales,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("detail", "初始化失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("初始化知识空间（笛卡尔积）失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/dimension-scales")
async def api_dimension_scales(body: DimensionScalesBody) -> dict[str, Any]:
    """根据维度名称返回该维度下的所有刻度。"""
    try:
        result = await asyncio.to_thread(
            service.get_dimension_scales,
            dimension=body.dimension,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("detail", "查询失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("查询维度刻度失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── 知识抽取 V2 ──


@router.post("/extract-v2")
async def api_extract_v2(body: ExtractV2Body) -> dict[str, Any]:
    """
    本体驱动的知识抽取 V2（无模板匹配）：
    - 输入本体 PUML + 待抽取文本
    - 返回 {raw, graph}
    """
    try:
        result = await asyncio.to_thread(
            service.extract_v2,
            ontology=body.ontology,
            text=body.text,
            base_url=body.base_url,
            api_key=body.api_key,
            model=body.model,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("知识抽取 V2 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── 抽取结果入库 ──


@router.post("/store-extraction")
async def api_store_extraction(body: StoreExtractionBody) -> dict[str, Any]:
    """
    将知识抽取 V2 返回的 graph 结构存入 Neo4j。
    按 uid 去重：同一 uid 的节点不重复创建。
    """
    try:
        result = await asyncio.to_thread(
            service.store_extraction_to_neo4j,
            graph=body.graph,
        )
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("detail", "存储失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("存储抽取结果失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
