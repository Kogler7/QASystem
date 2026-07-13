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


def split_by_characters(
    documents: list[Document],
    chunk_size: int = 500,
    separators: list[str] | None = None,
) -> list[Document]:
    """Split documents through every configured separator, then pack chunks."""
    active_separators = [separator for separator in (separators or ["\n"]) if separator]
    if not active_separators:
        active_separators = ["\n"]

    fragments: list[Document] = []
    for document in documents:
        texts = [document.page_content]
        for separator in active_separators:
            next_texts: list[str] = []
            for text in texts:
                parts = text.split(separator)
                next_texts.extend(
                    part + (separator if index < len(parts) - 1 else "")
                    for index, part in enumerate(parts)
                    if part or index < len(parts) - 1
                )
            texts = next_texts
        fragments.extend(
            Document(page_content=text, metadata=dict(document.metadata))
            for text in texts
            if text
        )

    chunks: list[Document] = []
    for fragment in fragments:
        if len(fragment.page_content) <= chunk_size:
            if chunks and len(chunks[-1].page_content) + len(fragment.page_content) <= chunk_size:
                chunks[-1].page_content += fragment.page_content
            else:
                chunks.append(fragment)
            continue
        chunks.extend(
            Document(
                page_content=fragment.page_content[start:start + chunk_size],
                metadata=dict(fragment.metadata),
            )
            for start in range(0, len(fragment.page_content), chunk_size)
        )
    return chunks


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
