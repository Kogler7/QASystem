from __future__ import annotations

import os
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AGENT_ROOT = ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))


@pytest.fixture(autouse=True)
def _inline_mcp_tools(monkeypatch):
    """Unit tests call tool functions without starting Redis workers."""
    monkeypatch.setenv("MCP_EXECUTION_MODE", "inline")


def _module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@pytest.fixture(autouse=True)
def _protocol_stub_lifetime(request):
    """Keep the session app stable but restore global stubs after each test."""
    if "protocol_module" not in request.fixturenames:
        yield
        return
    protocol = request.getfixturevalue("protocol_module")
    sys.modules.update(protocol.stubs)
    try:
        yield
    finally:
        for name, previous in protocol.original_stubs.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


@pytest.fixture(scope="session")
def protocol_module():
    # The app and registry share module/class references for the session.
    # _protocol_stub_lifetime limits sys.modules overrides to each protocol
    # test without rebuilding the app and invalidating monkeypatch targets.
    @asynccontextmanager
    async def _run():
        yield

    class _MCPStub:
        session_manager = types.SimpleNamespace(run=_run)

        @staticmethod
        def streamable_http_app():
            async def app(_scope, _receive, send):
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b""})

            return app

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
        valid_uids = {uid for uid in nodes if uid}
        for item in items:
            for relation in item["graph"].get("relationships", []):
                from_uid = relation.get("from_uid")
                to_uid = relation.get("to_uid")
                if not from_uid or not to_uid:
                    continue
                if from_uid not in valid_uids or to_uid not in valid_uids:
                    continue
                key = (relation.get("from_uid"), relation.get("to_uid"), relation.get("type"))
                relationships[key] = relation
        return {"nodes": list(nodes.values()), "relationships": list(relationships.values())}

    stubs = {
        "algorithm.NL_to_cypher.vector": _module(
            "algorithm.NL_to_cypher.vector",
            batch_text_to_vectors=lambda texts: [[float(index), 1.0] for index, _ in enumerate(texts)],
        ),
        # V1 now only serves the no-ontology summary paths.
        "algorithm.knowledge_extract": _module(
            "algorithm.knowledge_extract",
            summarize_text=lambda **kwargs: {
                "text": kwargs.get("text", ""),
                "summary": "摘要",
                "attempts": [],
            },
            store_extraction_to_neo4j=lambda **_kwargs: {"success": True},
        ),
        "algorithm.knowledge_extract.v1.batch": _module(
            "algorithm.knowledge_extract.v1.batch",
            summarize_chunks=lambda chunks, **_kwargs: [
                {"index": index, "text": text, "summary": "摘要", "attempts": []}
                for index, text in enumerate(chunks)
            ],
        ),
        "algorithm.knowledge_extract.v2": _module(
            "algorithm.knowledge_extract.v2",
            deduplicate_chunk_graphs=deduplicate_chunk_graphs,
        ),
        "algorithm.knowledge_extract.v2.config": _module(
            "algorithm.knowledge_extract.v2.config",
            get_llm_config=lambda: {
                "max_concurrency": 4, "qps": 0, "qpm": 0,
                "base_url": "http://test-llm/v1", "model": "test-model",
                "max_tokens": 16384, "request_timeout": 300.0,
                "disable_thinking": True, "api_key": "",
            },
        ),
        # Production extraction runs Knowledge Extract V2 through this adapter,
        # which would reach the LLM.  Protocol tests stay hermetic by stubbing
        # the adapter instead of the algorithm package.
        "backend.app.services.knowledge_extract_adapter": _module(
            "backend.app.services.knowledge_extract_adapter",
            extract_chunks=lambda ontology, chunks, **_kwargs: [
                {
                    "index": index,
                    "text": text,
                    "raw": {},
                    "graph": {"nodes": [], "relationships": []},
                    "attempts": [],
                    "warnings": [],
                    "profile": {},
                    "error": None,
                    "error_code": None,
                    "raw_response": "",
                }
                for index, text in enumerate(chunks)
            ],
            # 与真实适配器的默认发布语义一致（KG_EXTRACT_PUBLISH_BEST_EFFORT=1）。
            publish_best_effort_graph=lambda: True,
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
        "mcp_services.server": _module("mcp_services.server", mcp=_MCPStub()),
    }
    original_environment = {
        name: os.environ.get(name)
        for name in (
            "EXECUTION_MODE",
            "EXECUTION_REDIS_URL",
            "ENVIRONMENT",
        )
    }
    # Existing protocol tests exercise algorithm contracts without starting
    # Redis or a Worker. Inline is explicit test configuration, never a
    # production fallback.
    os.environ["EXECUTION_MODE"] = "inline"
    os.environ.pop("EXECUTION_REDIS_URL", None)
    os.environ["ENVIRONMENT"] = "test"
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
        yield types.SimpleNamespace(app=app, stubs=stubs, original_stubs=original)
    finally:
        for name, value in original_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
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
