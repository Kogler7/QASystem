from datetime import datetime

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from algorithm.confidence_calculate.calculator import calculate_graph_confidence
from backend.app.responses import payload
from backend.app.schemas import GraphRequest


router = APIRouter(tags=["confidence"])


@router.post("/api/component/kg-update/calculate-confidence")
async def calculate_confidence_api(body: GraphRequest) -> dict:
    graph = body.graph or {}
    if not graph.get("relationships") and not graph.get("ontology_relations"):
        return payload(
            {
                "evaluation_id": f"EVAL-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                "total_count": 0,
                "graph": {
                    "nodes": graph.get("nodes", []),
                    "relationships": graph.get("relationships", []),
                    "ontology_relations": graph.get("ontology_relations", []),
                },
                "details": {},
            },
            "置信度评估完成",
        )
    return await run_in_threadpool(calculate_graph_confidence, body.model_dump())
