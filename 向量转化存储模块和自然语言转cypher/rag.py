from typing import Optional

import torch
import re

import requests
from langchain_community.document_loaders import TextLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate
from langchain_chroma import Chroma
from transformers import AutoTokenizer, AutoModel
from langchain.embeddings.base import Embeddings
from langchain_experimental.text_splitter import SemanticChunker
# from langchain.memory import ConversationBufferMemory #处理多轮对话的记忆功能

OLLAMA_URL = "http://192.168.5.180:11434/api/generate"
MODEL_NAME = "deepseek-r1:32b"  # 使用的模型名称

# 修改：使用Chroma本地存储路径
CHROMA_DB_PATH = "./chroma_db"

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

# 模型选取
def call_llm(prompt: str) -> str:
    """调用Ollama API"""
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=500)
        response.raise_for_status()
        result = response.json()
        result=result.get("response", "").strip()
        result=extract_final_response(result)
        return result
    except Exception as e:
        print(f"LLM API调用错误: {e}")
        return ""


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
    for split_doc in split_docs:
        print(split_doc)
        print("#"*10)
    return split_docs


# 文本嵌入+向量存储
def embedding(split_docs: list):
    model_path = "models/bge-small-zh-v1.5"
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
    model_path = "models/bge-small-zh-v1.5"
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
    answer = call_llm(final_prompt)

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


def extract_final_response(response_text):
    if "<think>" in response_text and "</think>" in response_text:
        # 分割思考部分和回答部分
        parts = response_text.split("</think>", 1)
        if len(parts) > 1:
            final_response = parts[1].strip()
            # 清理开头可能的多余换行
            final_response = re.sub(r'^\n+', '', final_response)
            return final_response
        else:
            return response_text
    return response_text


def main():
    documents = load_txt("列车着火应急处置_extracted.txt")
    # RecursiveCharacterTextSplitter
    # split_docs = txt_split(documents)

    #SemanticChunker
    split_docs = txt_split_semantic(documents)

    # embedding(split_docs)
    # interactive_qa()

if __name__ == "__main__":
    main()
