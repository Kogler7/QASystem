import os
import re
import json
from typing import Optional, List, Dict, Any

import torch
import requests
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate
from langchain_chroma import Chroma
from transformers import AutoTokenizer, AutoModel
from langchain.embeddings.base import Embeddings
from langchain_experimental.text_splitter import SemanticChunker
# from langchain.memory import ConversationBufferMemory #处理多轮对话的记忆功能

# 使用统一 LLM 接口（algorithm/common）
import sys
from pathlib import Path
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
from common.llm_client import complete, extract_final_response

def call_llm(prompt: str, timeout: int = 500) -> str:
    return complete(prompt, timeout=timeout)

# 修改：使用Chroma本地存储路径，可由环境变量覆盖
CHROMA_DB_PATH = os.environ.get("NL2CYPHER_CHROMA_DB_PATH", "./chroma_db")

class CustomHuggingFaceEmbeddings(Embeddings):
    def __init__(self, model, tokenizer):
        self.model = model
        self.tokenizer = tokenizer

    def embed_documents(self, texts):
        # 实现文档嵌入逻辑
        inputs = self.tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)
        with torch.no_grad():
            outputs = self.model(**inputs)
        embeddings = outputs.last_hidden_state.mean(dim=1)
        return embeddings.tolist()

    def embed_query(self, text):
        return self.embed_documents([text])[0]

# 加载数据
def load_txt(filepath: str) -> list[Document]:
    loader = TextLoader(filepath, encoding='utf-8')
    documents = loader.load()
    # print(documents)
    return documents


def txt_split_semantic(
        documents: list[Document],
        threshold: float = 0.5,
        chunk_size: Optional[int] = None,
        breakpoint_threshold_type: str = "percentile"
) -> list[Document]:
    """
    使用语义分割文本

    Args:
        documents: 输入的文档列表
        embedding_model: 嵌入模型，如果为None则使用默认
        threshold: 分割阈值，越高分割越少
        chunk_size: 可选，用于控制块大小
        breakpoint_threshold_type: 断点阈值类型，可选 "percentile", "standard_deviation", "interquartile"

    Returns:
        分割后的文档列表
    """
    model_path = "models/bge-small-zh-v1.5"
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModel.from_pretrained(model_path, local_files_only=True)
    custom_embedding = CustomHuggingFaceEmbeddings(model, tokenizer)

    # 创建语义分割器
    text_splitter = SemanticChunker(
        embeddings=custom_embedding,
        breakpoint_threshold_type=breakpoint_threshold_type,
        breakpoint_threshold_amount=threshold,
        sentence_split_regex="[。！？.!?\n\n\n]+",  # 初步句子分割
    )

    # 分割文档
    split_docs = text_splitter.split_documents(documents)

    # 如果指定了chunk_size，可以进一步合并小段
    if chunk_size:
        split_docs = merge_small_chunks(split_docs, min_size=500)
    for split_doc in split_docs:
        print(split_doc)
        print("#"*10)
    return split_docs


# 辅助函数：合并小片段
def merge_small_chunks(documents: list[Document], min_size: int =500) -> list[Document]:
    """
    合并小片段以符合最小大小要求
    """
    merged_docs = []
    current_chunk = ""
    current_metadata = {}

    for doc in documents:
        if not current_metadata:
            current_metadata = doc.metadata

        if len(current_chunk) + len(doc.page_content) <= min_size * 2:
            # 合并到当前块
            if current_chunk:
                current_chunk += "\n\n" + doc.page_content
            else:
                current_chunk = doc.page_content
        else:
            # 保存当前块
            if current_chunk:
                merged_docs.append(Document(
                    page_content=current_chunk,
                    metadata=current_metadata
                ))
            # 开始新块
            current_chunk = doc.page_content
            current_metadata = doc.metadata

    # 添加最后一个块
    if current_chunk:
        merged_docs.append(Document(
            page_content=current_chunk,
            metadata=current_metadata
        ))

    return merged_docs

# 数据切片
def txt_split(documents: list[Document]) -> list[Document]:
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=200,  #文本块最大字符数
        chunk_overlap=50,  #块之间的重叠字数
        separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?"]
    )
    split_docs = text_splitter.split_documents(documents)
    # for split_doc in split_docs:
        # print(split_doc)
        # print("#"*10)
    return split_docs


# 文本嵌入+向量存储
def embedding(split_docs: list):
    model_path = "/mnt/data3/srv/qasystem/models/bge-small-zh-v1.5"
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModel.from_pretrained(model_path, local_files_only=True)
    custom_embedding = CustomHuggingFaceEmbeddings(model, tokenizer)
    print("模型加载成功")
    # 存储到Chroma数据库
    vector_store = Chroma.from_documents(
        documents=split_docs,
        embedding=custom_embedding,
        persist_directory=CHROMA_DB_PATH,  # 指定存储路径
        collection_name="emergency_procedures"
    )
    print("向量存储成功")
    return vector_store

#加载数据
def load_vector_store():
    """加载已存在的向量数据库"""
    model_path = "/mnt/data3/srv/qasystem/models/bge-small-zh-v1.5"
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModel.from_pretrained(model_path, local_files_only=True)
    custom_embedding = CustomHuggingFaceEmbeddings(model, tokenizer)

    vector_store = Chroma(
        persist_directory=CHROMA_DB_PATH,
        embedding_function=custom_embedding,
        collection_name="emergency_procedures"
    )
    return vector_store
#构建prompt
def create_rag_prompt():
    """创建RAG提示词模板"""
    template = """基于以下背景信息，请回答用户的问题。如果信息不足，请根据你的知识回答。

    背景信息:
    {context}
    
    问题: {question}
    
    请提供准确、详细的回答:"""

    return PromptTemplate(
        template=template,
        input_variables=["context", "question"]
    )

# 检索器构造
# 回答增强
def rag_query(question: str, k=3):
    """RAG问答流程"""
    # 加载向量数据库
    vector_store = load_vector_store()
    docs_with_scores = vector_store.similarity_search_with_score(question, k=k)

    print("=" * 60)
    #print(f"问题: {question}")
    #print("=" * 60)

    if docs_with_scores:  # 判断是否有检索结果
        #print("-" * 40)

        # 过滤文档：只保留距离 <= 80 的文档
        filtered_docs = []
        for i, (doc, distance) in enumerate(docs_with_scores, 1):
            #print(f"文档 {i} (距离: {distance:.4f}):")
            # print(f"内容: {doc.page_content}")
            #print("-" * 40)

            if distance <= 60:  # 距离阈值过滤
                filtered_docs.append(doc)
            #else:
                #print(f"文档 {i} 因距离过大({distance:.4f} > 80)被过滤")

        docs = filtered_docs
        print(f"经过过滤后保留 {len(docs)}个相关文档")

    else:
        print("未检索到相关文档")
        docs = []

    # 构建上下文
    context = "\n\n".join([doc.page_content for doc in docs])

    # 构建Prompt
    prompt_template = create_rag_prompt()
    final_prompt = prompt_template.format(context=context, question=question)

    # 调用LLM生成回答
    answer = call_llm(final_prompt, timeout=500)

    return answer


def interactive_qa(): #多轮问答
    """交互式问答系统"""
    print("RAG系统已启动！输入'退出'或'quit'结束程序。")
    print("=" * 50)

    while True:
        question = input("\n请输入您的问题: ").strip()

        if question.lower() in ['退出', 'quit', 'exit']:
            break

        if not question:
            continue

        try:
            answer = rag_query(question, k=3)
            print("=" * 50)
            print(f"回答: {answer}")
            print("=" * 50)
        except Exception as e:
            print(f"查询过程中出现错误: {e}")
def get_vector_by_index(index: int) -> Optional[Dict[str, Any]]:
    """
    根据索引返回特定的向量片段（原文本内容）
    
    Args:
        index: 索引位置（从0开始）
    
    Returns:
        包含向量片段信息的字典，如果找不到返回None
        格式: {
            'id': 向量ID,
            'index': 索引位置,
            'content': 文本内容,
            'metadata': 元数据,
            'content_length': 内容长度
        }
    """
    vector_store = load_vector_store()
    if vector_store is None:
        print("向量数据库加载失败")
        return None
    
    # 获取所有数据
    collection = vector_store.get()
    if collection is None:
        print("无法获取向量集合")
        return None
    
    # 获取各个字段
    ids = collection.get('ids', [])
    documents = collection.get('documents', [])
    metadatas = collection.get('metadatas', [])
    
    # 检查索引是否有效
    if index < 0 or index >= len(ids):
        print(f"索引 {index} 超出范围，有效范围: 0-{len(ids)-1}")
        return None
    
    # 构建返回结果
    result = {
        'id': ids[index],
        'index': index,
        'slice_number': index + 1,  # 切片编号（从1开始）
        'content': documents[index] if index < len(documents) else '',
        'metadata': metadatas[index] if index < len(metadatas) and metadatas[index] else {},
        'content_length': len(documents[index]) if index < len(documents) else 0
    }
    
    return result

# 新增函数：根据多个索引返回向量片段
def get_vectors_by_indices(indices: List[int]) -> List[Dict[str, Any]]:
    """
    根据多个索引返回向量片段
    
    Args:
        indices: 索引列表（从0开始）
    
    Returns:
        向量片段列表
    """
    results = []
    for index in indices:
        vector_data = get_vector_by_index(index)
        if vector_data:
            results.append(vector_data)
    return results
def get_vector_by_database_id(database_id: str) -> Optional[Dict[str, Any]]:
    """
    根据数据库ID返回向量片段
    
    Args:
        database_id: 数据库中的向量ID，如 "fcc262e9-2baa-4260-bf18-6ac0c75f470a"
    
    Returns:
        包含向量片段信息的字典，如果找不到返回None
    """
    vector_store = load_vector_store()
    if vector_store is None:
        print("向量数据库加载失败")
        return None
    
    # 获取所有数据
    collection = vector_store.get()
    if collection is None:
        print("无法获取向量集合")
        return None
    
    # 获取各个字段
    ids = collection.get('ids', [])
    documents = collection.get('documents', [])
    metadatas = collection.get('metadatas', [])
    
    # 查找指定数据库ID
    target_id = str(database_id).strip()
    for i, db_id in enumerate(ids):
        if str(db_id) == target_id:
            # 构建返回结果
            result = {
                'database_id': db_id,
                'index': i,
                'slice_number': i + 1,  # 切片编号（从1开始）
                'content': documents[i] if i < len(documents) else '',
                'metadata': metadatas[i] if i < len(metadatas) and metadatas[i] else {},
                'content_length': len(documents[i]) if i < len(documents) else 0
            }
            return result
    
    print(f"未找到数据库ID为 '{database_id}' 的向量片段")
    return None

# 新增函数：根据多个数据库ID返回向量片段
def get_vectors_by_database_ids(database_ids: List[str]) -> List[Dict[str, Any]]:
    """
    根据多个数据库ID返回向量片段
    
    Args:
        database_ids: 数据库ID列表
    
    Returns:
        向量片段列表
    """
    results = []
    for db_id in database_ids:
        vector_data = get_vector_by_database_id(db_id)
        if vector_data:
            results.append(vector_data)
    return results

def load_json_nodes(json_file: str) -> List[Dict[str, Any]]:
    """
    从JSON文件中加载节点数据
    
    Args:
        json_file: JSON文件路径
    
    Returns:
        节点数据列表
    """
    try:
        with open(json_file, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        
        nodes = []
        for item in data:
            if 'n' in item:
                nodes.append(item['n'])
        
        print(f"从 {json_file} 加载了 {len(nodes)} 个节点")
        return nodes
    except Exception as e:
        print(f"加载JSON文件失败: {e}")
        return []

def get_vectors_for_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    从节点数据中提取有VectorAddress的节点，并从向量数据库获取内容
    
    Args:
        nodes: 节点数据列表
    
    Returns:
        包含节点和向量内容的列表
    """
    results = []
    
    for node in nodes:
        properties = node.get('properties', {})
        
        # 检查是否有VectorAddress字段
        if 'VectorAddress' in properties:
            vector_id = properties['VectorAddress']
            
            # 从向量数据库获取内容
            vector_content = get_vector_by_database_id(vector_id)
            
            if vector_content:
                result = {
                    'node_id': node.get('identity'),
                    'element_id': node.get('elementId'),
                    'node_name': properties.get('name', ''),
                    'labels': node.get('labels', []),
                    'vector_address': vector_id,
                    'vector_content': vector_content.get('content', ''),
                    'slice_number': vector_content.get('slice_number', 0),
                    'vector_index': vector_content.get('index', 0),
                    'other_properties': {k: v for k, v in properties.items() if k != 'VectorAddress'}
                }
                results.append(result)
    
    print(f"找到 {len(results)} 个有VectorAddress的节点")
    return results

# extract_final_response 已由 llm_client 提供


def main():
    nodes = load_json_nodes("/home/admin_user_5/wangshuo/copy/algorithm/NL_to_cypher/records.json")
    results = get_vectors_for_nodes(nodes)
    for result in results:
        print(f"节点 {result['node_id']} ({result['node_name']})")
        print(f"标签: {', '.join(result['labels'])}")
        print(f"向量地址: {result['vector_address']}")
        print(f"切片编号: {result['slice_number']}")
        print(f"向量内容: {result['vector_content'][:200]}...")
        print("-" * 50)
    
    print(f"\n共处理 {len(results)} 个节点")
    # results = get_vectors_for_nodes(nodes)
    # vector_store = load_vector_store()
    # print(vector_store)
    # # documents = load_txt("列车着火应急处置_extracted.txt")
    # # RecursiveCharacterTextSplitter
    # # split_docs = txt_split(documents)

    # #SemanticChunker
    # # split_docs = txt_split_semantic(documents)

    # # embedding(split_docs)
    # # interactive_qa()
    # if vector_store is None:
    #         print("vector_store 为 None")
    #         return
        
    # print(f"Chroma 对象: {vector_store}")
        
    # # 尝试获取数据
    # print("\n尝试获取 collection...")
    # collection = vector_store.get()
        
    # if collection is None:
    #     print("collection 为 None")
    #     return
        
    # print(f"collection 类型: {type(collection)}")
    # print(f"collection 键: {collection.keys() if hasattr(collection, 'keys') else 'No keys attribute'}")
    # print("\n2. 各字段详细信息:")
    # for key in collection.keys():
    #     value = collection[key]
    #     print(f"\n=== {key} ===")
    #     print(f"类型: {type(value)}")
            
    #     if hasattr(value, '__len__'):
    #         print(f"长度: {len(value)}")
                
    #         if len(value) > 0:
    #             first_item = value[0]
    #             print(f"第一个元素类型: {type(first_item)}")
                    
    #             if key == 'documents':
    #                 print(f"内容示例: {str(first_item)[:100]}...")
    #             elif key == 'embeddings':
    #                 if isinstance(first_item, list):
    #                     print(f"向量维度: {len(first_item)}")
    #                     print(f"前5个值: {first_item[:5]}")
    #                 elif hasattr(first_item, 'shape'):
    #                     print(f"形状: {first_item.shape}")
    #                     print(f"类型: {first_item.dtype}")
    #                 else:
    #                     print(f"值: {first_item}")
    #             elif key == 'metadatas':
    #                 print(f"元数据: {first_item}")
    #             elif key == 'ids':
    #                 print(f"ID: {first_item}")
            
    #     # 特殊处理 embeddings
    #     if key == 'embeddings':
    #         print(f"\nembeddings 的具体检查:")
    #         print(f"是否为 None: {value is None}")
    #         print(f"是否为 bool: {isinstance(value, bool)}")
    #         print(f"值: {value}")
                
    # # 检查是否存在其他可能的键
    # print("\n3. 检查可能的替代键名:")
    # possible_embedding_keys = ['embeddings', 'embedding', 'vectors', 'vector']
    # for key in possible_embedding_keys:
    #     if key in collection:
    #         print(f"找到 '{key}': {type(collection[key])}, 长度: {len(collection[key]) if hasattr(collection[key], '__len__') else 'N/A'}")
    #     else:
    #         print(f"未找到 '{key}'")
        

if __name__ == "__main__":
    main()
