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
    class DocumentProcessor:
        def process_file(self, *_args, **_kwargs):
            return {"success": True, "content": "# 测试文档\n测试内容", "metadata": {"model_used": "pandas"}}

    def store_artifact(_content, file_name, *_args, **_kwargs):
        return {"success": True, "file_url": f"/knowledge/{file_name}"}

    stubs = {
        "algorithm.NL_to_cypher.vector": _module(
            "algorithm.NL_to_cypher.vector",
            batch_text_to_vectors=lambda texts: [[float(index), 1.0] for index, _ in enumerate(texts)],
        ),
        "algorithm.knowledge_extract": _module(
            "algorithm.knowledge_extract",
            extract_v2=lambda **_kwargs: {"raw": {}, "graph": {"nodes": [], "relationships": []}},
            store_extraction_to_neo4j=lambda **_kwargs: {"success": True},
        ),
        "algorithm.knowledge_pipeline": _module(
            "algorithm.knowledge_pipeline",
            build_graphs_response=lambda graph: {"graphs": [{"graph": {"index": "0", **graph}}]} if graph.get("nodes") else {"graphs": []},
            chunk_document=lambda content, *_args, **_kwargs: [content],
            generate_understanding=lambda graph: f"节点数: {len(graph.get('nodes', []))}",
            md_to_text=lambda text: text,
            normalize_document_content=lambda content: str(content or ""),
            parse_chunk_characters=lambda _raw: ["。", "\n"],
            store_artifact=store_artifact,
        ),
        "algorithm.knowledge_pipeline.artifacts": _module(
            "algorithm.knowledge_pipeline.artifacts",
            resolve_local_artifact=lambda *_args: None,
        ),
        "algorithm.preproc.preproc_src.main_processor": _module(
            "algorithm.preproc.preproc_src.main_processor",
            DocumentProcessor=DocumentProcessor,
        ),
        "algorithm.confidence_calculate.calculator": _module(
            "algorithm.confidence_calculate.calculator",
            calculate_graph_confidence=lambda payload: {
                "code": 0,
                "message": "置信度评估完成",
                "info": "置信度评估完成",
                "data": {"evaluation_id": "EVAL-TEST", "total_count": 1, "graph": payload["graph"], "details": {}},
                "error": None,
            },
            close_confidence_resources=lambda: None,
        ),
    }
    original = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        from fastapi import FastAPI
        from backend.app.routers.confidence import router as confidence_router
        from backend.app.routers.knowledge_precipitation import router as knowledge_router
        from backend.app.routers.vectorization import router as vector_router

        app = FastAPI()
        app.include_router(knowledge_router)
        app.include_router(vector_router)
        app.include_router(confidence_router)
        yield types.SimpleNamespace(app=app)
    finally:
        for name, previous in original.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
