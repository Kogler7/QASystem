from fastapi import APIRouter, Request

from backend.app.responses import ErrorCode, deprecated_info, error_response
from backend.app.schemas import GraphRequest


router = APIRouter(tags=["confidence"])


@router.post("/api/v1/graphs/confidence")
@router.post("/api/component/kg-update/calculate-confidence", deprecated=True)
async def calculate_confidence_api(http_request: Request, _body: GraphRequest):
    info = "置信度评估失败"
    if http_request.url.path != "/api/v1/graphs/confidence":
        info = deprecated_info(info, "/api/v1/graphs/confidence")
    return error_response(
        ErrorCode.EXTERNAL_CAPABILITY_UNAVAILABLE,
        info,
        stage="confidence",
        details={
            "capability": "confidence.calculate",
            "reason": "第三方置信度接口尚未开通",
        },
        status_code=503,
    )
