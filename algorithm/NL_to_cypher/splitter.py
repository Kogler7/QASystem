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


def split_by_character(documents: List[Document]) -> List[Document]:
    """单个字符分割器"""
    text_splitter = CharacterTextSplitter(
        separator="\n",  # 空字符串表示按字符分割
        chunk_size=200,  # 文本块最大字符数
        chunk_overlap=50,  # 块之间的重叠字数
    )
    split_docs = text_splitter.split_documents(documents)
    for i, split_doc in enumerate(split_docs):
        print(f"分块 {i + 1}:")
        print(f"内容: {split_doc.page_content[:100]}...")
        print(f"长度: {len(split_doc.page_content)} 字符")
        print("#" * 30)
    return split_docs


def split_recursively(documents: List[Document]) -> List[Document]:
    """递归字符列表分割器"""
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=200,  # 文本块最大字符数
        chunk_overlap=50,  # 块之间的重叠字数
        separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]
    )
    split_docs = text_splitter.split_documents(documents)
    for i, split_doc in enumerate(split_docs):
        print(f"分块 {i + 1}:")
        print(f"内容: {split_doc.page_content[:100]}...")
        print(f"长度: {len(split_doc.page_content)} 字符")
        print("#" * 30)
    return split_docs


def split_by_markdown(documents: List[Document]) -> List[Document]:
    """Markdown标题分割器"""
    text_splitter = MarkdownTextSplitter(
        chunk_size=200,  # 文本块最大字符数
        chunk_overlap=50,  # 块之间的重叠字数
    )
    split_docs = text_splitter.split_documents(documents)
    for i, split_doc in enumerate(split_docs):
        print(f"分块 {i + 1}:")
        print(f"内容: {split_doc.page_content[:100]}...")
        print(f"长度: {len(split_doc.page_content)} 字符")
        print("#" * 30)
    return split_docs


def split_by_language_python(documents: List[Document]) -> List[Document]:
    """Python代码分割器"""
    text_splitter = RecursiveCharacterTextSplitter.from_language(
        language=Language.PYTHON,
        chunk_size=200,  # 文本块最大字符数
        chunk_overlap=50,  # 块之间的重叠字数
    )
    split_docs = text_splitter.split_documents(documents)
    for i, split_doc in enumerate(split_docs):
        print(f"分块 {i + 1}:")
        print(f"内容: {split_doc.page_content[:100]}...")
        print(f"长度: {len(split_doc.page_content)} 字符")
        print("#" * 30)
    return split_docs


# def split_by_tokens(documents: List[Document]) -> List[Document]:
#     """Token编码分割器"""
#     # 使用tiktoken编码器
#     text_splitter = TokenTextSplitter(
#         encoding_name="cl100k_base",  # GPT-4/3.5-turbo 使用的编码
#         chunk_size=200,  # 文本块最大Token数
#         chunk_overlap=50,  # 块之间的重叠Token数
#     )
#     split_docs = text_splitter.split_documents(documents)
#     for i, split_doc in enumerate(split_docs):
#         print(f"分块 {i + 1}:")
#         print(f"内容: {split_doc.page_content[:100]}...")
#         # 计算token数
#         encoding = tiktoken.get_encoding("cl100k_base")
#         token_count = len(encoding.encode(split_doc.page_content))
#         print(f"长度: {token_count} tokens, {len(split_doc.page_content)} 字符")
#         print("#" * 30)
#     return split_docs
#
#
# def split_by_sentence_transformer(documents: List[Document]) -> List[Document]:
#     """Sentence Transformer分割器 - 替代实现"""
#     try:
#         from sentence_transformers import SentenceTransformer
#         from langchain.text_splitter import SentenceTransformersTokenTextSplitter
#         text_splitter = SentenceTransformersTokenTextSplitter(
#             model_name="sentence-transformers/all-mpnet-base-v2",
#             chunk_size=200,  # 文本块最大Token数
#             chunk_overlap=50,  # 块之间的重叠Token数
#         )
#         split_docs = text_splitter.split_documents(documents)
#         for i, split_doc in enumerate(split_docs):
#             print(f"分块 {i + 1}:")
#             print(f"内容: {split_doc.page_content[:100]}...")
#             print(f"长度: {len(split_doc.page_content)} 字符")
#             print("#" * 30)
#         return split_docs
#     except ImportError:
#         print("需要安装 sentence-transformers: pip install sentence-transformers")
#         return documents
#
#
# def split_by_nltk(documents: List[Document]) -> List[Document]:
#     """NLTK句子分割器"""
#     # 注意：langchain的NLTKTextSplitter可能需要单独安装
#     try:
#         # 确保nltk数据已下载
#         import nltk
#         nltk.download('punkt', quiet=True)
#
#         text_splitter = NLTKTextSplitter(
#             separator="\n",  # 按句子分割
#             chunk_size=200,  # 文本块最大字符数
#             chunk_overlap=50,  # 块之间的重叠字数
#         )
#         split_docs = text_splitter.split_documents(documents)
#         for i, split_doc in enumerate(split_docs):
#             print(f"分块 {i + 1}:")
#             print(f"内容: {split_doc.page_content[:100]}...")
#             print(f"长度: {len(split_doc.page_content)} 字符")
#             print("#" * 30)
#         return split_docs
#     except ImportError as e:
#         print(f"NLTK分割器错误: {e}")
#         print("需要安装: pip install nltk")
#         return documents
#
#
# def split_by_spacy(documents: List[Document]) -> List[Document]:
#     """Spacy分割器"""
#     try:
#         import spacy
#         # 确保模型已下载
#         try:
#             nlp = spacy.load("zh_core_web_sm" if spacy.util.is_package("zh_core_web_sm") else "en_core_web_sm")
#         except OSError:
#             print("请先下载spacy模型: python -m spacy download en_core_web_sm")
#             return documents
#
#         text_splitter = SpacyTextSplitter(
#             separator="\n",  # 按句子分割
#             pipeline="zh_core_web_sm" if spacy.util.is_package("zh_core_web_sm") else "en_core_web_sm",
#             chunk_size=200,  # 文本块最大字符数
#             chunk_overlap=50,  # 块之间的重叠字数
#         )
#         split_docs = text_splitter.split_documents(documents)
#         for i, split_doc in enumerate(split_docs):
#             print(f"分块 {i + 1}:")
#             print(f"内容: {split_doc.page_content[:100]}...")
#             print(f"长度: {len(split_doc.page_content)} 字符")
#             print("#" * 30)
#         return split_docs
#     except ImportError as e:
#         print(f"Spacy分割器错误: {e}")
#         print("需要安装: pip install spacy")
#         return documents


# 完整可执行的测试代码
if __name__ == "__main__":
    # 创建测试文档
    test_docs = [
        Document(
            page_content="""这是一个测试文档。包含多行文本。
第一段结束。

这是第二段。有多个句子。看这里！这是问句吗？
Python代码示例：
def hello_world():
    \"\"\"打印Hello World\"\"\"
    print("Hello, World!")
    return True

最后一段结束。""",
            metadata={"source": "test1.txt"}
        )
    ]

    print("=" * 60)
    print("=== Character 分割器 ===")
    result1 = split_by_character(test_docs)

    print("\n" + "=" * 60)
    print("=== Recursive 分割器 ===")
    result2 = split_recursively(test_docs)

    print("\n" + "=" * 60)
    print("=== Markdown 分割器 ===")
    result3 = split_by_markdown(test_docs)

    print("\n" + "=" * 60)
    print("=== Python语言分割器 ===")
    result4 = split_by_language_python(test_docs)
    #
    # print("\n" + "=" * 60)
    # print("=== Token 分割器 ===")
    # result5 = split_by_tokens(test_docs)
    #
    # print("\n" + "=" * 60)
    # print("=== SentenceTransformer 分割器 ===")
    # result6 = split_by_sentence_transformer(test_docs)
    #
    # print("\n" + "=" * 60)
    # print("=== NLTK 分割器 ===")
    # result7 = split_by_nltk(test_docs)
    #
    # print("\n" + "=" * 60)
    # print("=== Spacy 分割器 ===")
    # result8 = split_by_spacy(test_docs)
    #
    # # 打印统计信息
    # print("\n" + "=" * 60)
    # print("分割结果统计:")
    # print(f"Character分割: {len(result1)} 个分块")
    # print(f"Recursive分割: {len(result2)} 个分块")
    # print(f"Markdown分割: {len(result3)} 个分块")
    # print(f"Python语言分割: {len(result4)} 个分块")
    # print(f"Token分割: {len(result5)} 个分块")