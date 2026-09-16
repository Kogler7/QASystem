#!/usr/bin/env python3
"""HTTP orchestrator for parse → chunk → extract.

Calls the production knowledge-ingestion API and writes whatever the
response already contains. This directory does not import algorithm code
or reimplement parsing, chunking, or extraction.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

KIT_DIR = Path(__file__).resolve().parent
ONTOLOGY_SUFFIXES = {".puml", ".uml"}
SKIP_DOC_PREFIXES = (".", "~$", ".~")
INGEST_PATH = "/api/v1/pipelines/knowledge-ingestion"
DEFAULT_PARAMS: dict[str, Any] = {
    "backend_url": "http://127.0.0.1:8006",
    "poll_interval_seconds": 2,
    "poll_timeout_seconds": 3600,
    "options": {
        "parser": {
            "engine": "auto",
            "force": False,
            "page_sharding": "auto",
            "page_shard_size": 0,
            "max_page_shards": 16,
            "mineru_options": {
                "language": "ch",
                "ocr_mode": "auto",
                "table_enable": True,
                "formula_enable": True,
            },
        },
        "chunking": {
            "strategy": "recursive",
            "size": 4000,
            "chunk_overlap": 0,
            "chunk_threshold": 12000,
            "similarity": 0.6,
            "separators": None,
            "size_unit": "char",
            "breakpoint_method": "percentile",
        },
        "extraction": {"model": None, "merge": True},
        "storage": {"persist_graph": False, "knowledge_space_id": None},
    },
    "groups": {},
}


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(KIT_DIR / ".env")
load_env_file(KIT_DIR.parent / ".env")


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_params(path: Path) -> dict[str, Any]:
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"参数文件必须是 YAML 对象: {path}")
    return deep_merge(DEFAULT_PARAMS, raw)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def auth_headers() -> dict[str, str]:
    token = os.environ.get("AUTHORIZATION", "").strip()
    if not token:
        return {}
    if " " not in token:
        token = f"Bearer {token}"
    return {"Authorization": token}


def request_headers() -> dict[str, str]:
    return auth_headers()


def json_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, dict[str, Any]]:
    req = Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read()
            payload = json.loads(body.decode("utf-8") or "{}") if body else {}
            return response.status, payload
    except HTTPError as exc:
        body = exc.read()
        try:
            payload = json.loads(body.decode("utf-8") or "{}") if body else {}
        except json.JSONDecodeError:
            payload = {"code": exc.code, "info": str(exc), "error": {"raw": body.decode("utf-8", "replace")}}
        return exc.code, payload


def encode_multipart(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----qasystem{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for name, filename, content, content_type in files:
        ascii_name = filename.encode("ascii", "replace").decode("ascii") or "upload.bin"
        encoded_name = quote(filename, safe="")
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}\r\n'
            ).encode("utf-8")
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".ofd": "application/ofd",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".ppt": "application/vnd.ms-powerpoint",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".csv": "text/csv",
        ".html": "text/html",
        ".htm": "text/html",
    }.get(suffix, "application/octet-stream")


def resolve_path(workspace: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (workspace / path).resolve()
    return path


def read_ontology(path: Path) -> str:
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(
            item
            for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in ONTOLOGY_SUFFIXES
        )
        if not files:
            files = sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in ONTOLOGY_SUFFIXES
            )
    else:
        raise FileNotFoundError(f"本体路径不存在: {path}")
    if not files:
        raise FileNotFoundError(f"未找到本体文件: {path}")
    parts = [item.read_text(encoding="utf-8").strip() for item in files]
    text = "\n\n".join(part for part in parts if part).strip()
    if not text:
        raise ValueError(f"本体文件为空: {path}")
    return text


def discover_groups(workspace: Path, names: list[str] | None = None) -> list[Path]:
    wanted = {item.strip() for item in (names or []) if item.strip()}
    groups: list[Path] = []
    for child in sorted(workspace.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        if not (child / "docs").is_dir():
            continue
        if wanted and child.name not in wanted:
            continue
        groups.append(child)
    return groups


def collect_source_files(docs_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(docs_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith(SKIP_DOC_PREFIXES) or name == ".gitkeep":
            continue
        files.append(path)
    return files


def allocate_stem(used: set[str], path: Path) -> str:
    base = re.sub(r"[\\/]+", "_", path.stem).strip() or "document"
    if base not in used:
        used.add(base)
        return base
    suffix = path.suffix.lstrip(".").lower() or "bin"
    alt = f"{base}__{suffix}"
    if alt not in used:
        used.add(alt)
        return alt
    index = 2
    while f"{alt}_{index}" in used:
        index += 1
    name = f"{alt}_{index}"
    used.add(name)
    return name


def dump_error(extract_dir: Path, message: str, *, error_code: Any = None, details: Any = None) -> dict[str, Any]:
    payload = {
        "status": "failed",
        "error_code": error_code,
        "message": message,
        "details": details,
    }
    write_json(extract_dir / "error.json", payload)
    return payload


def envelope_ok(status_code: int, payload: dict[str, Any]) -> bool:
    if status_code >= 400:
        return False
    code = payload.get("code")
    return code in (None, 0)


def job_id_from(payload: dict[str, Any]) -> str:
    data = payload.get("data") or {}
    execution = payload.get("execution") or {}
    return str(data.get("job_id") or execution.get("job_id") or "")


def wait_for_job(base_url: str, job_id: str, params: dict[str, Any]) -> dict[str, Any]:
    interval = max(0.5, float(params.get("poll_interval_seconds") or 2))
    timeout = max(interval, float(params.get("poll_timeout_seconds") or 3600))
    deadline = time.time() + timeout
    status_url = urljoin(base_url.rstrip("/") + "/", f"api/v1/jobs/{job_id}")
    result_url = urljoin(base_url.rstrip("/") + "/", f"api/v1/jobs/{job_id}/result")
    headers = request_headers()
    last: dict[str, Any] = {}
    while time.time() < deadline:
        status_code, last = json_request(status_url, headers=headers)
        job = last.get("data") or {}
        job_status = str(job.get("status") or "")
        log(f"任务 {job_id} status={job_status or status_code}")
        if job_status == "succeeded":
            result_code, result_payload = json_request(result_url, headers=headers)
            if result_code == 409:
                time.sleep(interval)
                continue
            if not envelope_ok(result_code, result_payload):
                raise RuntimeError(result_payload.get("info") or f"任务结果不可用 HTTP {result_code}")
            return result_payload
        if job_status in {"failed", "cancelled", "canceled"}:
            raise RuntimeError(str(job.get("error") or last.get("info") or f"任务 {job_status}"))
        if status_code >= 400:
            raise RuntimeError(last.get("info") or f"查询任务失败 HTTP {status_code}")
        time.sleep(interval)
    raise TimeoutError(f"等待任务超时: {job_id}")


def ingest_file(
    *,
    base_url: str,
    source: Path,
    ontology: str,
    options: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", INGEST_PATH.lstrip("/"))
    body, content_type = encode_multipart(
        [("ontology", ontology), ("options", json.dumps(options, ensure_ascii=False))],
        [("file", source.name, source.read_bytes(), guess_content_type(source))],
    )
    headers = {**request_headers(), "Content-Type": content_type}
    timeout = max(60.0, float(params.get("poll_timeout_seconds") or 3600))
    status_code, payload = json_request(url, method="POST", data=body, headers=headers, timeout=timeout)
    if status_code == 202 or str((payload.get("data") or {}).get("status") or "") in {"queued", "running"}:
        job_id = job_id_from(payload)
        if not job_id:
            raise RuntimeError("接口返回 202 但没有 job_id")
        log(f"{source.name} 已入队 job_id={job_id}")
        return wait_for_job(base_url, job_id, params)
    if not envelope_ok(status_code, payload):
        raise RuntimeError(payload.get("info") or f"知识沉淀失败 HTTP {status_code}")
    return payload


def dump_response(group_dir: Path, stem: str, envelope: dict[str, Any]) -> dict[str, Any]:
    parse_dir = group_dir / "parse" / stem
    chunks_dir = group_dir / "chunks" / stem
    extract_dir = group_dir / "extract" / stem
    data = envelope.get("data") or {}
    stats = data.get("stats") or {}
    extraction = data.get("extraction") or {}
    write_json(extract_dir / "response.json", envelope)
    graph = extraction.get("graph")
    graphs = extraction.get("graphs")
    if graph is not None:
        write_json(extract_dir / "graph.json", graph)
    elif graphs is not None:
        write_json(extract_dir / "graph.json", {"graphs": graphs})
    document = stats.get("document")
    if document is not None:
        write_json(parse_dir / "parser.json", document)
    write_json(
        chunks_dir / "ranges.json",
        {
            "ranges": extraction.get("ranges"),
            "chunking": stats.get("chunking"),
        },
    )
    graph_obj = graph or {}
    return {
        "nodes": len(graph_obj.get("nodes") or []) if isinstance(graph_obj, dict) else 0,
        "relationships": len(graph_obj.get("relationships") or []) if isinstance(graph_obj, dict) else 0,
        "chunk_count": (stats.get("chunking") or {}).get("chunk_count"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HTTP 编排：知识沉淀流水线")
    parser.add_argument("--workspace", default=str(KIT_DIR), help="工作区根目录")
    parser.add_argument("--params", default=None, help="参数 YAML，默认工作区 params.yaml")
    parser.add_argument("--groups", default="", help="只处理指定分组，逗号分隔")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    params_path = Path(args.params).expanduser().resolve() if args.params else workspace / "params.yaml"
    group_filter = [item.strip() for item in str(args.groups or "").split(",") if item.strip()]
    params = load_params(params_path)
    base_url = (os.environ.get("QASYSTEM_BACKEND_URL") or params.get("backend_url") or DEFAULT_PARAMS["backend_url"]).rstrip("/")
    options = params.get("options") or {}
    group_cfg = params.get("groups") or {}

    log(f"工作区: {workspace}")
    log(f"参数文件: {params_path}")
    log(f"Backend: {base_url}")

    groups = discover_groups(workspace, group_filter or None)
    if not groups:
        log("未发现任何包含 docs/ 的分组")
        return 1

    started = datetime.now()
    run_id = started.strftime("%Y%m%d-%H%M%S")
    run_dir = workspace / "_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = dict(params)
    snapshot["backend_url"] = base_url
    write_json(run_dir / "params.resolved.json", snapshot)
    if params_path.is_file():
        (run_dir / "params.yaml").write_text(params_path.read_text(encoding="utf-8"), encoding="utf-8")

    summary: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "workspace": str(workspace),
        "backend_url": base_url,
        "groups": [],
        "files": [],
        "succeeded": 0,
        "failed": 0,
    }

    for group_dir in groups:
        name = group_dir.name
        cfg = group_cfg.get(name) or {}
        ontology_raw = cfg.get("ontology") if isinstance(cfg, dict) else None
        ontology_path = (
            resolve_path(workspace, str(ontology_raw))
            if ontology_raw
            else group_dir / "ontology"
        )
        if not ontology_path.exists():
            log(f"分组 {name}: 缺少权威本体 {ontology_path}，跳过")
            summary["groups"].append({"name": name, "error": f"缺少本体: {ontology_path}", "files": 0})
            continue
        try:
            ontology = read_ontology(ontology_path)
        except Exception as exc:  # noqa: BLE001 - 分组失败后继续
            log(f"分组 {name}: 读取本体失败: {exc}")
            summary["groups"].append({"name": name, "error": str(exc), "files": 0})
            continue

        sources = collect_source_files(group_dir / "docs")
        log(f"分组 {name}: {len(sources)} 个原文件")
        summary["groups"].append({"name": name, "files": len(sources), "error": None})
        used_stems: set[str] = set()

        for source in sources:
            stem = allocate_stem(used_stems, source)
            extract_dir = group_dir / "extract" / stem
            extract_dir.mkdir(parents=True, exist_ok=True)
            log(f"POST {INGEST_PATH} ← {name}/{source.name}")
            record: dict[str, Any] = {
                "group": name,
                "file": source.name,
                "stem": stem,
                "status": "failed",
                "nodes": 0,
                "relationships": 0,
                "chunk_count": None,
                "error": None,
            }
            try:
                envelope = ingest_file(
                    base_url=base_url,
                    source=source,
                    ontology=ontology,
                    options=options,
                    params=params,
                )
                if not envelope_ok(200, envelope):
                    write_json(extract_dir / "response.json", envelope)
                    error_obj = envelope.get("error")
                    record["error"] = dump_error(
                        extract_dir,
                        str(envelope.get("info") or "知识沉淀失败"),
                        error_code=(
                            error_obj.get("code")
                            if isinstance(error_obj, dict)
                            else envelope.get("code")
                        ),
                        details=error_obj,
                    )
                else:
                    dumped = dump_response(group_dir, stem, envelope)
                    record.update(dumped)
                    record["status"] = "succeeded"
            except (URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                record["error"] = dump_error(extract_dir, str(exc), error_code="HTTP_ORCHESTRATION_FAILED")
            except Exception as exc:  # noqa: BLE001 - 单文件失败后继续
                record["error"] = dump_error(extract_dir, str(exc), error_code="ORCHESTRATION_EXCEPTION")
            summary["files"].append(record)
            if record["status"] == "succeeded":
                summary["succeeded"] += 1
            else:
                summary["failed"] += 1
            log(f"完成 {name}/{source.name}: status={record['status']}")

    summary["finished_at"] = datetime.now().isoformat()
    write_json(run_dir / "summary.json", summary)
    log(
        f"结束 run={run_id} succeeded={summary['succeeded']} "
        f"failed={summary['failed']} → {run_dir / 'summary.json'}"
    )
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
