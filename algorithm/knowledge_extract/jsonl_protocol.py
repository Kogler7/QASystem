"""Compatibility imports for the shared JSONL protocol."""
from external_services.llm.jsonl import (
    collection_name, record_schema, jsonl_instruction, messages_for_jsonl,
    decode_jsonl, load_jsonl_response,
)

__all__ = ["collection_name", "record_schema", "jsonl_instruction", "messages_for_jsonl",
           "decode_jsonl", "load_jsonl_response"]
