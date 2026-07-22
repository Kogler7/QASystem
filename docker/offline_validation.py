"""End-to-end validator for the sealed offline runtime compose environment."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx


SAMPLE_FILES = {
    "pdf": "KEM-book_1.pdf",
    "scanned_pdf": "供应商网络安全承诺-交大.pdf",
    "doc": "02中国标准A型地铁列车车钩统型方案.doc",
    "xlsx": "故障数据样例（规则抽取）.xlsx",
    "markdown": "术语库（采购管理域）V1.md",
    "image": "测试图片.png",
}


def _wait_for_backend(client: httpx.Client) -> None:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            if client.get("/health").json() == {"status": "ok"}:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError("backend did not become healthy")


def _assert_success(response: httpx.Response, label: str) -> dict:
    body = response.json()
    if response.status_code != 200 or body.get("code") != 0:
        raise RuntimeError(f"{label} failed: status={response.status_code} body={body}")
    return body["data"]


def _parse(client: httpx.Client, path: Path, engine: str = "auto") -> dict:
    with path.open("rb") as source:
        response = client.post(
            "/api/v1/documents/parse",
            files={"file": (path.name, source, "application/octet-stream")},
            data={"options": json.dumps({"engine": engine}, ensure_ascii=False)},
            timeout=600,
        )
    result = _assert_success(response, f"parse {path.name}")
    if not result["markdown"].strip():
        raise RuntimeError(f"parse {path.name} returned empty markdown")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--base-url", default="http://backend:8000")
    args = parser.parse_args()
    samples = args.samples / "解析测试数据"

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        _wait_for_backend(client)
        health_response = client.get("/health?full=true")
        if health_response.status_code != 200:
            raise RuntimeError(f"full health failed: status={health_response.status_code} body={health_response.json()}")
        health = health_response.json()
        if health["status"] != "ok":
            raise RuntimeError(f"full health returned invalid status: {health}")

        parsed = {kind: _parse(client, samples / name) for kind, name in SAMPLE_FILES.items()}
        if parsed["pdf"]["parser"]["final_engine"] != "docling":
            raise RuntimeError(f"text PDF did not use Docling: {parsed['pdf']['parser']}")
        if parsed["xlsx"]["parser"]["final_engine"] != "pandas":
            raise RuntimeError(f"XLSX did not use pandas: {parsed['xlsx']['parser']}")
        if parsed["markdown"]["parser"]["final_engine"] != "text":
            raise RuntimeError(f"Markdown did not use text parser: {parsed['markdown']['parser']}")

        embeddings = _assert_success(
            client.post("/api/v1/embeddings/texts", json={"items": [{"text": "离线验收"}]}),
            "embedding",
        )
        if embeddings["dimension"] != 512:
            raise RuntimeError(f"expected 512-dimensional embedding, got {embeddings['dimension']}")

        ontology = "@startuml\nclass Term\n@enduml"
        graph = _assert_success(
            client.post("/api/v1/graphs/extract", json={"ontology": ontology, "items": [{"text": "离线知识抽取"}]}),
            "mock LLM graph extraction",
        )
        if "graph" not in graph:
            raise RuntimeError(f"graph extraction response missing graph: {graph}")

        ingestion = _assert_success(
            client.post(
                "/api/v1/pipelines/knowledge-ingestion",
                files={"file": ("offline.md", "# 离线验收\n知识抽取测试".encode("utf-8"), "text/markdown")},
                data={"ontology": ontology, "options": json.dumps({"parser": {"engine": "text"}})},
                timeout=180,
            ),
            "knowledge ingestion",
        )
        if ingestion["stats"]["extraction"]["scheduler"] is None:
            raise RuntimeError("knowledge ingestion did not execute extraction")

    print("offline runtime validation passed", flush=True)


if __name__ == "__main__":
    main()
