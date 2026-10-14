from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx
from qasystem_config.configuration import getenv


USER_CONTEXT_HEADERS = (
    "authorization",
    "x-user-id",
    "x-user-name",
    "x-tenant-id",
    "x-user-roles",
)


@dataclass(frozen=True, slots=True)
class Endpoint:
    method: str
    path: str | None
    env_name: str | None = None


# All application-team APIs are registered here. A missing path means the
# contract has not been opened yet and must fail only when invoked.
ENDPOINTS: dict[str, Endpoint] = {
    "files.upload": Endpoint("POST", "/third/files/upload"),
    "vectors.store": Endpoint("POST", "/third/vectors/store"),
    "graph.cypher.query": Endpoint("POST", "/third/cypher/query"),
    "graph.write": Endpoint("POST", None),
    "confidence.calculate": Endpoint("POST", None),
    "documents.evidence.search": Endpoint("POST", None),
    "models.embeddings.create": Endpoint("POST", "/v1/embeddings", "EMBEDDING_SERVICE_PATH"),
    "models.decisions.create": Endpoint("POST", "/v1/systemone", "CLEF_SERVICE_PATH"),
    "models.nanojev.create": Endpoint("POST", "/v1/systemone", "NANOJEV_SERVICE_PATH"),
    "documents.parse": Endpoint("POST", "/v1/documents/parse", "DOCUMENT_PARSE_SERVICE_PATH"),
}

_current_user_headers: ContextVar[dict[str, str]] = ContextVar(
    "application_service_user_headers", default={}
)


class ExternalCapabilityUnavailable(RuntimeError):
    def __init__(self, capability: str, reason: str):
        super().__init__(f"外部能力 {capability} 不可用：{reason}")
        self.capability = capability
        self.reason = reason


class ApplicationServiceError(RuntimeError):
    pass


def get_application_service_url(explicit: str | None = None) -> str:
    return (
        explicit
        or config_getenv("APPLICATION_SERVICE_URL", "").strip()
    ).rstrip("/")


def endpoint_for(capability: str) -> Endpoint:
    try:
        registered = ENDPOINTS[capability]
    except KeyError as exc:
        raise ExternalCapabilityUnavailable(capability, "能力未注册") from exc
    configured_path = getenv(registered.env_name, "").strip() if registered.env_name else ""
    path = configured_path or registered.path
    if not path:
        raise ExternalCapabilityUnavailable(capability, "第三方接口尚未开通")
    return Endpoint(registered.method, path, registered.env_name)


def capture_user_headers(headers: dict[str, str]) -> Token:
    filtered = {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in USER_CONTEXT_HEADERS and value
    }
    return _current_user_headers.set(filtered)


def reset_user_headers(token: Token) -> None:
    _current_user_headers.reset(token)


def get_user_headers() -> dict[str, str]:
    return dict(_current_user_headers.get())


@contextmanager
def bind_user_headers(headers: dict[str, str] | None) -> Iterator[None]:
    token = capture_user_headers(headers or {})
    try:
        yield
    finally:
        reset_user_headers(token)


class ApplicationServiceClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = get_application_service_url(base_url)
        self.timeout = timeout or float(config_getenv("APPLICATION_SERVICE_TIMEOUT_SECONDS", "30"))

    def _headers(self) -> dict[str, str]:
        return {
            key: value
            for key, value in get_user_headers().items()
            if key in USER_CONTEXT_HEADERS
        }

    def request(
        self,
        capability: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        endpoint = endpoint_for(capability)
        if not self.base_url:
            raise ExternalCapabilityUnavailable(
                capability,
                "未配置 APPLICATION_SERVICE_URL",
            )
        url = f"{self.base_url}{endpoint.path}"
        try:
            response = httpx.request(
                endpoint.method,
                url,
                headers=self._headers(),
                json=json,
                data=data,
                files=files,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise ApplicationServiceError(
                    f"{capability} 鉴权失败（HTTP {status}）"
                ) from exc
            raise ApplicationServiceError(f"{capability} 请求失败（HTTP {status}）") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ApplicationServiceError(f"{capability} 调用失败：{exc}") from exc
        if not isinstance(body, dict):
            raise ApplicationServiceError(f"{capability} 返回体不是对象")
        if body.get("code", 0) != 0:
            message = body.get("msg") or body.get("info") or "未知业务错误"
            raise ApplicationServiceError(f"{capability} 业务失败：{message}")
        return body

    def query_cypher(self, statement: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self.request(
            "graph.cypher.query",
            json={"statement": statement, "parameters": parameters or {}},
        )
        return body.get("data") or {}

    def execute_cypher(self, statement: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        data = self.query_cypher(statement, parameters)
        rows = data.get("rows") or []
        columns = data.get("columns") or []
        if rows and columns and not isinstance(rows[0], dict):
            return [dict(zip(columns, row)) for row in rows]
        return rows

    def upload_file(
        self,
        content: bytes,
        file_name: str,
        content_type: str,
        knowledge_space_id: str | None = None,
    ) -> dict[str, Any]:
        safe_name = Path(file_name or "artifact.bin").name
        data = {"knowledgeSpaceId": knowledge_space_id} if knowledge_space_id else None
        body = self.request(
            "files.upload",
            data=data,
            files={"file": (safe_name, content, content_type)},
        )
        result = body.get("data") or {}
        return {
            "id": result.get("id"),
            "name": result.get("file_name") or result.get("name") or safe_name,
            "path": result.get("file_path") or result.get("path"),
            "url": result.get("file_url") or result.get("url"),
        }

    def store_vectors(
        self,
        items: list[dict[str, Any]],
        knowledge_space_id: str | None,
        file_id: str | None,
    ) -> dict[str, Any]:
        body = self.request(
            "vectors.store",
            json={
                "items": [
                    {"vector": item["vector"], "text": item["text"]}
                    for item in items
                ],
                "knowledgeSpaceId": knowledge_space_id,
                "fileId": file_id,
            },
        )
        return body.get("data") or {}
