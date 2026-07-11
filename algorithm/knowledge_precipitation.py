"""
知识沉淀接口 - 文档解析 → 知识抽取 → 切片 → 向量化
符合接口文档：
- 接收原始文件，自动完成文档解析（MD转换）、知识抽取、文本切片、向量化
- 返回：natural language understanding summary, origin_url, vector_url, extract_url, graphs
"""
import json
import logging
import tempfile
import os
import re
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime

import requests
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from langchain_core.documents import Document
from pydantic import BaseModel, Field

_ALGO_ROOT = Path(__file__).resolve().parent
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
from algorithm.knowledge_extract import extract_v2, store_extraction_to_neo4j

# 从 NL_to_cypher 导入向量化和切分函数
from algorithm.NL_to_cypher.vector import batch_text_to_vectors, text_to_vector as single_text_to_vector
from algorithm.NL_to_cypher.splitter import (
    split_by_character,
    split_recursively,
    split_by_markdown,
)
from algorithm.confidence_calculate.calculator import calculate_graph_confidence

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge-precipitation"])

DEFAULT_ONTOLOGY = """@startuml
class BusinessObject {
  name
  description
}
@enduml"""

MOCK_DIR = _ALGO_ROOT / "data" / "mock_storage"
FILE_DIR = MOCK_DIR / "files"


class TextVectorRequest(BaseModel):
    text: str = Field(..., description="待向量化文本")


class BatchTextVectorRequest(BaseModel):
    texts: List[str] = Field(..., description="待向量化文本列表")


class GraphRequest(BaseModel):
    graph: Dict[str, Any] = Field(..., description="包含 nodes 和 relationships 的知识图谱数据")


# ==================== 2. 辅助函数 ====================

def parse_chunk_character(raw: Optional[str]) -> List[str]:
    """解析切分符号 JSON 数组字符串"""
    if not raw:
        return ['.', '。', '\n', '\n\n', '?', '!']
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(item) for item in result]
        return ['.', '。', '\n', '\n\n', '?', '!']
    except json.JSONDecodeError:
        return ['.', '。', '\n', '\n\n', '?', '!']


def chunk_document(content: str, chunk_type: int = 0, chunk_size: int = 500,
                   chunk_character: Optional[List[str]] = None,
                   similarity: float = 0.6) -> List[str]:
    """
    文档切片分发器 - 复用 splitter.py 中的函数
    
    Args:
        content: 文档内容
        chunk_type: 0语义/1符号/2递归/3Markdown
        chunk_size: 切片大小
        chunk_character: 切分符号列表（仅对符号切分有效）
        similarity: 语义切分相似度阈值（仅对语义切分有效，暂未使用）
    
    Returns:
        List[str]: 切片后的文本列表
    """
    if not content:
        return []
    
    # 将文本转为 Document 列表
    doc = Document(page_content=content, metadata={})
    docs = [doc]
    
    if chunk_type == 0:
        # 语义切分 - 暂用递归切分替代，后续可接入真正的语义切分
        split_docs = split_recursively(docs, chunk_size=chunk_size)
        return [d.page_content for d in split_docs]
    elif chunk_type == 1:
        # 符号切分
        separator = (chunk_character or ["\n"])[0]
        split_docs = split_by_character(docs, chunk_size=chunk_size, separator=separator)
        return [d.page_content for d in split_docs]
    elif chunk_type == 2:
        # 递归切分
        split_docs = split_recursively(
            docs,
            chunk_size=chunk_size,
            separators=chunk_character,
        )
        return [d.page_content for d in split_docs]
    elif chunk_type == 3:
        # Markdown 切分
        split_docs = split_by_markdown(docs, chunk_size=chunk_size)
        return [d.page_content for d in split_docs]
    else:
        return [content]


def md_to_text(markdown_content: str) -> str:
    """将 Markdown 转换为纯文本（用于知识抽取）"""
    if not markdown_content:
        return ""
    
    text = markdown_content
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'`.*?`', '', text)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[([^\]]*?)\]\([^)]*?\)', r'\1', text)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*+]\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'__(.*?)__', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        if '|' in line and not line.strip().startswith('|---'):
            line = line.strip()
            if line.startswith('|'):
                line = line[1:]
            if line.endswith('|'):
                line = line[:-1]
            cells = [cell.strip() for cell in line.split('|')]
            line = ' '.join(cells)
        cleaned_lines.append(line)
    text = '\n'.join(cleaned_lines)
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
    return text.strip()


def normalize_document_content(content: Any) -> str:
    """Normalize parser output into text for storage, extraction, and chunking."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, indent=2)


def build_graphs_response(graph: Dict[str, Any]) -> Dict[str, List]:
    """构建接口文档定义的 graphs 格式"""
    if not graph:
        return {"graphs": []}
    
    if isinstance(graph, list):
        graphs_list = []
        for idx, g in enumerate(graph):
            if g.get("nodes") or g.get("relationships"):
                graphs_list.append({
                    "graph": {
                        "index": str(idx),
                        "nodes": g.get("nodes", []),
                        "relationships": g.get("relationships", [])
                    }
                })
        return {"graphs": graphs_list}
    
    if graph.get("nodes") or graph.get("relationships"):
        return {
            "graphs": [
                {
                    "graph": {
                        "index": "0",
                        "nodes": graph.get("nodes", []),
                        "relationships": graph.get("relationships", [])
                    }
                }
            ]
        }
    return {"graphs": []}


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


def _safe_name(filename: str) -> str:
    name = Path(filename or "upload.bin").name
    return name or "upload.bin"


def _save_file_bytes(
    content: bytes,
    file_name: str,
    knowledge_space_id: Optional[str] = None,
) -> Dict[str, Any]:
    file_id = str(uuid.uuid4())
    filename = _safe_name(file_name)
    target_dir = FILE_DIR / file_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    target_path.write_bytes(content)
    return {
        "id": file_id,
        "file_name": filename,
        "file_path": str(target_path),
        "file_url": f"/file/{file_id}/{filename}",
        "knowledgeSpaceId": knowledge_space_id,
    }


def upload_md_to_file_service(md_content: str, file_name: str,
                              file_service_url: str,
                              knowledge_space_id: Optional[str] = None) -> Dict[str, Any]:
    """将 Markdown 内容上传到文件存储服务"""
    try:
        if not file_service_url:
            data = _save_file_bytes(md_content.encode("utf-8"), file_name, knowledge_space_id)
            return {
                "success": True,
                "file_id": data.get("id"),
                "file_path": data.get("file_path"),
                "file_url": data.get("file_url"),
                "error": None,
            }

        upload_url = f"{file_service_url.rstrip('/')}/third/files/upload"
        
        files = {
            'file': (file_name, md_content.encode('utf-8'), 'text/markdown')
        }
        
        data = {}
        if knowledge_space_id:
            data['knowledgeSpaceId'] = knowledge_space_id
        
        logger.info(f"上传文件到: {upload_url}, 文件名: {file_name}")
        
        response = requests.post(upload_url, files=files, data=data, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                data = result.get('data', {})
                return {
                    "success": True,
                    "file_id": data.get('id'),
                    "file_path": data.get('file_path'),
                    "file_url": data.get('file_url'),
                    "error": None
                }
            else:
                return {
                    "success": False,
                    "file_id": None,
                    "file_path": None,
                    "file_url": None,
                    "error": result.get('info', '上传失败')
                }
        else:
            return {
                "success": False,
                "file_id": None,
                "file_path": None,
                "file_url": None,
                "error": f"HTTP {response.status_code}"
            }
    
    except Exception as e:
        logger.error(f"上传文件失败: {e}")
        return {
            "success": False,
            "file_id": None,
            "file_path": None,
            "file_url": None,
            "error": str(e)
        }


# ==================== 3. 主接口 ====================

@router.post("/api/component/nl2cypher/text-to-vector")
async def text_to_vector_api(body: TextVectorRequest) -> Dict[str, Any]:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="文本不能为空")
    vector = single_text_to_vector(text)
    return {
        "code": 0,
        "info": "文本转向量完成",
        "message": "文本转向量完成",
        "data": {"text": text, "vector": vector},
    }


@router.post("/api/component/nl2cypher/text-to-vectors")
async def text_to_vectors_api(body: BatchTextVectorRequest) -> Dict[str, Any]:
    texts = [text.strip() for text in body.texts if text and text.strip()]
    if not texts:
        raise HTTPException(status_code=400, detail="文本列表不能为空")
    vectors = batch_text_to_vectors(texts)
    return {
        "code": 0,
        "info": f"批量文本转向量完成，共 {len(vectors)} 个向量",
        "message": f"批量文本转向量完成，共 {len(vectors)} 个向量",
        "data": {"count": len(vectors), "vectors": vectors},
    }


@router.post("/api/component/kg-update/calculate-confidence")
async def calculate_confidence_api(body: GraphRequest) -> Dict[str, Any]:
    graph = body.graph or {}
    if not graph.get("relationships") and not graph.get("ontology_relations"):
        return {
            "code": 0,
            "info": "置信度评估完成",
            "data": {
                "evaluation_id": f"EVAL-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "total_count": 0,
                "graph": {
                    "nodes": graph.get("nodes", []),
                    "relationships": graph.get("relationships", []),
                    "ontology_relations": graph.get("ontology_relations", []),
                },
                "details": {},
            },
        }
    return calculate_graph_confidence(body.model_dump())


@router.post("/api/knowledge/precipitation")
async def knowledge_precipitation(
    # ===== 文件与基础参数 =====
    file: UploadFile = File(..., description="待解析的原始文档文件"),
    lang: str = Form("ch", description="文件语言，默认 ch，可选 en"),
    parser_engine: Optional[str] = Form(None, description="解析引擎: mineru / docling，不传则自动选择"),
    
    # ===== 切片参数 =====
    chunk_character: Optional[str] = Form(None, description="切分符号，JSON数组格式"),
    chunk_size: int = Form(500, description="切片大小，默认500"),
    chunk_type: int = Form(0, description="切片类型：0语义/1符号/2递归/3Markdown，默认0"),
    similarity: float = Form(0.6, description="语义切分相似度阈值，默认0.6"),
    
    # ===== 知识抽取参数 =====
    ontology: Optional[str] = Form(None, description="PlantUML 本体定义"),
    save_to_db: bool = Form(False, description="是否将抽取结果存入 Neo4j"),
    model: Optional[str] = Form(None, description="LLM 模型名"),
    base_url: Optional[str] = Form(None, description="LLM API 地址"),
    
    # ===== 文件存储参数 =====
    knowledge_space_id: Optional[str] = Form(None, description="知识空间ID"),
    file_service_url: Optional[str] = Form(None, description="文件存储服务地址"),
) -> JSONResponse:
    """
    知识沉淀统一接口
    
    逻辑链：
    1. 文档解析 → 得到 MD 内容 → 存储原始 MD (origin_url)
    2. 知识抽取 → 得到 graph → 存储抽取结果 (extract_url)
    3. 文本切片 → 得到 chunks → 存储切片结果（注：当前版本未单独存储切片结果）
    4. 向量化 → 得到 vectors → 存储向量结果 (vector_url)
    """
    start_time = datetime.now()
    stats = {"document": {}, "extraction": {}, "chunking": {}, "upload": {}}
    
    temp_file_path = None
    file_name_stem = Path(file.filename).stem
    file_service = file_service_url or os.getenv("FILE_SERVICE_URL", "").strip()
    
    try:
        # ==================== 1. 保存上传文件 ====================
        suffix = Path(file.filename).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            temp_file_path = tmp.name
        
        stats["document"]["file_name"] = file.filename
        stats["document"]["file_size"] = len(content)
        stats["document"]["file_type"] = suffix.lstrip('.')

        # ==================== 2. 文档解析 ====================
        logger.info(f"文档解析，引擎: {parser_engine or '自动选择'}")
        processor = DocumentProcessor()
        parse_result = processor.process_file(temp_file_path, lang=lang, model_name=parser_engine)
        
        if not parse_result.get("success"):
            return JSONResponse(status_code=200, content={
                "code": 1001, "info": "文档解析失败",
                "data": {
                    "name": None,
                    "understanding": f"文档解析失败：{parse_result.get('error', '未知错误')}",
                    "origin_url": None,
                    "vector_url": None,
                    "extract_url": None,
                    "graphs": None
                }
            })
        
        md_content = normalize_document_content(parse_result.get("content", ""))
        stats["document"]["content_length"] = len(md_content)

        # ==================== 3. 存储原始MD (origin_url) ====================
        origin_upload = upload_md_to_file_service(
            md_content, 
            f"{file_name_stem}.md", 
            file_service, 
            knowledge_space_id
        )
        origin_url = origin_upload.get("file_url") if origin_upload.get("success") else None
        stats["upload"]["origin"] = origin_upload

        # ==================== 4. 知识抽取 ====================
        plain_text = md_to_text(md_content)
        ontology_text = (ontology or os.getenv("KG_EXTRACT_ONTOLOGY") or DEFAULT_ONTOLOGY).strip()
        extract_result = extract_v2(
            ontology=ontology_text,
            text=plain_text, 
            model=model, 
            base_url=base_url
        )
        
        extract_error = extract_result.get("error") or extract_result.get("_error")
        if extract_error:
            return JSONResponse(status_code=200, content={
                "code": 1002, "info": "知识抽取失败",
                "data": {
                    "name": None,
                    "understanding": f"知识抽取失败：{extract_error}",
                    "origin_url": origin_url,
                    "vector_url": None,
                    "extract_url": None,
                    "graphs": None
                }
            })
        
        graph = extract_result.get("graph", {"nodes": [], "relationships": []})
        stats["extraction"]["nodes_extracted"] = len(graph.get("nodes", []))
        stats["extraction"]["relationships_extracted"] = len(graph.get("relationships", []))
        if save_to_db:
            stats["extraction"]["storage"] = store_extraction_to_neo4j(graph=graph)

        # ==================== 5. 存储抽取结果 (extract_url) ====================
        extract_md = f"# 知识抽取结果\n\n"
        extract_md += f"节点数: {stats['extraction']['nodes_extracted']}\n"
        extract_md += f"关系数: {stats['extraction']['relationships_extracted']}\n\n"
        extract_md += "## 节点列表:\n\n"
        for node in graph.get("nodes", [])[:20]:
            name = node.get("properties", {}).get("name", node.get("uid", ""))
            extract_md += f"- **{node.get('label', 'Unknown')}**: {name}\n"
        
        extract_upload = upload_md_to_file_service(
            extract_md, 
            f"{file_name_stem}_extract.md", 
            file_service, 
            knowledge_space_id
        )
        extract_url = extract_upload.get("file_url") if extract_upload.get("success") else None
        stats["upload"]["extract"] = extract_upload

        # ==================== 6. 文本切片 ====================
        chunk_chars = parse_chunk_character(chunk_character)
        chunks = chunk_document(
            md_content, 
            chunk_type, 
            chunk_size, 
            chunk_chars, 
            similarity
        )
        stats["chunking"]["chunk_count"] = len(chunks)
        stats["chunking"]["chunk_type"] = chunk_type
        logger.info(f"切片完成，类型: {chunk_type}, 数量: {len(chunks)}")

        # ==================== 7. 向量化 ====================
        vectors = batch_text_to_vectors(chunks)
        stats["chunking"]["vector_dimension"] = len(vectors[0]) if vectors else 0

        # ==================== 8. 存储向量结果 (vector_url) ====================
        vector_md = f"# 向量化结果\n\n"
        vector_md += f"向量数量: {len(vectors)}\n"
        vector_md += f"向量维度: {stats['chunking']['vector_dimension']}"
        vector_upload = upload_md_to_file_service(
            vector_md, 
            f"{file_name_stem}_vector.md", 
            file_service, 
            knowledge_space_id
        )
        vector_url = vector_upload.get("file_url") if vector_upload.get("success") else None
        stats["upload"]["vector"] = vector_upload

        # ==================== 9. 构建响应 ====================
        stats["total_time_seconds"] = round((datetime.now() - start_time).total_seconds(), 2)

        return JSONResponse(status_code=200, content={
            "code": 0,
            "info": "success",
            "data": {
                "name": file_name_stem,
                "understanding": generate_understanding(graph),
                "origin_url": origin_url,
                "vector_url": vector_url,
                "extract_url": extract_url,
                "graphs": build_graphs_response(graph),
                "stats": stats,
            }
        })
    
    except Exception as e:
        logger.exception("知识沉淀处理失败")
        return JSONResponse(status_code=500, content={
            "code": 9999,
            "info": f"系统异常: {str(e)}",
            "data": {
                "name": None,
                "understanding": f"处理失败：{str(e)}",
                "origin_url": None,
                "vector_url": None,
                "extract_url": None,
                "graphs": None
            }
        })
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.unlink(temp_file_path)
            except Exception:
                pass
