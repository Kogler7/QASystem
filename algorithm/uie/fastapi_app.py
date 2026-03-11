import logging
import sys
import time
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

from fastapi import FastAPI
from pydantic import BaseModel

from ie.extraction_api import extract, store_graph_to_neo4j
from ie.utils import ApiLLM

logger = logging.getLogger(__name__)


def _get_backend(base_url: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None) -> ApiLLM:
    kwargs = {}
    if base_url is not None:
        kwargs["base_url"] = base_url
    if model is not None:
        kwargs["model"] = model
    if api_key is not None:
        kwargs["api_key"] = api_key
    return ApiLLM(**kwargs) if kwargs else ApiLLM()


app = FastAPI(
    title="Knowledge Extraction API",
    description="基于 UML 本体驱动的信息抽取服务。",
    version="1.0.0",
)


class ExtractRequest(BaseModel):
    main_object: str
    text: str
    use_templates: bool = True
    store_to_neo4j: bool = False
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None


class StoreGraphRequest(BaseModel):
    graph: dict
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j2025"


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/extract")
def api_extract(req: ExtractRequest):
    t0 = time.perf_counter()
    backend = _get_backend(req.llm_base_url, req.llm_model, req.llm_api_key)
    data = extract(
        main_object=req.main_object,
        text=req.text,
        use_templates=req.use_templates,
        store_to_neo4j=req.store_to_neo4j,
        backend=backend,
    )
    logger.info("extract done: elapsed=%.2fs", time.perf_counter() - t0)
    return data


@app.post("/store_graph")
def api_store_graph(req: StoreGraphRequest):
    store_graph_to_neo4j(
        graph_struct=req.graph,
        neo4j_uri=req.neo4j_uri,
        neo4j_user=req.neo4j_user,
        neo4j_password=req.neo4j_password,
    )
    return {"status": "ok", "message": "已写入 Neo4j"}
