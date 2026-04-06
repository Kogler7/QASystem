import os
import re
import json
from typing import Optional, List, Dict, Any
from pymilvus import connections, MilvusClient, FieldSchema, DataType, CollectionSchema, Collection, MilvusException

import torch
import requests
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate
from transformers import AutoTokenizer, AutoModel
from langchain.embeddings.base import Embeddings
from langchain_experimental.text_splitter import SemanticChunker
from langchain_community.vectorstores import Milvus
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

MILVUS_DB_PATH="http://localhost:19530"
MODEL_PATH = _root / "NL_to_cypher" / "bge-small-zh-v1.5"

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

#语义切分
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
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
    model = AutoModel.from_pretrained(MODEL_PATH, local_files_only=True)
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
    try:
        tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
        model = AutoModel.from_pretrained(MODEL_PATH, local_files_only=True)
        custom_embedding = CustomHuggingFaceEmbeddings(model, tokenizer)
        print("模型加载成功")
        # 存储到milvus数据库
        vector_store = Milvus.from_documents(
                documents=split_docs,
                embedding=custom_embedding,
                connection_args={
                    "host": "localhost",
                    "port": "19530"
                },
                collection_name="vector_info",  # 集合名称
                drop_old=False,  # 是否覆盖现有集合
            )
        print("向量存储成功")
        return vector_store
    except Exception as e:
        print(f"向量存储失败: {e}")
        import traceback
        traceback.print_exc()
        raise
#加载数据
def load_vector_store():
    try:
        """加载已存在的向量数据库"""
        
        tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
        model = AutoModel.from_pretrained(MODEL_PATH, local_files_only=True)
        custom_embedding = CustomHuggingFaceEmbeddings(model, tokenizer)
        collection_name="vector_info"
        connections.connect(host="localhost", port="19530")
        print(f"连接到 Milvus")
        
        # 2. 加载集合
        collection = Collection(collection_name)
        collection.load()
        print(f"加载集合: {collection_name}")
        print(f"数据量: {collection.num_entities} 条")
        
        # 3. 查询所有数据
        all_data = collection.query(
            expr="",  # 空表达式 = 获取所有
            output_fields=["*"],  # 所有字段
            limit=collection.num_entities  # 获取全部
        )
        parsed_data = []
        for item in all_data:
            parsed_item = {}
            for key, value in item.items():
                if isinstance(value, str) and value.startswith('{') and value.endswith('}'):
                    try:
                        parsed_item[key] = json.loads(value)
                    except:
                        parsed_item[key] = value
                else:
                    parsed_item[key] = value
            parsed_data.append(parsed_item)
        
        connections.disconnect("default")
        print("Milvus 向量数据库加载成功")

        return parsed_data
    except Exception as e:
        print(f"加载 Milvus 向量数据库失败: {e}")
        raise
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

def get_vector_by_database_id(database_id: str) -> Optional[Dict[str, Any]]:
    all_data = load_vector_store()
    if not all_data:
        print("向量数据库加载失败或为空")
        return None
    
    # 查找指定数据库ID
    target_id = str(database_id).strip()
    
    for i, item in enumerate(all_data):
        # 检查各种可能的ID字段
        item_id = item.get('pk') or item.get('id') or item.get('_id')
        
        if item_id is not None and str(item_id) == target_id:
            # 获取文本内容
            text_content = item.get('text', '')
            if isinstance(text_content, dict):
                # 如果text是字典，提取实际文本
                text_content = text_content.get('text', '') or str(text_content)
            
            # 构建返回结果
            result = {
                'database_id': item_id,
                'index': i,
                'content': text_content,
                'source': item.get('source', ''),
            }
            return result
    
    print(f"未找到数据库ID为 '{database_id}' 的向量片段")
    return None

#根据多个数据库ID返回向量片段（实际用）
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
#测试函数 暂时从json文件中读取内容
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
                results.append(vector_content)
    
    print(f"找到 {len(results)} 个有VectorAddress的节点")
    return results

# extract_final_response 已由 llm_client 提供


def main():
    # nodes = load_json_nodes("/home/admin_user_5/wangshuo/copy/algorithm/NL_to_cypher/records.json")
    # results = get_vectors_for_nodes(nodes)
    # for result in results:
    #     print(f"节点 {result['node_id']} ({result['node_name']})")
    #     print(f"标签: {', '.join(result['labels'])}")
    #     print(f"向量地址: {result['vector_address']}")
    #     print(f"切片编号: {result['slice_number']}")
    #     print(f"向量内容: {result['vector_content'][:200]}...")
    #     print("-" * 50)
    
    # print(f"\n共处理 {len(results)} 个节点")


    # results = get_vectors_for_nodes(nodes)

    documents = load_txt("列车着火应急处置_extracted.txt")
    # RecursiveCharacterTextSplitter
    split_docs = txt_split(documents)
    print(split_docs)

    # #SemanticChunker
    # # split_docs = txt_split_semantic(documents)

    # embedding(split_docs)
    # vector = get_vector_by_database_id("464971672050206437")
    # print(vector)
    
        

if __name__ == "__main__":
    main()
