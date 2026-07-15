"""Compatibility exports for the former monolithic knowledge protocol module."""

from algorithm.knowledge_pipeline import (
    build_graphs_response,
    chunk_document,
    generate_understanding,
    md_to_text,
    normalize_document_content,
    parse_chunk_characters,
    store_vectors,
    upload_file,
)

__all__ = [
    "build_graphs_response",
    "chunk_document",
    "generate_understanding",
    "md_to_text",
    "normalize_document_content",
    "parse_chunk_characters",
    "store_vectors",
    "upload_file",
]
