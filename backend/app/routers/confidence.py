from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from algorithm.confidence_calculate.calculator import calculate_graph_confidence
from backend.app.responses import ErrorCode, deprecated_info, error_response, payload
from backend.app.schemas import GraphRequest


router = APIRouter(tags=["confidence"])


async def _calculate_confidence(body: GraphRequest, info: str) -> dict:
    graph = body.graph or {}
    if not graph.get("relationships") and not graph.get("ontology_relations"):
        return payload(
            {
                "evaluation_id": f"EVAL-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                "total_count": 0,
                "relationships": [],
                "ontology_relations": [],
            },
            info,
        )
    result = await run_in_threadpool(calculate_graph_confidence, body.model_dump())
    return payload(result.get("data"), info, result.get("code", 0), result.get("error"))


@router.post("/api/v1/graphs/confidence")
@router.post("/api/component/kg-update/calculate-confidence", deprecated=True)
async def calculate_confidence_api(http_request: Request, body: GraphRequest):
    info = "置信度评估完成"
    if http_request.url.path != "/api/v1/graphs/confidence":
        info = deprecated_info(info, "/api/v1/graphs/confidence")
    try:
        return await _calculate_confidence(body, info)
    except TimeoutError as exc:
        return error_response(
            ErrorCode.DEPENDENCY_TIMEOUT,
            info.replace("完成", "失败"),
            stage="confidence",
            details={"exception_type": type(exc).__name__, "message": str(exc)},
        )
    except ConnectionError as exc:
        return error_response(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            info.replace("完成", "失败"),
            stage="confidence",
            details={"exception_type": type(exc).__name__, "message": str(exc)},
        )
    except Exception as exc:
        return error_response(
            ErrorCode.CONFIDENCE_FAILED,
            info.replace("完成", "失败"),
            stage="confidence",
            details={"exception_type": type(exc).__name__, "message": str(exc)},
        )
