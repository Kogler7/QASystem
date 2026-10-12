from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from algorithm.runtime_settings import getenv

from .application import endpoint_for


class ModelServiceError(RuntimeError):
    """Raised when a remote model service cannot return a valid result."""


def _base_url() -> str:
    return getenv("EMBEDDING_SERVICE_URL", "").strip().rstrip("/")


class RemoteEmbeddingClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or _base_url()).rstrip("/")
        self.path = endpoint_for("models.embeddings.create").path or "/v1/embeddings"
        self.model = model or getenv("EMBEDDING_MODEL", "")
        self.timeout = timeout or float(getenv("EMBEDDING_TIMEOUT_SECONDS", "60"))
        self.api_key = os.getenv("EMBEDDING_SERVICE_API_KEY", "").strip()
        try:
            self.max_batch_size = max(1, int(getenv("EMBEDDING_MAX_BATCH_SIZE", "32")))
        except ValueError:
            self.max_batch_size = 32

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not self.base_url:
            raise ModelServiceError("未配置 EMBEDDING_SERVICE_URL")
        if not texts:
            raise ValueError("文本列表不能为空")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.max_batch_size):
            vectors.extend(self._encode_batch(texts[start : start + self.max_batch_size]))
        return vectors

    def _encode_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"input": texts}
        if self.model:
            payload["model"] = self.model
        try:
            response = httpx.post(
                f"{self.base_url}{self.path}",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ModelServiceError(
                f"远程 Embedding 请求失败（HTTP {exc.response.status_code}）"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelServiceError(f"远程 Embedding 调用失败：{exc}") from exc

        vectors = body.get("data") if isinstance(body, dict) else None
        if vectors is None and isinstance(body, dict):
            vectors = body.get("vectors")
        if not isinstance(vectors, list):
            raise ModelServiceError("远程 Embedding 返回缺少 data/vectors 数组")

        indexed: list[tuple[int, list[float]]] = []
        for fallback_index, item in enumerate(vectors):
            if not isinstance(item, dict):
                raise ModelServiceError("远程 Embedding 返回项不是对象")
            index = item.get("index", fallback_index)
            vector = item.get("embedding", item.get("vector"))
            if not isinstance(index, int) or not isinstance(vector, list):
                raise ModelServiceError("远程 Embedding 返回项缺少有效 index/embedding")
            try:
                indexed.append((index, [float(value) for value in vector]))
            except (TypeError, ValueError) as exc:
                raise ModelServiceError("远程 Embedding 向量包含非数字值") from exc
        indexed.sort(key=lambda item: item[0])
        if [index for index, _ in indexed] != list(range(len(texts))):
            raise ModelServiceError("远程 Embedding 返回数量或 index 与输入不一致")
        dimensions = {len(vector) for _, vector in indexed}
        if len(dimensions) > 1:
            raise ModelServiceError("远程 Embedding 返回向量维度不一致")
        return [vector for _, vector in indexed]


class RemoteDocumentParserClient:
    def __init__(self, base_url: str | None = None, *, timeout: float | None = None) -> None:
        self.base_url = (
            base_url or getenv("DOCUMENT_PARSE_SERVICE_URL", "")
        ).strip().rstrip("/")
        self.path = endpoint_for("documents.parse").path or "/v1/documents/parse"
        self.timeout = timeout or float(getenv("DOCUMENT_PARSE_TIMEOUT_SECONDS", "900"))
        self.api_key = os.getenv("DOCUMENT_PARSE_SERVICE_API_KEY", "").strip()

    def parse(self, file_path: str, **options: Any) -> dict[str, Any]:
        if not self.base_url:
            raise ModelServiceError("未配置 DOCUMENT_PARSE_SERVICE_URL")
        path = Path(file_path)
        if not path.is_file():
            raise ModelServiceError("待解析文件不存在")
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        data = {key: str(value) for key, value in options.items() if value is not None}
        try:
            with path.open("rb") as handle:
                response = httpx.post(
                    f"{self.base_url}{self.path or '/v1/documents/parse'}",
                    headers=headers,
                    data=data,
                    files={"file": (path.name, handle, "application/octet-stream")},
                    timeout=self.timeout,
                )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ModelServiceError(
                f"远程文档解析请求失败（HTTP {exc.response.status_code}）"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelServiceError(f"远程文档解析调用失败：{exc}") from exc
        if not isinstance(body, dict):
            raise ModelServiceError("远程文档解析返回体不是对象")
        result = body.get("data") if isinstance(body.get("data"), dict) else body
        markdown = result.get("markdown") or result.get("content")
        if not isinstance(markdown, str):
            raise ModelServiceError("远程文档解析返回缺少 markdown/content")
        return {
            "content": markdown,
            "metadata": {
                "processor": "remote-document-parser",
                "model_used": result.get("model") or result.get("parser") or "remote",
                "request_id": result.get("request_id") or body.get("request_id"),
                "remote": True,
                "pages": result.get("pages"),
            },
        }
