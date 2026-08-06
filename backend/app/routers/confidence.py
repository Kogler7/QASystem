from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.app.responses import ErrorCode, deprecated_info, error_response, payload
from backend.app.schemas import GraphRequest
from backend.app.execution.api import ExecutionUnavailable, ManagedExecutionError, dispatch_operation, prefer_wait_seconds


router = APIRouter(tags=["confidence"])


@router.post("/api/v1/graphs/confidence")
@router.post("/api/component/kg-update/calculate-confidence", deprecated=True)
async def calculate_confidence_api(http_request: Request, body: GraphRequest):
    info = "置信度评估完成"
    if http_request.url.path != "/api/v1/graphs/confidence":
        info = deprecated_info(info, "/api/v1/graphs/confidence")
    try:
        result = await dispatch_operation(
            operation="graph.confidence",
            resource_class="gpu.confidence",
            payload=body.model_dump(),
            total_units=len((body.graph or {}).get("relationships", [])) + len((body.graph or {}).get("ontology_relations", [])),
            wait_seconds=prefer_wait_seconds(http_request.headers.get("Prefer")),
        )
        return JSONResponse(
            status_code=result.status_code,
            content=payload(result.data, info, execution=result.execution),
        )
    except ExecutionUnavailable as exc:
        return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, info.replace("完成", "失败"), stage="execution", details=str(exc), status_code=503)
    except ManagedExecutionError as exc:
        cause = exc.cause
        if isinstance(cause, TimeoutError):
            return error_response(ErrorCode.DEPENDENCY_TIMEOUT, info.replace("完成", "失败"), stage="confidence", details={"exception_type": type(cause).__name__, "message": str(cause)}, execution=exc.execution)
        if isinstance(cause, ConnectionError):
            return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, info.replace("完成", "失败"), stage="confidence", details={"exception_type": type(cause).__name__, "message": str(cause)}, execution=exc.execution)
        return error_response(ErrorCode.CONFIDENCE_FAILED, info.replace("完成", "失败"), stage="confidence", details={"exception_type": type(cause).__name__, "message": str(cause)}, execution=exc.execution)
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
