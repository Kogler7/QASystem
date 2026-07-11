from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@pytest.fixture(scope="session")
def protocol_module():
    class Document:
        def __init__(self, page_content: str):
            self.page_content = page_content

    class DocumentProcessor:
        def process_file(self, *_args, **_kwargs):
            return {"success": True, "content": "# 测试文档\n测试内容"}

    document_processor_module = _module(
        "algorithm.preproc.preproc_src.main_processor", DocumentProcessor=DocumentProcessor
    )
    knowledge_extract_module = _module(
        "algorithm.knowledge_extract",
        extract_v2=lambda **_kwargs: {
            "graph": {"nodes": [], "relationships": []}
        },
        store_extraction_to_neo4j=lambda **_kwargs: {"success": True},
    )
    vector_module = _module(
        "algorithm.NL_to_cypher.vector",
        batch_text_to_vectors=lambda texts: [[float(index)] for index, _ in enumerate(texts)],
        text_to_vector=lambda _text: [0.1, 0.2],
    )
    splitter_module = _module(
        "algorithm.NL_to_cypher.splitter",
        split_by_character=lambda text, **_kwargs: [text],
        split_recursively=lambda text, **_kwargs: [text],
        split_by_markdown=lambda text, **_kwargs: [text],
    )
    calculator_module = _module(
        "algorithm.confidence_calculate.calculator",
        calculate_graph_confidence=lambda payload: {
            "code": 0,
            "info": "置信度评估完成",
            "data": {
                "evaluation_id": "EVAL-TEST",
                "total_count": len(payload["graph"].get("relationships", [])),
                "graph": payload["graph"],
                "details": {},
            },
        },
    )
    stubs = {
        "langchain_core": _module("langchain_core"),
        "langchain_core.documents": _module("langchain_core.documents", Document=Document),
        "preproc_src": _module("preproc_src"),
        "preproc_src.main_processor": document_processor_module,
        "algorithm.preproc.preproc_src.main_processor": document_processor_module,
        "knowledge_extract": knowledge_extract_module,
        "algorithm.knowledge_extract": knowledge_extract_module,
        "NL_to_cypher": _module("NL_to_cypher"),
        "NL_to_cypher.vector": vector_module,
        "algorithm.NL_to_cypher.vector": vector_module,
        "NL_to_cypher.splitter": splitter_module,
        "algorithm.NL_to_cypher.splitter": splitter_module,
        "calculator": calculator_module,
        "algorithm.confidence_calculate.calculator": calculator_module,
    }

    original_modules = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        import algorithm.knowledge_precipitation as protocol
        from fastapi import FastAPI

        protocol.app = FastAPI()
        protocol.app.include_router(protocol.router)

        yield protocol
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
