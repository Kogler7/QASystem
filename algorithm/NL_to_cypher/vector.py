import json
import requests
from typing import List, Dict, Any, Optional
from pathlib import Path

import torch
from pymilvus import connections, Collection, FieldSchema, DataType, CollectionSchema
from langchain_core.documents import Document
from langchain_experimental.text_splitter import SemanticChunker
from langchain.embeddings.base import Embeddings
from transformers import AutoTokenizer, AutoModel
from neo4j import GraphDatabase
import time

from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
    CharacterTextSplitter,
    MarkdownTextSplitter,
    Language,
    TokenTextSplitter,
    NLTKTextSplitter,
    SpacyTextSplitter
)
from langchain_core.documents import Document
# import tiktoken
from typing import List

# 导入统一的LLM客户端
import sys
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
from common.llm_client import complete as call_llm


# ==================== 配置 ====================
MILVUS_HOST = "localhost"
MILVUS_PORT = "19530"
MILVUS_COLLECTION = "knowledge_nodes"
MODEL_PATH = str(_root / "NL_to_cypher" / "bge-small-zh-v1.5")

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "neo4j2025"



# ==================== 嵌入模型 ====================

class HuggingFaceEmbeddings(Embeddings):
    """HuggingFace嵌入模型"""
    def __init__(self, model_path: str):
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        self.model = AutoModel.from_pretrained(model_path, local_files_only=True)
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        inputs = self.tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)
        with torch.no_grad():
            outputs = self.model(**inputs)
        embeddings = outputs.last_hidden_state.mean(dim=1)
        return embeddings.tolist()
    
    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]

# ==================== 语义切片 ====================
def load_markdown_file(filepath: str) -> str:
    """
    从Markdown文件加载文本内容
    
    Args:
        filepath: Markdown文件路径
    
    Returns:
        文件中的纯文本内容
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    print(f"从文件加载成功: {filepath}")
    print(f"文件长度: {len(content)} 字符")
    return content
# ==================== 语义切片 ====================

def semantic_chunking(text: str,chunk_size:int,similarity:float=0.6, model_path: Optional[str] = MODEL_PATH) -> List[str]:
    """
    语义切片 - 使用bge-small-zh-v1.5模型
    
    原理：先用正则切分成句子，然后计算相邻句子的语义相似度，
          相似度高的合并，低的断开
    
    Args:
        text: 待切片的文本
        model_path: 嵌入模型路径
    
    Returns:
        切片后的文本列表
    """
    import re
    import numpy as np
    from sklearn.metrics.pairwise import cosine_similarity
    
    embeddings = HuggingFaceEmbeddings(model_path)
    
    # 正则切分
    sentences = re.split(r"(?<=[。！？!?])\s*", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    print(f"初始句子数: {len(sentences)}")
    
    if len(sentences) <= 1:
        return sentences
    
    # 计算每个句子的向量
    vectors = embeddings.embed_documents(sentences)
    
    # 3. 计算相邻句子的余弦相似度
    similarities = []
    for i in range(len(vectors) - 1):
        sim = cosine_similarity([vectors[i]], [vectors[i+1]])[0][0]
        similarities.append(sim)
    
    # 4.语义融合，相似度低于阈值则切分
    threshold = similarity
    split_points = [0]
    for i, sim in enumerate(similarities):
        if sim < threshold:
            split_points.append(i + 1)
    
    chunks = []
    for i in range(len(split_points)):
        start = split_points[i]
        if i + 1 < len(split_points):
            end = split_points[i + 1]
        else:
            end = len(sentences)
        
        chunk = "".join(sentences[start:end])
        chunks.append(chunk)
    
    print(f"语义切片完成，共 {len(chunks)} 个片段（阈值={threshold}）")
    for i, chunk in enumerate(chunks):
        print(f"  片段 {i+1}: {chunk}")
    
    return chunks
# ==================== 文本转向量 ====================

def text_to_vector(text: str, model_path: Optional[str] = MODEL_PATH) -> List[float]:
    """
    将单段文本转换为向量
    
    Args:
        text: 输入文本
        model_path: 嵌入模型路径
    
    Returns:
        向量表示
    """
    embeddings = HuggingFaceEmbeddings(model_path)
    vector = embeddings.embed_query(text)
    print(f"文本转向量完成")
    return vector


def batch_text_to_vectors(texts: List[str], model_path: Optional[str] = MODEL_PATH) -> List[List[float]]:
    """
    批量将文本转换为向量
    
    Args:
        texts: 文本列表
        model_path: 嵌入模型路径
    
    Returns:
        向量列表
    """
    embeddings = HuggingFaceEmbeddings(model_path)
    vectors = embeddings.embed_documents(texts)
    print(f"批量文本转向量完成，共 {len(vectors)} 个向量")
    return vectors


# ==================== 向量存入向量数据库 ====================

def store_vectors_to_milvus(
    texts: List[str],
    vectors: List[List[float]],
    collection_name: str = MILVUS_COLLECTION
) -> List[int]:
    """
    将向量存入Milvus向量数据库
    
    Args:
        texts: 原始文本列表
        vectors: 对应的向量列表
        collection_name: 集合名称
    
    Returns:
        存入数据库后的ID列表
    """
    # 连接到Milvus
    connections.connect(host=MILVUS_HOST, port=MILVUS_PORT)
    
    # 检查集合是否存在，不存在则创建
    try:
        collection = Collection(collection_name)
        collection.load()
        print(f"集合 {collection_name} 已存在")
    except:
        # 创建集合
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=len(vectors[0]))
        ]
        schema = CollectionSchema(fields)
        collection = Collection(collection_name, schema)
        print(f"创建集合 {collection_name}")
    
    # 插入数据
    entities = [
        texts,
        vectors
    ]
    
    insert_result = collection.insert(entities)
    collection.flush()
    
    # 获取插入的ID
    ids = insert_result.primary_keys
    print(f"向量存入Milvus成功，共 {len(ids)} 条记录")
    print(f"生成的ID: {ids}")
    
    connections.disconnect("default")
    
    return ids


# ==================== 创建知识单元节点并连接 ====================

def create_knowledge_node(
    vector_id: int,
    text: str,
    vector: List[float],
    extra_properties: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    在Neo4j中创建知识单元节点，支持自定义属性
    
    Args:
        vector_id: 向量数据库中的ID
        text: 文本内容
        vector: 文本对应的向量
        extra_properties: 额外的知识单元逻辑属性（JSON格式），用于覆盖默认的null值
    
    Returns:
        成功返回 resourceId，失败返回 None
    """
    timestamp = int(time.time() * 1000)  # 毫秒时间戳
    resource_id = f"ku_{vector_id}"
    
    # 完整的知识单元逻辑属性，未知的填null
    properties = {
        "resourceId": resource_id,
        "resourceType": None,
        "version": None,
        "status": None,
        "permissions": None,
        "name": text[:10] if text else None,
        "creator": None,
        "createTime": timestamp,
        "modifier": None,
        "modifyTime": timestamp,
        "reviewer": None,
        "reviewTime": None,
        "validityPeriod": None,
        "archived": None,
        "archiveTime": None,
        "history": None,
        "summary": None,
        "description": None,
        "tags": [],
        "language": None,
        "knowledgeContentFormat": None,
        "knowledgeContent": text,
        "storageType": None,
        "storageLocation": "Milvus",
        "contentSize": 512,
        "knowledgeType": "向量块",
        "logicalStructure": None,
        "interfaceDefinition": None,
        "businessRules": None,
        "relatedKUs": None,
        "vectorAddress": f"{MILVUS_COLLECTION}/{vector_id}",
        "vector": vector
    }
    
    # 合并用户传入的额外属性，覆盖默认值
    if extra_properties:
        properties.update(extra_properties)
    
    # 构建属性字符串
    prop_strings = []
    for key, value in properties.items():
        if value is None:
            prop_strings.append(f"{key}: null")
        elif isinstance(value, str):
            escaped_value = value.replace("'", "\\'").replace("\n", "\\n")
            prop_strings.append(f"{key}: '{escaped_value}'")
        elif isinstance(value, bool):
            prop_strings.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, int):
            prop_strings.append(f"{key}: {value}")
        elif isinstance(value, float):
            prop_strings.append(f"{key}: {value}")
        elif isinstance(value, list):
            if value and isinstance(value[0], float):
                # 向量列表
                vec_str = ", ".join([str(v) for v in value])
                prop_strings.append(f"{key}: [{vec_str}]")
            elif value:
                list_str = ", ".join([f"'{item}'" for item in value])
                prop_strings.append(f"{key}: [{list_str}]")
            else:
                prop_strings.append(f"{key}: []")
        else:
            prop_strings.append(f"{key}: {value}")
    
    props = ", ".join(prop_strings)
    
    cypher = f"""
    CREATE (n:KnowledgeUnit {{{props}}})
    RETURN n.resourceId AS resourceId
    """
    
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            result = session.run(cypher)
            record = result.single()
            if record:
                created_resource_id = record["resourceId"]
                print(record)
                print(f"知识单元节点创建成功: resourceId={created_resource_id}")
                return created_resource_id
            else:
                print(f"知识单元节点创建失败: 无返回结果")
                return None
    except Exception as e:
        print(f"知识单元节点创建失败: {e}")
        return None
    finally:
        driver.close()


# ==================== 连接到资源空间节点 =====================
def connect_nodes(
    source_id: str,
    target_id: str,
    relationship_type: str = "PLACE_IN",
    relation_properties: Optional[Dict[str, Any]] = None
) -> bool:
    """
    连接两个知识单元节点，支持关系属性
    
    Args:
        source_id: 源节点resourceId（新创建的节点）
        target_id: 目标节点spaceId（已有的节点）
        relationship_type: 关系类型
        relation_properties: 关系的额外属性（JSON格式）
    
    Returns:
        是否成功
    """
    
    # 基础关系属性
    base_props = {
        "relationType": relationship_type
    }
    
    if relation_properties:
        base_props.update(relation_properties)
    
    # 构建关系属性字符串
    prop_strings = []
    for key, value in base_props.items():
        if isinstance(value, str):
            escaped_value = value.replace("'", "\\'")
            prop_strings.append(f"{key}: '{escaped_value}'")
        elif isinstance(value, bool):
            prop_strings.append(f"{key}: {str(value).lower()}")
        elif value is None:
            prop_strings.append(f"{key}: null")
        else:
            prop_strings.append(f"{key}: {value}")
    
    props = ", ".join(prop_strings)
    
    cypher = f"""
    MATCH (a {{resourceId: $source_id}}), (b {{spaceId: $target_id}})
    CREATE (a)-[r:{relationship_type} {{{props}}}]->(b)
    RETURN r
    """
    
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            result = session.run(
                cypher,
                source_id=source_id,
                target_id=target_id
            )
            record = result.single()
            if record:
                print(f"节点连接成功: {source_id} -> {target_id} ({relationship_type})")
                return True
            else:
                print(f"节点连接失败: 未找到节点")
                return False
    except Exception as e:
        print(f"节点连接失败: {e}")
        return False
    finally:
        driver.close()


# ==================== 主函数 ====================

def main():
    
    markdown_file = _root / "NL_to_cypher" / "列车着火应急处置_extracted.txt"
    test_text = load_markdown_file(markdown_file)
    
    # ========== 语义切片 ==========
    chunks = semantic_chunking(test_text)
    
    # ========== 文本转向量 ==========
    batch_vectors = batch_text_to_vectors(chunks)
    print(f"向量转换完成")
    
    # ========== 向量存入向量数据库 ==========
    try:
        stored_ids = store_vectors_to_milvus(chunks, batch_vectors)
        print(f"存储成功，生成的ID: {stored_ids}")
    except Exception as e:
        print(f"存储失败（可能是Milvus未启动）: {e}")
        stored_ids = []
    
    # ========== 创建知识单元节点并连接 ==========    
    if stored_ids:
        # 已知的目标节点resourceId列表（已有资源空间节点）
        target_resource_ids = ["KS-资源"]
        
        for i, chunk_id in enumerate(stored_ids):
            
            # 创建知识单元节点，获取返回的resourceId
            source_resource_id = create_knowledge_node(
                vector_id=chunk_id,
                text=chunks[i],
                vector=batch_vectors[i]
            )
            
            if source_resource_id:
                # 新创建的节点作为source，依次连接到所有目标节点
                for target_resource_id in target_resource_ids:
                    connect_nodes(
                        source_id=source_resource_id,
                        target_id=target_resource_id,
                        relationship_type="PLACE_IN"
                    )

def split_by_character(documents: List[Document],chunk_size:int=500,chunk_character:str="\n") -> List[Document]:
    """单个字符分割器"""
    text_splitter = CharacterTextSplitter(
        separator=chunk_character,  # 空字符串表示按字符分割
        chunk_size=chunk_size,  # 文本块最大字符数
        chunk_overlap=50,  # 块之间的重叠字数
    )
    split_docs = text_splitter.split_documents(documents)
    # for i, split_doc in enumerate(split_docs):
    #     print(f"分块 {i + 1}:")
    #     print(f"内容: {split_doc.page_content[:100]}...")
    #     print(f"长度: {len(split_doc.page_content)} 字符")
    #     print("#" * 30)
    return split_docs


def split_recursively(documents: List[Document],chunk_size:int=500,chunk_character:list[str]=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]) -> List[Document]:
    """递归字符列表分割器"""
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,  # 文本块最大字符数
        chunk_overlap=50,  # 块之间的重叠字数
        separators=chunk_character
    )
    split_docs = text_splitter.split_documents(documents)
    # for i, split_doc in enumerate(split_docs):
    #     print(f"分块 {i + 1}:")
    #     print(f"内容: {split_doc.page_content[:100]}...")
    #     print(f"长度: {len(split_doc.page_content)} 字符")
    #     print("#" * 30)
    return split_docs


def split_by_markdown(documents: List[Document],chunk_size:int=500) -> List[Document]:
    """Markdown标题分割器"""
    text_splitter = MarkdownTextSplitter(
        chunk_size=chunk_size,  # 文本块最大字符数
        chunk_overlap=50,  # 块之间的重叠字数
    )
    split_docs = text_splitter.split_documents(documents)
    # for i, split_doc in enumerate(split_docs):
    #     print(f"分块 {i + 1}:")
    #     print(f"内容: {split_doc.page_content[:100]}...")
    #     print(f"长度: {len(split_doc.page_content)} 字符")
    #     print("#" * 30)
    return split_docs


if __name__ == "__main__":
    main()