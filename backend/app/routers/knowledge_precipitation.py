import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge-precipitation"])

_protocol_app = None
_protocol_import_error: Exception | None = None

try:
    from algorithm.knowledge_precipitation import app as _protocol_app
except Exception as exc:  # pragma: no cover - depends on optional algorithm deps
    _protocol_import_error = exc
    logger.warning("知识沉淀协议接口加载失败: %s", exc)


def _copy_protocol_routes() -> None:
    if _protocol_app is None:
        return

    for route in _protocol_app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        endpoint = getattr(route, "endpoint", None)
        if not path or not methods or endpoint is None:
            continue
        if path in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}:
            continue
        router.add_api_route(
            path,
            endpoint,
            methods=sorted(methods),
            name=getattr(route, "name", None),
            response_model=getattr(route, "response_model", None),
            status_code=getattr(route, "status_code", None),
            tags=["knowledge-precipitation"],
        )


_copy_protocol_routes()


async def _unavailable() -> dict[str, Any]:
    raise HTTPException(
        status_code=503,
        detail=f"知识沉淀协议接口不可用: {_protocol_import_error}",
    )


if _protocol_app is None:
    for _method, _path in [
        ("POST", "/third/files/upload"),
        ("POST", "/vector/save"),
        ("POST", "/api/component/nl2cypher/text-to-vector"),
        ("POST", "/api/component/nl2cypher/text-to-vectors"),
        ("POST", "/api/component/kg-update/calculate-confidence"),
        ("POST", "/api/knowledge/precipitation"),
        ("GET", "/api/knowledge/health"),
    ]:
        router.add_api_route(_path, _unavailable, methods=[_method])
