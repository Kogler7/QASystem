#!/usr/bin/env python3
"""知识沉淀评测编排：先后台流程，再插件流程。

评测集布局（每个类型下每个 case）：
  <类型>/<case>/docs/<原文件>
  <类型>/<case>/extract/golden.json
类型级本体在 <类型>/ontology/，也可用 params.yaml 覆盖。

本目录只调已有 HTTP / MCP，不 import algorithm / backend。
"""
from __future__ import annotations

import argparse
import json
import os
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
TYPE_RESERVED = {"docs", "ontology", "extract", "parse", "chunks"}
TYPE_SKIP_NAMES = {"中车RAG验证数据集"}
INGEST_PATH = "/api/v1/pipelines/knowledge-ingestion"
PARSE_PATH = "/api/v1/documents/parse"
CHUNK_PATH = "/api/v1/documents/chunk"
EXTRACT_PATH = "/api/v1/graphs/extract"
MCP_TOOL_NAME = "kg_extract_v2"
DEFAULT_PARAMS: dict[str, Any] = {
    "backend_url": "http://127.0.0.1:8006",
    "mcp_url": None,
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
            "keep_page_edges": "first_page",
            "page_edge_min_chars": 3,
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
    "eval": {
        "match_mode": "llm",
        "judge_model": None,
        "judge_base_url": None,
        "judge_timeout_seconds": 300,
        "judge_max_tokens": 8192,
        "judge_batch_size": 20,
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
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("需要 PyYAML：pip install pyyaml") from exc

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"参数文件必须是 YAML 对象: {path}")
    return deep_merge(DEFAULT_PARAMS, raw)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def auth_headers() -> dict[str, str]:
    token = os.environ.get("AUTHORIZATION", "").strip()
    if not token:
        return {}
    if " " not in token:
        token = f"Bearer {token}"
    return {"Authorization": token}


def http_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, dict[str, str], bytes]:
    req = Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read()
            return response.status, {key.lower(): value for key, value in response.headers.items()}, body
    except HTTPError as exc:
        body = exc.read()
        return exc.code, {key.lower(): value for key, value in (exc.headers.items() if exc.headers else [])}, body


def decode_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {"code": -1, "info": "响应不是 JSON", "error": {"raw": body.decode("utf-8", "replace")}}
    return payload if isinstance(payload, dict) else {"data": payload}


def json_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, dict[str, Any]]:
    status, _response_headers, body = http_request(
        url, method=method, data=data, headers=headers, timeout=timeout
    )
    return status, decode_json_body(body)


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
            ).encode()
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


def collect_source_files(docs_dir: Path) -> list[Path]:
    files: list[Path] = []
    if not docs_dir.is_dir():
        return files
    for path in sorted(docs_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith(SKIP_DOC_PREFIXES) or name == ".gitkeep":
            continue
        files.append(path)
    return files


def discover_types(workspace: Path, names: list[str] | None = None) -> list[Path]:
    wanted = {item.strip() for item in (names or []) if item.strip()}
    types: list[Path] = []
    for child in sorted(workspace.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        if child.name in TYPE_SKIP_NAMES:
            continue
        if wanted and child.name not in wanted:
            continue
        types.append(child)
    return types


def discover_cases(type_dir: Path, names: list[str] | None = None) -> list[Path]:
    wanted = {item.strip() for item in (names or []) if item.strip()}
    cases: list[Path] = []
    for child in sorted(type_dir.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        if child.name in TYPE_RESERVED:
            continue
        if not (child / "docs").is_dir():
            continue
        if wanted and child.name not in wanted:
            continue
        cases.append(child)
    return cases


def locate_target_path(workspace: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        candidates = [path]
    else:
        rest = Path(*path.parts[1:]) if path.parts and path.parts[0] == workspace.name else None
        candidates = [
            Path.cwd() / path,
            workspace / path,
            workspace.parent / path,
        ]
        if rest is not None:
            candidates.append(workspace / rest)
    seen: set[Path] = set()
    for item in candidates:
        resolved = item.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    raise FileNotFoundError(f"评测路径不存在: {raw}")


def normalize_eval_dir(path: Path) -> Path:
    current = path
    if current.is_file():
        current = current.parent
    while current.name in TYPE_RESERVED:
        if current.parent == current:
            break
        current = current.parent
    return current


def classify_eval_path(workspace: Path, path: Path) -> tuple[str | None, str | None]:
    path = normalize_eval_dir(path.resolve())
    workspace = workspace.resolve()
    try:
        rel = path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"路径不在评测工作区内: {path}") from exc
    parts = [part for part in rel.parts if part not in {".", ""}]
    if not parts:
        return None, None
    group = parts[0]
    if group.startswith(".") or group.startswith("_") or group in TYPE_SKIP_NAMES or group in TYPE_RESERVED:
        raise ValueError(f"不是评测类型或 case 目录: {path}")
    if len(parts) == 1:
        return group, None
    case = parts[1]
    if case.startswith(".") or case.startswith("_") or case in TYPE_RESERVED:
        raise ValueError(f"不是 case 目录: {path}")
    return group, case


def selection_from_targets(workspace: Path, targets: list[str]) -> dict[str, set[str] | None] | None:
    selection: dict[str, set[str] | None] = {}
    for raw in targets:
        group, case = classify_eval_path(workspace, locate_target_path(workspace, raw))
        if group is None:
            return None
        if case is None:
            selection[group] = None
            continue
        existing = selection.get(group)
        if existing is None and group in selection:
            continue
        chosen = set() if existing is None else existing
        chosen.add(case)
        selection[group] = chosen
    return selection


def case_names_for_type(
    type_name: str,
    *,
    selection: dict[str, set[str] | None] | None,
    case_filter: list[str],
) -> list[str] | None:
    if selection is not None:
        wanted = selection.get(type_name)
        return None if wanted is None else sorted(wanted)
    return case_filter or None


def resolve_ontology_path(workspace: Path, type_dir: Path, case_dir: Path, group_cfg: dict[str, Any]) -> Path:
    case_ontology = case_dir / "ontology"
    if case_ontology.exists():
        return case_ontology
    cfg = group_cfg.get(type_dir.name) or {}
    ontology_raw = cfg.get("ontology") if isinstance(cfg, dict) else None
    if ontology_raw:
        return resolve_path(workspace, str(ontology_raw))
    return type_dir / "ontology"


def load_golden(case_dir: Path) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    path = case_dir / "extract" / "golden.json"
    if not path.is_file():
        return path, None, "缺少 extract/golden.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return path, None, f"golden.json 不是合法 JSON: {exc}"
    if not isinstance(data, dict):
        return path, None, "golden.json 顶层必须是对象"
    entities, relations = normalize_graph(data)
    if not entities and not relations and not data.get("score_empty"):
        return path, None, "golden.json 为空（未标注）；填写实体/关系后才评分"
    return path, data, None


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
    headers = auth_headers()
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


def submit_and_wait(
    url: str,
    *,
    base_url: str,
    data: bytes | None,
    headers: dict[str, str],
    params: dict[str, Any],
) -> dict[str, Any]:
    timeout = max(60.0, float(params.get("poll_timeout_seconds") or 3600))
    status_code, payload = json_request(url, method="POST", data=data, headers=headers, timeout=timeout)
    if status_code == 202 or str((payload.get("data") or {}).get("status") or "") in {"queued", "running"}:
        job_id = job_id_from(payload)
        if not job_id:
            raise RuntimeError("接口返回 202 但没有 job_id")
        log(f"已入队 job_id={job_id}")
        return wait_for_job(base_url, job_id, params)
    if not envelope_ok(status_code, payload):
        info = payload.get("info") or f"请求失败 HTTP {status_code}"
        raise RuntimeError(json.dumps({"info": info, "error": payload.get("error")}, ensure_ascii=False))
    return payload


def fail_payload(exc: BaseException) -> dict[str, Any]:
    message = str(exc)
    details: Any = None
    if isinstance(exc, RuntimeError):
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and "info" in parsed:
            message = str(parsed.get("info") or message)
            details = parsed.get("error")
    return {
        "status": "failed",
        "error_code": type(exc).__name__,
        "message": message,
        "details": details,
    }


# --- graph scoring (identity: type+name / 五元组) ---


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _node_type_name(node: dict[str, Any]) -> tuple[str, str]:
    properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    etype = _safe_text(node.get("type") or node.get("label"))
    ename = _safe_text(node.get("name") or properties.get("name") or node.get("identity"))
    if (not etype or not ename) and "::" in _safe_text(node.get("uid")):
        uid_type, uid_name = _safe_text(node.get("uid")).split("::", 1)
        etype = etype or uid_type
        ename = ename or uid_name
    return etype, ename


def graph_from_entities(entities: list[Any], relations: list[Any]) -> dict[str, list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        etype, ename = _node_type_name(entity)
        if not etype or not ename:
            continue
        nodes.append(
            {
                "uid": f"{etype}::{ename}",
                "label": etype,
                "properties": {"name": ename, **(entity.get("properties") or {} if isinstance(entity.get("properties"), dict) else {})},
            }
        )
    node_by_name = {(item["label"], item["properties"]["name"]): item["uid"] for item in nodes}
    relationships: list[dict[str, Any]] = []
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        from_type = _safe_text(relation.get("from_type"))
        from_name = _safe_text(relation.get("from_name"))
        to_type = _safe_text(relation.get("to_type"))
        to_name = _safe_text(relation.get("to_name"))
        rel_type = _safe_text(relation.get("rel_type") or relation.get("type"))
        from_uid = node_by_name.get((from_type, from_name))
        to_uid = node_by_name.get((to_type, to_name))
        if from_uid and to_uid and rel_type:
            relationships.append({"from_uid": from_uid, "to_uid": to_uid, "type": rel_type})
    return {"nodes": nodes, "relationships": relationships}


def normalize_graph(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        return [], []
    if "entities" in payload or "relations" in payload:
        return list(payload.get("entities") or []), list(payload.get("relations") or [])
    graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else payload
    if not isinstance(graph, dict):
        return [], []
    if "entities" in graph or "relations" in graph:
        return list(graph.get("entities") or []), list(graph.get("relations") or [])
    nodes = [item for item in (graph.get("nodes") or []) if isinstance(item, dict)]
    relationships = [item for item in (graph.get("relationships") or []) if isinstance(item, dict)]
    entities: list[dict[str, Any]] = []
    by_uid: dict[str, tuple[str, str]] = {}
    for node in nodes:
        etype, ename = _node_type_name(node)
        if not etype or not ename:
            continue
        entities.append({"type": etype, "name": ename, "properties": node.get("properties") or {}})
        uid = _safe_text(node.get("uid"))
        if uid:
            by_uid[uid] = (etype, ename)
    relations: list[dict[str, Any]] = []
    for rel in relationships:
        rel_type = _safe_text(rel.get("type") or rel.get("rel_type"))
        from_uid = _safe_text(rel.get("from_uid") or rel.get("head"))
        to_uid = _safe_text(rel.get("to_uid") or rel.get("tail"))
        from_type, from_name = by_uid.get(from_uid, ("", ""))
        to_type, to_name = by_uid.get(to_uid, ("", ""))
        if not from_type and "::" in from_uid:
            from_type, from_name = from_uid.split("::", 1)
        if not to_type and "::" in to_uid:
            to_type, to_name = to_uid.split("::", 1)
        if from_type and from_name and rel_type and to_type and to_name:
            relations.append(
                {
                    "from_type": from_type,
                    "from_name": from_name,
                    "rel_type": rel_type,
                    "to_type": to_type,
                    "to_name": to_name,
                }
            )
    return entities, relations


def entity_key(entity: dict[str, Any]) -> tuple[str, str] | None:
    etype = _safe_text(entity.get("type"))
    ename = _safe_text(entity.get("name"))
    if not etype or not ename:
        return None
    return (etype, ename)


def relation_key(relation: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    fields = (
        relation.get("from_type"),
        relation.get("from_name"),
        relation.get("rel_type"),
        relation.get("to_type"),
        relation.get("to_name"),
    )
    keys = tuple(_safe_text(field) for field in fields)
    if any(not key for key in keys):
        return None
    return keys  # type: ignore[return-value]


def calculate_metrics(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    accuracy = tp / (tp + fp + fn) if (tp + fp + fn) > 0 else 0.0
    return {
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "accuracy": round(accuracy, 6),
        "tp": int(tp),
        "fp": int(fp),
        "fn": int(fn),
    }


def score_entities_relations(
    golden_entities: list[dict[str, Any]],
    golden_relations: list[dict[str, Any]],
    predicted_entities: list[dict[str, Any]],
    predicted_relations: list[dict[str, Any]],
) -> dict[str, Any]:
    gold_ent = {key for item in golden_entities if (key := entity_key(item))}
    pred_ent = {key for item in predicted_entities if (key := entity_key(item))}
    gold_rel = {key for item in golden_relations if (key := relation_key(item))}
    pred_rel = {key for item in predicted_relations if (key := relation_key(item))}
    return {
        "entities": {
            "golden": len(gold_ent),
            "predicted": len(pred_ent),
            **calculate_metrics(len(gold_ent & pred_ent), len(pred_ent - gold_ent), len(gold_ent - pred_ent)),
        },
        "relations": {
            "golden": len(gold_rel),
            "predicted": len(pred_rel),
            **calculate_metrics(len(gold_rel & pred_rel), len(pred_rel - gold_rel), len(gold_rel - pred_rel)),
        },
    }


def _strip_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in item.items() if key != "evidence"}
        for item in items
        if isinstance(item, dict)
    ]


def _simple_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    simplified = []
    for entity in entities:
        simplified.append(
            {
                "type": entity.get("type"),
                "name": entity.get("name"),
                "properties": dict(entity.get("properties") or {}),
            }
        )
    return simplified


def _simple_relations(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    simplified = []
    for relation in relations:
        item = {
            "from_type": relation.get("from_type"),
            "from_name": relation.get("from_name"),
            "rel_type": relation.get("rel_type"),
            "to_type": relation.get("to_type"),
            "to_name": relation.get("to_name"),
        }
        properties = relation.get("properties")
        if isinstance(properties, dict) and properties:
            item["properties"] = dict(properties)
        simplified.append(item)
    return simplified


def _decode_judge_response(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    text = str(response).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM 评测返回必须是 JSON object")
    return data


def _judge_schema(collection_name: str) -> dict[str, Any]:
    pair_schema = {
        "type": "object",
        "properties": {
            "golden_index": {"type": "integer"},
            "predicted_index": {"type": "integer"},
        },
        "required": ["golden_index", "predicted_index"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {collection_name: {"type": "array", "items": pair_schema}},
        "required": [collection_name],
        "additionalProperties": False,
    }


def _build_judge_messages(
    collection_name: str,
    golden_items: list[dict[str, Any]],
    predicted_items: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if collection_name == "entity_matches":
        system = """你是知识抽取评测员。请判断 predicted 实体是否相对 golden 算“抽对了”。

判断规则：
1. 预测实体的 type 与 golden type 指向同一个本体类型即可，不要求字符串完全一致。
2. name 指向同一实体即可，允许同义表达、别名、规范名、标点/空格/全半角差异。
3. 同时比较 properties：两侧都出现的属性，键与值应一致（值允许同义/规范化差异，如日期格式、全半角、数值 100 与 100.0）；golden 没有的属性不会因为多出而判错，但关键属性缺失或冲突会降低匹配置信度。
4. 不比较 evidence。
5. 匹配必须是一对一：每个 golden 最多匹配一个 predicted，每个 predicted 最多匹配一个 golden。
6. 只要输出匹配 pair；没有匹配到的一律不要列出。
7. 输出 JSON，不要输出解释。"""
    else:
        system = """你是知识抽取评测员。请判断 predicted 关系是否相对 golden 算“抽对了”。

判断规则：
1. 预测关系的 rel_type 与 golden rel_type 等价即可，允许同义关系表达。
2. from/to 两个端点必须分别对应到 golden 中同一实体，方向不能反；端点用 type/name 判断（允许别名/同义）。
3. 若关系带有 properties，两侧都出现的属性键与值应一致（允许同义/规范化差异）。
4. 不比较 evidence。
5. 匹配必须是一对一：每个 golden 最多匹配一个 predicted，每个 predicted 最多匹配一个 golden。
6. 只要输出匹配 pair；没有匹配到的一律不要列出。
7. 输出 JSON，不要输出解释。"""
    golden_items = (
        _simple_entities(golden_items) if collection_name == "entity_matches" else _simple_relations(golden_items)
    )
    predicted_items = (
        _simple_entities(predicted_items) if collection_name == "entity_matches" else _simple_relations(predicted_items)
    )
    user = f"""请判断下面的 predicted 是否在 golden 中存在“抽对了”的对应项。

## golden items
{json.dumps(golden_items, ensure_ascii=False, indent=2)}

## predicted items
{json.dumps(predicted_items, ensure_ascii=False, indent=2)}

请只输出：
{{
  "{collection_name}": [
    {{"golden_index": 0, "predicted_index": 1}}
  ]
}}

golden_index 和 predicted_index 都从 0 开始；没有匹配的不要列出来。"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _unique_matches(raw_matches: Any) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    seen_golden: set[int] = set()
    seen_predicted: set[int] = set()
    if not isinstance(raw_matches, list):
        return pairs
    for item in raw_matches:
        if isinstance(item, dict):
            try:
                golden_idx = int(item.get("golden_index"))
                predicted_idx = int(item.get("predicted_index"))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            try:
                golden_idx = int(item[0])
                predicted_idx = int(item[1])
            except (TypeError, ValueError):
                continue
        else:
            continue
        if golden_idx < 0 or predicted_idx < 0:
            continue
        if golden_idx in seen_golden or predicted_idx in seen_predicted:
            continue
        seen_golden.add(golden_idx)
        seen_predicted.add(predicted_idx)
        pairs.append((golden_idx, predicted_idx))
    return pairs


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


class JudgeClient:
    """OpenAI-compatible chat client for LLM-judge. Stdlib only."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        max_tokens: int,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def _post_chat(self, messages: list[dict[str, str]], response_format: dict[str, Any] | None = None) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        status, _headers, body = http_request(
            _chat_completions_url(self.base_url),
            method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
        )
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"judge 响应不是 JSON HTTP {status}: {body[:300]!r}") from exc
        if status >= 400:
            error = data.get("error") if isinstance(data, dict) else None
            raise RuntimeError(error or data.get("info") or f"judge HTTP {status}")
        choice = ((data.get("choices") or [{}])[0]) if isinstance(data, dict) else {}
        message = choice.get("message") or {}
        content = message.get("content") or message.get("reasoning_content") or ""
        if isinstance(content, list):
            content = "".join(
                part.get("text") or "" if isinstance(part, dict) else str(part) for part in content
            )
        return str(content or "")

    def generate_structured(
        self,
        schema: dict[str, Any],
        messages: list[dict[str, str]],
        run_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mode = str((run_context or {}).get("response_mode") or "json_object")
        schema_name = str((run_context or {}).get("schema_name") or "knowledge_extract_judge")
        if mode == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        elif mode == "json_object":
            response_format = {"type": "json_object"}
        else:
            response_format = None
        return _decode_judge_response(self._post_chat(messages, response_format))


def _judge_one_batch(
    judge_client: JudgeClient,
    collection_name: str,
    golden_items: list[dict[str, Any]],
    batch_predicted_items: list[dict[str, Any]],
    predicted_start: int,
) -> list[tuple[int, int]]:
    messages = _build_judge_messages(collection_name, golden_items, batch_predicted_items)
    schema = _judge_schema(collection_name)
    errors: list[str] = []
    for response_mode in ("json_schema", "json_object", "plain"):
        try:
            data = judge_client.generate_structured(
                schema=schema,
                messages=messages,
                run_context={"schema_name": "knowledge_extract_judge", "response_mode": response_mode},
            )
            raw_matches = data.get(collection_name)
            if raw_matches is None:
                raise ValueError(f"响应缺少字段: {collection_name}")
            batch_pairs = _unique_matches(raw_matches)
            return [
                (g, predicted_start + p)
                for g, p in batch_pairs
                if g < len(golden_items) and p < len(batch_predicted_items)
            ]
        except Exception as exc:  # noqa: BLE001 - 按 v2 口径依次降级 json_schema/json_object/plain
            errors.append(f"{response_mode}: {type(exc).__name__}: {exc}")
    raise RuntimeError(
        f"LLM judge {collection_name} 失败，json_schema/json_object/plain 均未成功: " + " | ".join(errors)
    )


def _judge_collection(
    judge_client: JudgeClient,
    collection_name: str,
    golden_items: list[dict[str, Any]],
    predicted_items: list[dict[str, Any]],
    *,
    batch_size: int = 20,
) -> list[tuple[int, int]]:
    all_pairs: list[tuple[int, int]] = []
    if not golden_items or not predicted_items:
        return all_pairs
    for start in range(0, len(predicted_items), batch_size):
        batch = predicted_items[start : start + batch_size]
        all_pairs.extend(_judge_one_batch(judge_client, collection_name, golden_items, batch, start))
    return _unique_matches(all_pairs)


def llm_judge_score_case(
    golden_entities: list[dict[str, Any]],
    golden_relations: list[dict[str, Any]],
    predicted_entities: list[dict[str, Any]],
    predicted_relations: list[dict[str, Any]],
    judge_client: JudgeClient,
    *,
    batch_size: int = 20,
) -> dict[str, Any]:
    golden_entities = _strip_evidence(golden_entities)
    golden_relations = _strip_evidence(golden_relations)
    predicted_entities = _strip_evidence(predicted_entities)
    predicted_relations = _strip_evidence(predicted_relations)
    entity_pairs = [
        (g, p)
        for g, p in _judge_collection(
            judge_client, "entity_matches", golden_entities, predicted_entities, batch_size=batch_size
        )
        if g < len(golden_entities) and p < len(predicted_entities)
    ]
    relation_pairs = [
        (g, p)
        for g, p in _judge_collection(
            judge_client, "relation_matches", golden_relations, predicted_relations, batch_size=batch_size
        )
        if g < len(golden_relations) and p < len(predicted_relations)
    ]
    return {
        "entities": {
            "golden": len(golden_entities),
            "predicted": len(predicted_entities),
            **calculate_metrics(
                len(entity_pairs),
                max(0, len(predicted_entities) - len(entity_pairs)),
                max(0, len(golden_entities) - len(entity_pairs)),
            ),
        },
        "relations": {
            "golden": len(golden_relations),
            "predicted": len(predicted_relations),
            **calculate_metrics(
                len(relation_pairs),
                max(0, len(predicted_relations) - len(relation_pairs)),
                max(0, len(golden_relations) - len(relation_pairs)),
            ),
        },
        "judge_pairs": {
            "entity_matches": entity_pairs,
            "relation_matches": relation_pairs,
        },
    }


def build_judge_client(params: dict[str, Any]) -> JudgeClient:
    eval_cfg = params.get("eval") or {}
    base_url = (
        os.environ.get("KG_EXTRACT_LLM_BASE_URL")
        or os.environ.get("QASYSTEM_JUDGE_BASE_URL")
        or eval_cfg.get("judge_base_url")
        or ""
    ).rstrip("/")
    model = (
        os.environ.get("KG_EXTRACT_LLM_MODEL")
        or os.environ.get("QASYSTEM_JUDGE_MODEL")
        or eval_cfg.get("judge_model")
        or "deepseek-v4-flash"
    )
    api_key = os.environ.get("KG_EXTRACT_LLM_API_KEY") or os.environ.get("QASYSTEM_JUDGE_API_KEY") or ""
    if not base_url:
        raise RuntimeError(
            "LLM-judge 需要 KG_EXTRACT_LLM_BASE_URL（或 params.eval.judge_base_url）。"
            "抽取评测默认走大模型匹配，不能只靠 HTTP 沉淀接口。"
        )
    timeout = float(eval_cfg.get("judge_timeout_seconds") or os.environ.get("KG_EXTRACT_LLM_TIMEOUT_SECONDS") or 300)
    max_tokens = int(eval_cfg.get("judge_max_tokens") or os.environ.get("KG_EXTRACT_LLM_MAX_TOKENS") or 8192)
    return JudgeClient(
        base_url=base_url,
        api_key=api_key,
        model=str(model),
        timeout=max(30.0, timeout),
        max_tokens=max(256, max_tokens),
    )


def score_against_golden(
    golden: dict[str, Any],
    predicted: Any,
    *,
    match_mode: str = "llm",
    judge_client: JudgeClient | None = None,
    batch_size: int = 20,
) -> dict[str, Any]:
    gold_ent, gold_rel = normalize_graph(golden)
    pred_ent, pred_rel = normalize_graph(predicted)
    identity = score_entities_relations(gold_ent, gold_rel, pred_ent, pred_rel)
    predicted_graph = graph_from_entities(pred_ent, pred_rel)
    if match_mode != "llm":
        identity["match_mode"] = "identity"
        identity["predicted_graph"] = predicted_graph
        return identity
    if judge_client is None:
        raise RuntimeError("match_mode=llm 但没有 judge client")
    try:
        metrics = llm_judge_score_case(
            gold_ent,
            gold_rel,
            pred_ent,
            pred_rel,
            judge_client,
            batch_size=batch_size,
        )
        metrics["match_mode"] = "llm"
        metrics["identity"] = identity
        metrics["predicted_graph"] = predicted_graph
        return metrics
    except Exception as exc:  # noqa: BLE001 - judge 失败时保留 identity 对照，并标明降级
        log(f"LLM-judge 失败，降级 identity：{exc}")
        identity["match_mode"] = "identity_fallback"
        identity["judge_error"] = str(exc)
        identity["predicted_graph"] = predicted_graph
        return identity


def maybe_score(
    golden: dict[str, Any] | None,
    predicted: Any,
    score_ctx: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if golden is None:
        return None
    ctx = score_ctx or {}
    return score_against_golden(
        golden,
        predicted,
        match_mode=str(ctx.get("match_mode") or "llm"),
        judge_client=ctx.get("judge_client"),
        batch_size=int(ctx.get("batch_size") or 20),
    )


def empty_metrics() -> dict[str, Any]:
    zero = calculate_metrics(0, 0, 0)
    return {
        "entities": {"golden": 0, "predicted": 0, **zero},
        "relations": {"golden": 0, "predicted": 0, **zero},
    }


def add_metrics(total: dict[str, Any], item: dict[str, Any] | None) -> None:
    if not item:
        return
    for name in ("entities", "relations"):
        src = item.get(name) or {}
        dst = total.setdefault(name, {"golden": 0, "predicted": 0, "tp": 0, "fp": 0, "fn": 0})
        for key in ("golden", "predicted", "tp", "fp", "fn"):
            dst[key] = int(dst.get(key) or 0) + int(src.get(key) or 0)


def finalize_metrics(total: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("entities", "relations"):
        src = total.get(name) or {"golden": 0, "predicted": 0, "tp": 0, "fp": 0, "fn": 0}
        result[name] = {
            "golden": src.get("golden", 0),
            "predicted": src.get("predicted", 0),
            **calculate_metrics(int(src.get("tp") or 0), int(src.get("fp") or 0), int(src.get("fn") or 0)),
        }
    return result


def merge_graphs(graphs: list[dict[str, Any]]) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for graph in graphs:
        items, rels = normalize_graph(graph)
        entities.extend(items)
        relations.extend(rels)
    return graph_from_entities(entities, relations)


def _payload_layers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    layers = [payload]
    current = payload
    for _ in range(5):
        nxt = current.get("data") if isinstance(current.get("data"), dict) else None
        if not isinstance(nxt, dict):
            break
        layers.append(nxt)
        current = nxt
    return layers


def queued_job_id(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    for item in _payload_layers(payload):
        job_id = str(item.get("job_id") or "")
        status = str(item.get("status") or "")
        if job_id and (item.get("accepted") or status in {"queued", "running", "accepted"}):
            return job_id
    return ""


def extract_graph_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        return {"nodes": [], "relationships": []}
    for data in _payload_layers(envelope):
        extraction = data.get("extraction") if isinstance(data.get("extraction"), dict) else {}
        graph = data.get("graph") or extraction.get("graph")
        if graph is None and data.get("graphs"):
            graph = merge_graphs(list(data.get("graphs") or []))
        if isinstance(graph, dict):
            entities, relations = normalize_graph(graph)
            return graph_from_entities(entities, relations)
        entities, relations = normalize_graph(data)
        if entities or relations:
            return graph_from_entities(entities, relations)
    return {"nodes": [], "relationships": []}


# --- backend HTTP ---


def parse_document(base_url: str, source: Path, options: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", PARSE_PATH.lstrip("/"))
    body, content_type = encode_multipart(
        [("options", json.dumps(options.get("parser") or {}, ensure_ascii=False))],
        [("file", source.name, source.read_bytes(), guess_content_type(source))],
    )
    log(f"POST {PARSE_PATH} ← {source.name}")
    return submit_and_wait(
        url,
        base_url=base_url,
        data=body,
        headers={**auth_headers(), "Content-Type": content_type},
        params=params,
    )


def chunk_document(base_url: str, markdown: str, file_name: str, options: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", CHUNK_PATH.lstrip("/"))
    payload = {
        "file_name": file_name,
        "markdown": markdown,
        "options": options.get("chunking") or {},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    log(f"POST {CHUNK_PATH} ← {file_name}")
    return submit_and_wait(
        url,
        base_url=base_url,
        data=body,
        headers={**auth_headers(), "Content-Type": "application/json"},
        params=params,
    )


def extract_graphs(base_url: str, ontology: str, items: list[dict[str, Any]], options: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", EXTRACT_PATH.lstrip("/"))
    payload = {
        "ontology": ontology,
        "items": [{"text": item.get("text") or ""} for item in items],
        "options": options.get("extraction") or {},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    log(f"POST {EXTRACT_PATH} items={len(payload['items'])}")
    return submit_and_wait(
        url,
        base_url=base_url,
        data=body,
        headers={**auth_headers(), "Content-Type": "application/json"},
        params=params,
    )


def ingest_file(base_url: str, source: Path, ontology: str, options: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", INGEST_PATH.lstrip("/"))
    body, content_type = encode_multipart(
        [("ontology", ontology), ("options", json.dumps(options, ensure_ascii=False))],
        [("file", source.name, source.read_bytes(), guess_content_type(source))],
    )
    log(f"POST {INGEST_PATH} ← {source.name}")
    return submit_and_wait(
        url,
        base_url=base_url,
        data=body,
        headers={**auth_headers(), "Content-Type": content_type},
        params=params,
    )


# --- plugin MCP ---


def _parse_sse_json(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", "replace")
    frames: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            frames.append(parsed)
    for item in reversed(frames):
        if "result" in item or "error" in item:
            return item
    return frames[-1] if frames else decode_json_body(body)


def mcp_rpc(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: float,
    session_id: str | None = None,
    protocol_version: str = "2025-03-26",
) -> tuple[dict[str, Any], str | None]:
    request_headers = {
        **headers,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol_version,
    }
    if session_id:
        request_headers["mcp-session-id"] = session_id
        request_headers["Mcp-Session-Id"] = session_id
    status, response_headers, body = http_request(
        url,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
        timeout=timeout,
    )
    next_session = response_headers.get("mcp-session-id") or session_id
    content_type = response_headers.get("content-type") or ""
    parsed = _parse_sse_json(body) if "event-stream" in content_type or body.startswith(b"event:") else decode_json_body(body)
    if status >= 400:
        raise RuntimeError(parsed.get("info") or parsed.get("error") or f"MCP HTTP {status}")
    if parsed.get("error"):
        raise RuntimeError(json.dumps(parsed.get("error"), ensure_ascii=False))
    return parsed, next_session


def extract_mcp_tool_payload(rpc_result: dict[str, Any]) -> dict[str, Any]:
    result = rpc_result.get("result") if isinstance(rpc_result.get("result"), dict) else rpc_result
    if not isinstance(result, dict):
        return {}
    structured = result.get("structuredContent") or result.get("structured_content")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    if "code" in result or "data" in result or "graph" in result:
        return result
    return {}


def plugin_extract(
    mcp_url: str,
    ontology: str,
    text: str,
    options: dict[str, Any],
    params: dict[str, Any],
    *,
    base_url: str,
) -> dict[str, Any]:
    timeout = max(60.0, float(params.get("poll_timeout_seconds") or 3600))
    headers = auth_headers()
    log(f"MCP {MCP_TOOL_NAME} ← text_len={len(text)}")
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "offline_ingestion", "version": "2.0"},
        },
    }
    init_result, session_id = mcp_rpc(mcp_url, init_payload, headers=headers, timeout=timeout)
    protocol_version = str(
        ((init_result.get("result") or {}).get("protocolVersion") if isinstance(init_result.get("result"), dict) else None)
        or "2025-03-26"
    )
    mcp_rpc(
        mcp_url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
        timeout=timeout,
        session_id=session_id,
        protocol_version=protocol_version,
    )
    extraction = options.get("extraction") or {}
    call_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": MCP_TOOL_NAME,
            "arguments": {
                "ontology": ontology,
                "text": text,
                "model": extraction.get("model"),
            },
        },
    }
    rpc_result, _session = mcp_rpc(
        mcp_url,
        call_payload,
        headers=headers,
        timeout=timeout,
        session_id=session_id,
        protocol_version=protocol_version,
    )
    tool = extract_mcp_tool_payload(rpc_result)
    job_id = queued_job_id(tool) or queued_job_id(rpc_result)
    if job_id:
        log(f"MCP 已入队 job_id={job_id}，轮询 Backend /api/v1/jobs")
        job_payload = wait_for_job(base_url, job_id, params)
        tool = extract_mcp_tool_payload(job_payload) or job_payload
    if tool.get("code") not in (None, 0):
        raise RuntimeError(tool.get("message") or json.dumps(tool.get("error"), ensure_ascii=False) or "MCP 抽取失败")
    graph = extract_graph_from_envelope(tool)
    if not graph.get("nodes") and not graph.get("relationships"):
        graph = extract_graph_from_envelope(rpc_result)
    return {"rpc": rpc_result, "tool": tool, "graph": graph}


def format_metrics_line(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return "未评分"
    ent = metrics.get("entities") or {}
    rel = metrics.get("relations") or {}
    mode = metrics.get("match_mode") or ""
    prefix = f"{mode} " if mode else ""
    line = (
        f"{prefix}实体 P={ent.get('precision', 0):.3f} R={ent.get('recall', 0):.3f} "
        f"F1={ent.get('f1', 0):.3f} Acc={ent.get('accuracy', 0):.3f} "
        f"关系 P={rel.get('precision', 0):.3f} R={rel.get('recall', 0):.3f} "
        f"F1={rel.get('f1', 0):.3f} Acc={rel.get('accuracy', 0):.3f}"
    )
    identity = metrics.get("identity")
    if isinstance(identity, dict) and identity.get("entities"):
        ident_ent = identity.get("entities") or {}
        ident_rel = identity.get("relations") or {}
        line += (
            f" | identity 实体 F1={ident_ent.get('f1', 0):.3f} "
            f"关系 F1={ident_rel.get('f1', 0):.3f}"
        )
    return line


def run_backend_case(
    *,
    base_url: str,
    ontology: str,
    sources: list[Path],
    options: dict[str, Any],
    params: dict[str, Any],
    golden: dict[str, Any] | None,
    out_dir: Path,
    score_ctx: dict[str, Any] | None = None,
    run_system: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "system": {"status": "skipped", "message": "默认不跑 knowledge-ingestion；需要时加 --system"},
        "modules": {
            "parse": {"status": "failed"},
            "chunk": {"status": "failed"},
            "extract": {"status": "failed"},
        },
    }
    markdown_parts: list[str] = []
    parse_failures: list[dict[str, Any]] = []
    for source in sources:
        try:
            envelope = parse_document(base_url, source, options, params)
            write_json(out_dir / f"parse_{source.stem}.json", envelope)
            data = envelope.get("data") or {}
            parsed = str(data.get("markdown") or "")
            if not parsed.strip():
                raise RuntimeError(f"解析结果为空: {source.name}")
            markdown_parts.append(parsed)
        except (URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            failed = fail_payload(exc)
            failed["file"] = source.name
            parse_failures.append(failed)
            log(f"模块 parse 失败 {source.name}: {failed['message']}")
    markdown = "\n\n".join(markdown_parts)
    items: list[dict[str, Any]] = []
    if markdown.strip():
        write_text(out_dir / "document.md", markdown)
        result["modules"]["parse"] = {
            "status": "partial" if parse_failures else "succeeded",
            "files": len(sources),
            "parsed": len(markdown_parts),
            "content_length": len(markdown),
            "failed_files": parse_failures,
        }
    else:
        result["modules"]["parse"] = {
            "status": "failed",
            "files": len(sources),
            "parsed": 0,
            "message": "docs/ 全部解析失败或结果为空",
            "failed_files": parse_failures,
        }
        result["modules"]["chunk"] = {"status": "skipped", "message": "解析无可用 Markdown"}
        result["modules"]["extract"] = {"status": "skipped", "message": "解析无可用 Markdown"}

    if markdown.strip():
        try:
            file_name = sources[0].with_suffix(".md").name if sources else "document.md"
            envelope = chunk_document(base_url, markdown, file_name, options, params)
            write_json(out_dir / "chunks.json", envelope)
            data = envelope.get("data") or {}
            items = list(data.get("items") or [])
            if not items:
                raise RuntimeError("切片结果为空")
            result["modules"]["chunk"] = {
                "status": "succeeded",
                "chunk_count": data.get("count") if data.get("count") is not None else len(items),
            }
        except (URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            result["modules"]["chunk"] = fail_payload(exc)
            log(f"模块 chunk 失败: {result['modules']['chunk']['message']}")
            items = [{"text": markdown, "range": [0, len(markdown)]}]

        try:
            envelope = extract_graphs(base_url, ontology, items, options, params)
            write_json(out_dir / "backend_extract.json", envelope)
            graph = extract_graph_from_envelope(envelope)
            metrics = maybe_score(golden, graph, score_ctx)
            result["modules"]["extract"] = {
                "status": "succeeded",
                "nodes": len(graph.get("nodes") or []),
                "relationships": len(graph.get("relationships") or []),
                "metrics": metrics,
            }
            log(f"模块 extract {format_metrics_line(metrics)}")
        except (URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            result["modules"]["extract"] = fail_payload(exc)
            log(f"模块 extract 失败: {result['modules']['extract']['message']}")

    if run_system:
        graphs: list[dict[str, Any]] = []
        ingest_failures: list[dict[str, Any]] = []
        for source in sources:
            try:
                envelope = ingest_file(base_url, source, ontology, options, params)
                write_json(out_dir / f"backend_system_{source.stem}.json", envelope)
                graphs.append(extract_graph_from_envelope(envelope))
            except (URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                failed = fail_payload(exc)
                failed["file"] = source.name
                ingest_failures.append(failed)
                log(f"系统 knowledge-ingestion 失败 {source.name}: {failed['message']}")
        if graphs:
            graph = merge_graphs(graphs) if len(graphs) > 1 else graphs[0]
            write_json(out_dir / "backend_system_graph.json", graph)
            metrics = maybe_score(golden, graph, score_ctx)
            result["system"] = {
                "status": "partial" if ingest_failures else "succeeded",
                "nodes": len(graph.get("nodes") or []),
                "relationships": len(graph.get("relationships") or []),
                "metrics": metrics,
                "files": len(sources),
                "ingested": len(graphs),
                "failed_files": ingest_failures,
            }
            log(f"系统 knowledge-ingestion {format_metrics_line(metrics)}")
        else:
            result["system"] = {
                "status": "failed",
                "message": "全部文件知识沉淀失败",
                "files": len(sources),
                "failed_files": ingest_failures,
            }
    result["markdown"] = markdown
    result["chunk_items"] = []
    return result


def run_plugin_case(
    *,
    mcp_url: str,
    base_url: str,
    ontology: str,
    markdown: str,
    options: dict[str, Any],
    params: dict[str, Any],
    golden: dict[str, Any] | None,
    out_dir: Path,
    score_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        payload = plugin_extract(
            mcp_url, ontology, markdown, options, params, base_url=base_url
        )
        write_json(out_dir / "plugin_extract.json", payload)
        graph = payload.get("graph") or {"nodes": [], "relationships": []}
        metrics = maybe_score(golden, graph, score_ctx)
        log(f"插件 {MCP_TOOL_NAME} {format_metrics_line(metrics)}")
        return {
            "status": "succeeded",
            "nodes": len(graph.get("nodes") or []),
            "relationships": len(graph.get("relationships") or []),
            "metrics": metrics,
        }
    except (URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        failed = fail_payload(exc)
        write_json(out_dir / "plugin_error.json", failed)
        log(f"插件抽取失败: {failed['message']}")
        return failed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="知识沉淀评测：默认只测后台解析/切片/抽取。可直接传 case/类型目录。",
        epilog="示例: python3 offline_ingestion/run.py offline_ingestion/公文/关于开展OpenClaw开源智能体安全风险排查工作的通知",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="路径",
        help="评测目录，如 公文/<case> 或 offline_ingestion/公文/<case>；传类型目录则跑该类型全部 case",
    )
    parser.add_argument("--workspace", default=str(KIT_DIR), help="工作区根目录")
    parser.add_argument("--params", default=None, help="参数 YAML，默认工作区 params.yaml")
    parser.add_argument("--groups", default="", help="只处理指定类型，逗号分隔，如 公文,标准,规则")
    parser.add_argument("--cases", default="", help="只处理指定 case 目录名，逗号分隔")
    parser.add_argument(
        "--stage",
        choices=("all", "backend", "plugin"),
        default="backend",
        help="backend=只测后台解析/切片/抽取（默认）；all=后台后再跑插件；plugin=只测插件",
    )
    parser.add_argument(
        "--system",
        action="store_true",
        help="额外跑 POST /api/v1/pipelines/knowledge-ingestion 整条沉淀；默认不跑",
    )
    parser.add_argument(
        "--match-mode",
        choices=("llm", "identity"),
        default=None,
        help="抽取评分：llm=大模型判断是否抽对（params 默认）；identity=type+name 精确匹配。未配 judge 地址时自动改 identity",
    )
    parser.add_argument("--min-f1", type=float, default=0.0, help="已评分 case 的最低实体 F1，低于则退出码 3")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只扫描评测集（类型/case/原文件/golden/本体），不调 Backend 或 judge",
    )
    return parser.parse_args()


def stage_failed(block: dict[str, Any] | None) -> bool:
    if not block:
        return False
    if block.get("status") == "failed":
        return True
    modules = block.get("modules")
    if isinstance(modules, dict):
        return any(item.get("status") == "failed" for item in modules.values() if isinstance(item, dict))
    return False


def dry_run_inventory(
    workspace: Path,
    types: list[Path],
    case_filter: list[str],
    group_cfg: dict[str, Any],
    selection: dict[str, set[str] | None] | None = None,
) -> int:
    log(f"dry-run 工作区: {workspace}")
    found = False
    for type_dir in types:
        cases = discover_cases(
            type_dir,
            case_names_for_type(type_dir.name, selection=selection, case_filter=case_filter),
        )
        log(f"类型 {type_dir.name}: {len(cases)} 个 case")
        for case_dir in cases:
            found = True
            sources = collect_source_files(case_dir / "docs")
            _golden_path, golden, golden_reason = load_golden(case_dir)
            try:
                ontology_path = resolve_ontology_path(workspace, type_dir, case_dir, group_cfg)
                ontology_note = str(ontology_path.relative_to(workspace)) if ontology_path.exists() else f"缺失 {ontology_path}"
            except Exception as exc:  # noqa: BLE001
                ontology_note = f"读取失败: {exc}"
            gold_ent, gold_rel = normalize_graph(golden) if golden else ([], [])
            scored = "评分" if golden else f"不评分 ({golden_reason})"
            log(
                f"  {type_dir.name}/{case_dir.name} files={len(sources)} "
                f"golden={len(gold_ent)}e/{len(gold_rel)}r {scored} ontology={ontology_note}"
            )
            if not sources:
                log("    警告: docs/ 中没有原文件")
    if not found:
        log("未发现任何 case（需要 <类型>/<case>/docs）")
        return 1
    return 0


def lowest_entity_f1(record: dict[str, Any]) -> float | None:
    values: list[float] = []
    backend = record.get("backend") or {}
    for block in (backend.get("system"), (backend.get("modules") or {}).get("extract"), record.get("plugin")):
        metrics = (block or {}).get("metrics") if isinstance(block, dict) else None
        if metrics and "entities" in metrics:
            values.append(float((metrics["entities"] or {}).get("f1") or 0.0))
    return min(values) if values else None


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    params_path = Path(args.params).expanduser().resolve() if args.params else workspace / "params.yaml"
    group_filter = [item.strip() for item in str(args.groups or "").split(",") if item.strip()]
    case_filter = [item.strip() for item in str(args.cases or "").split(",") if item.strip()]
    if args.targets and (group_filter or case_filter):
        log("路径与 --groups/--cases 不能同时使用，请只保留一种筛选方式")
        return 1
    try:
        selection = selection_from_targets(workspace, list(args.targets)) if args.targets else None
    except (FileNotFoundError, ValueError) as exc:
        log(str(exc))
        return 1
    try:
        params = load_params(params_path)
    except RuntimeError as exc:
        log(str(exc))
        return 1
    base_url = (os.environ.get("QASYSTEM_BACKEND_URL") or params.get("backend_url") or DEFAULT_PARAMS["backend_url"]).rstrip("/")
    mcp_url = (
        os.environ.get("QASYSTEM_MCP_URL")
        or params.get("mcp_url")
        or urljoin(base_url + "/", "mcp")
    )
    options = params.get("options") or {}
    group_cfg = params.get("groups") or {}
    eval_cfg = params.get("eval") or {}
    match_mode = str(args.match_mode or eval_cfg.get("match_mode") or "llm").strip().lower()
    if match_mode not in {"llm", "identity"}:
        log(f"不支持的 match_mode: {match_mode}")
        return 1
    types = discover_types(workspace, list(selection.keys()) if selection is not None else (group_filter or None))
    if not types:
        log("未发现任何类型目录")
        return 1
    if args.dry_run:
        return dry_run_inventory(workspace, types, case_filter, group_cfg, selection=selection)

    score_ctx: dict[str, Any] = {
        "match_mode": match_mode,
        "judge_client": None,
        "batch_size": int(eval_cfg.get("judge_batch_size") or 20),
    }
    if match_mode == "llm":
        try:
            score_ctx["judge_client"] = build_judge_client(params)
        except RuntimeError as exc:
            if args.match_mode == "llm":
                log(str(exc))
                return 1
            log(f"{exc} 未配置 judge，改用 identity。需要 LLM-judge 时加 --match-mode llm 并设置 KG_EXTRACT_LLM_BASE_URL。")
            match_mode = "identity"
            score_ctx["match_mode"] = match_mode
    run_backend = args.stage in {"all", "backend"}
    run_plugin = args.stage in {"all", "plugin"}

    log(f"工作区: {workspace}")
    log(f"参数文件: {params_path}")
    log(f"Backend: {base_url}")
    if run_plugin:
        log(f"MCP: {mcp_url}")
    if selection is not None:
        labels: list[str] = []
        for group, cases in selection.items():
            if cases is None:
                labels.append(f"{group}/*")
            else:
                labels.extend(f"{group}/{name}" for name in sorted(cases))
        log(f"目标: {', '.join(labels)}")
    log(f"阶段: {' → '.join(name for name, flag in (('后台', run_backend), ('插件', run_plugin)) if flag)}")
    log("沉淀: knowledge-ingestion" if args.system else "沉淀: 跳过（停在抽取评分；需要时加 --system）")
    if match_mode == "llm":
        judge = score_ctx["judge_client"]
        log(f"评分: LLM-judge model={judge.model} base_url={judge.base_url}")
    else:
        log("评分: identity（type+name 精确匹配）")

    started = datetime.now()
    run_id = started.strftime("%Y%m%d-%H%M%S")
    run_dir = workspace / "_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = dict(params)
    snapshot["backend_url"] = base_url
    snapshot["mcp_url"] = mcp_url
    snapshot["stage"] = args.stage
    snapshot["match_mode"] = match_mode
    snapshot["targets"] = list(args.targets)
    snapshot["system"] = bool(args.system)
    if match_mode == "llm":
        snapshot.setdefault("eval", {})
        snapshot["eval"] = dict(eval_cfg)
        snapshot["eval"]["judge_model"] = score_ctx["judge_client"].model
        snapshot["eval"]["judge_base_url"] = score_ctx["judge_client"].base_url
    write_json(run_dir / "params.resolved.json", snapshot)
    if params_path.is_file():
        (run_dir / "params.yaml").write_text(params_path.read_text(encoding="utf-8"), encoding="utf-8")

    summary: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "workspace": str(workspace),
        "backend_url": base_url,
        "mcp_url": mcp_url,
        "stage": args.stage,
        "match_mode": match_mode,
        "groups": [],
        "cases": [],
        "succeeded": 0,
        "failed": 0,
        "unscored": 0,
        "overall": {
            "backend_system": empty_metrics(),
            "backend_extract": empty_metrics(),
            "plugin_extract": empty_metrics(),
        },
    }
    running = {
        "backend_system": {"entities": {}, "relations": {}},
        "backend_extract": {"entities": {}, "relations": {}},
        "plugin_extract": {"entities": {}, "relations": {}},
    }
    running_identity = {
        "backend_system": {"entities": {}, "relations": {}},
        "backend_extract": {"entities": {}, "relations": {}},
        "plugin_extract": {"entities": {}, "relations": {}},
    }

    found_cases = False
    for type_dir in types:
        cases = discover_cases(
            type_dir,
            case_names_for_type(type_dir.name, selection=selection, case_filter=case_filter),
        )
        log(f"类型 {type_dir.name}: {len(cases)} 个 case")
        summary["groups"].append({"name": type_dir.name, "cases": len(cases)})
        for case_dir in cases:
            found_cases = True
            sources = collect_source_files(case_dir / "docs")
            golden_path, golden, golden_reason = load_golden(case_dir)
            out_dir = run_dir / type_dir.name / case_dir.name
            out_dir.mkdir(parents=True, exist_ok=True)
            record: dict[str, Any] = {
                "group": type_dir.name,
                "case": case_dir.name,
                "files": [path.name for path in sources],
                "golden": str(golden_path) if golden_path else None,
                "scored": golden is not None,
                "status": "failed",
                "backend": None,
                "plugin": None,
                "error": None,
            }
            log(f"开始 {type_dir.name}/{case_dir.name} files={len(sources)} golden={'yes' if golden else golden_reason}")
            if golden and match_mode == "llm":
                gold_ent, gold_rel = normalize_graph(golden)
                if len(gold_ent) + len(gold_rel) > 150:
                    log(
                        f"提示: golden 较大（{len(gold_ent)} 实体 / {len(gold_rel)} 关系），"
                        "LLM-judge 会多次调用且较慢；可先 --match-mode identity 看精确匹配"
                    )
            if not sources:
                record["error"] = "docs/ 中没有原文件"
                summary["cases"].append(record)
                summary["failed"] += 1
                continue
            try:
                ontology = read_ontology(resolve_ontology_path(workspace, type_dir, case_dir, group_cfg))
            except Exception as exc:  # noqa: BLE001 - 单个 case 失败后继续
                record["error"] = str(exc)
                summary["cases"].append(record)
                summary["failed"] += 1
                log(f"读取本体失败: {exc}")
                continue

            markdown = ""
            if run_backend:
                backend = run_backend_case(
                    base_url=base_url,
                    ontology=ontology,
                    sources=sources,
                    options=options,
                    params=params,
                    golden=golden,
                    out_dir=out_dir,
                    score_ctx=score_ctx,
                    run_system=bool(args.system),
                )
                markdown = str(backend.pop("markdown", "") or "")
                backend.pop("chunk_items", None)
                record["backend"] = backend
                add_metrics(running["backend_system"], (backend.get("system") or {}).get("metrics"))
                add_metrics(running["backend_extract"], ((backend.get("modules") or {}).get("extract") or {}).get("metrics"))
                add_metrics(
                    running_identity["backend_system"],
                    ((backend.get("system") or {}).get("metrics") or {}).get("identity"),
                )
                add_metrics(
                    running_identity["backend_extract"],
                    (((backend.get("modules") or {}).get("extract") or {}).get("metrics") or {}).get("identity"),
                )
            if run_plugin:
                if not markdown:
                    try:
                        parts = []
                        for source in sources:
                            envelope = parse_document(base_url, source, options, params)
                            parts.append(str((envelope.get("data") or {}).get("markdown") or ""))
                        markdown = "\n\n".join(part for part in parts if part.strip())
                        write_text(out_dir / "document.md", markdown)
                    except (URLError, TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                        record["plugin"] = fail_payload(exc)
                        log(f"插件阶段解析失败: {record['plugin']['message']}")
                if markdown.strip() and (record.get("plugin") or {}).get("status") != "failed":
                    record["plugin"] = run_plugin_case(
                        mcp_url=mcp_url,
                        base_url=base_url,
                        ontology=ontology,
                        markdown=markdown,
                        options=options,
                        params=params,
                        golden=golden,
                        out_dir=out_dir,
                        score_ctx=score_ctx,
                    )
                    add_metrics(running["plugin_extract"], (record["plugin"] or {}).get("metrics"))
                    add_metrics(
                        running_identity["plugin_extract"],
                        ((record["plugin"] or {}).get("metrics") or {}).get("identity"),
                    )
            if golden is None:
                record["unscored_reason"] = golden_reason
                summary["unscored"] += 1
            failed = stage_failed(record.get("backend")) or stage_failed(record.get("plugin")) or bool(record.get("error"))
            record["status"] = "failed" if failed else "succeeded"
            if failed:
                summary["failed"] += 1
            else:
                summary["succeeded"] += 1
            write_json(out_dir / "scores.json", record)
            summary["cases"].append(record)
            log(f"完成 {type_dir.name}/{case_dir.name}: status={record['status']}")

    if not found_cases:
        log("未发现任何 case（需要 <类型>/<case>/docs 与同级 extract/golden.json）")
        return 1

    summary["overall"] = {key: finalize_metrics(value) for key, value in running.items()}
    if match_mode == "llm":
        summary["overall_identity"] = {key: finalize_metrics(value) for key, value in running_identity.items()}
    summary["finished_at"] = datetime.now().isoformat()
    write_json(run_dir / "summary.json", summary)

    print("\n---------- 评测总览 ----------")
    print(f"评分口径: {match_mode}（精确率 P / 召回率 R / F1 / 准确率 Acc）")
    overview = [
        ("backend_extract", "后台模块 graphs/extract"),
        ("plugin_extract", f"插件 {MCP_TOOL_NAME}"),
    ]
    if args.system:
        overview.insert(0, ("backend_system", "后台系统 knowledge-ingestion"))
    for key, title in overview:
        print(f"{title}: {format_metrics_line(summary['overall'][key])}")
        identity_overall = (summary.get("overall_identity") or {}).get(key)
        if identity_overall:
            print(f"  identity 对照: {format_metrics_line(identity_overall)}")
    print(
        f"cases succeeded={summary['succeeded']} failed={summary['failed']} "
        f"unscored={summary['unscored']} → {run_dir / 'summary.json'}"
    )

    if summary["failed"]:
        return 2
    if args.min_f1 > 0:
        below = [
            f"{item['group']}/{item['case']}"
            for item in summary["cases"]
            if item.get("scored") and (lowest_entity_f1(item) is not None) and (lowest_entity_f1(item) or 0) < args.min_f1
        ]
        if below:
            log(f"实体 F1 低于 {args.min_f1}: {', '.join(below)}")
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
