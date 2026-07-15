from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AGENT_ROOT = ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))


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

    def upload_file(_content, file_name, _content_type, base_url, *_args, **_kwargs):
        return {
            "success": True,
            "id": f"id-{file_name}",
            "name": file_name,
            "path": f"/storage/{file_name}",
            "url": f"/files/{file_name}",
        }

    def store_vectors(items, _base_url, _knowledge_space_id, _file_id):
        return {
            "success": True,
            "inserted_count": len(items),
            "ids": [f"vector-{position}" for position, _ in enumerate(items)],
        }

    def deduplicate_chunk_graphs(items):
        nodes = {}
        relationships = {}
        for item in items:
            for node in item["graph"].get("nodes", []):
                uid = node.get("uid")
                if uid not in nodes:
                    nodes[uid] = {**node, "properties": dict(node.get("properties") or {})}
                else:
                    nodes[uid]["properties"].update(node.get("properties") or {})
            for relation in item["graph"].get("relationships", []):
                key = (relation.get("from_uid"), relation.get("to_uid"), relation.get("type"))
                relationships[key] = relation
        return {"nodes": list(nodes.values()), "relationships": list(relationships.values())}

    stubs = {
        "algorithm.NL_to_cypher.vector": _module(
            "algorithm.NL_to_cypher.vector",
            batch_text_to_vectors=lambda texts: [[float(index), 1.0] for index, _ in enumerate(texts)],
        ),
        "algorithm.knowledge_extract": _module(
            "algorithm.knowledge_extract",
            extract_v2=lambda **_kwargs: {"raw": {}, "graph": {"nodes": [], "relationships": []}},
            extract_chunks=lambda ontology, chunks, **_kwargs: [
                {
                    "index": index,
                    "text": text,
                    "raw": {},
                    "graph": {"nodes": [], "relationships": []},
                    "attempts": [],
                    "error": None,
                    "error_code": None,
                    "raw_response": "",
                }
                for index, text in enumerate(chunks)
            ],
            deduplicate_chunk_graphs=deduplicate_chunk_graphs,
            store_extraction_to_neo4j=lambda **_kwargs: {"success": True},
        ),
        "algorithm.knowledge_extract.config": _module(
            "algorithm.knowledge_extract.config",
            get_llm_config=lambda: {"max_concurrency": 4, "qps": 0.0, "qpm": 0},
        ),
        "algorithm.knowledge_pipeline": _module(
            "algorithm.knowledge_pipeline",
            build_graphs_response=lambda items: {
                "graphs": [
                    {"graph": {"index": str(item["index"]), **item["graph"]}}
                    for item in items
                ]
            },
            chunk_document=lambda content, *_args, **_kwargs: [content],
            chunk_document_items=lambda content, *_args, **_kwargs: [
                {"text": content, "range": [0, len(content)]}
            ],
            generate_understanding=lambda graph: f"节点数: {len(graph.get('nodes', []))}",
            md_to_text=lambda text: text,
            normalize_document_content=lambda content: str(content or ""),
            parse_chunk_characters=lambda _raw: ["。", "\n"],
            upload_file=upload_file,
            store_vectors=store_vectors,
        ),
        "algorithm.preproc.preproc_src.main_processor": _module(
            "algorithm.preproc.preproc_src.main_processor",
            DocumentProcessor=DocumentProcessor,
        ),
        "algorithm.confidence_calculate.calculator": _module(
            "algorithm.confidence_calculate.calculator",
            calculate_graph_confidence=lambda payload: {
                "code": 0,
                "info": "置信度评估完成",
                "data": {
                    "evaluation_id": "EVAL-TEST",
                    "total_count": len(payload["graph"].get("relationships", [])),
                    "relationships": [],
                    "ontology_relations": [],
                },
                "error": None,
            },
            close_confidence_resources=lambda: None,
        ),
    }
    original = {name: sys.modules.get(name) for name in stubs}
    application_modules = [
        "backend.app.services.algorithm_capabilities",
        "backend.app.services.knowledge_precipitation",
        "backend.app.routers.algorithm_capabilities",
        "backend.app.routers.knowledge_precipitation",
        "backend.app.routers.confidence",
        "backend.app.main",
    ]
    original_application = {name: sys.modules.pop(name, None) for name in application_modules}
    sys.modules.update(stubs)
    try:
        from fastapi import FastAPI
        from backend.app.routers.algorithm_capabilities import router as capabilities_router
        from backend.app.routers.confidence import router as confidence_router
        from backend.app.routers.knowledge_precipitation import router as knowledge_router

        app = FastAPI()
        app.include_router(capabilities_router)
        app.include_router(knowledge_router)
        app.include_router(confidence_router)
        yield types.SimpleNamespace(app=app)
    finally:
        for name, previous in original.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        for name in application_modules:
            sys.modules.pop(name, None)
        for name, previous in original_application.items():
            if previous is not None:
                sys.modules[name] = previous
