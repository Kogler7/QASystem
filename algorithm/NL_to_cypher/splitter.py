from langchain_core.documents import Document
from langchain_text_splitters import (
    CharacterTextSplitter,
    MarkdownTextSplitter,
    RecursiveCharacterTextSplitter,
)


def split_by_character(
    documents: list[Document],
    chunk_size: int = 500,
    separator: str = "\n",
) -> list[Document]:
    splitter = CharacterTextSplitter(
        separator=separator,
        chunk_size=chunk_size,
        chunk_overlap=0,
    )
    return splitter.split_documents(documents)


def split_recursively(
    documents: list[Document],
    chunk_size: int = 500,
    separators: list[str] | None = None,
) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(
        separators=separators or ["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
        chunk_size=chunk_size,
        chunk_overlap=0,
    )
    return splitter.split_documents(documents)


def split_by_markdown(
    documents: list[Document],
    chunk_size: int = 500,
) -> list[Document]:
    splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=0)
    return splitter.split_documents(documents)
