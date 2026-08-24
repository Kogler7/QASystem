#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""knowledge_extract_v2 benchmark 公共评测实现。"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # local 直连算法模式不依赖 requests
    requests = None

ENDPOINT = "/api/v1/pipelines/knowledge-ingestion"
EMBEDDING_ENDPOINT = "/api/v1/embeddings/texts"
LOCAL_ENDPOINT = "algorithm.knowledge_extract_v2.extract_harness_items"
DEFAULT_BASE_URL = "http://127.0.0.1:8006"
DEFAULT_SEMANTIC_THRESHOLD = 0.80

ROOT = Path(__file__).resolve().parent
ROOT_ENV_FILE = ROOT / ".env"
V2_BENCHMARK = ROOT / "algorithm" / "knowledge_extract_v2" / "benchmark"
BENCHMARK_ROOTS = {
    "official": V2_BENCHMARK / "official",
    "simple": V2_BENCHMARK / "simple",
    "standard": V2_BENCHMARK / "standard",
}

ONTOLOGY_SUFFIXES = {".puml", ".uml", ".plantuml"}
DOC_SUFFIXES = {".pdf", ".docx", ".doc", ".txt", ".md"}

_DASHES = str.maketrans({"—": "-", "–": "-", "－": "-", "﹣": "-"})
_CJK_SPACE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")


def canonical(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    text = _CJK_SPACE.sub("", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def canonical_json(value: Any) -> str:
    if isinstance(value, dict):
        value = {str(k): v for k, v in value.items()}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def entity_identity_key(entity: dict[str, Any]) -> tuple[str, str]:
    return canonical(entity.get("type")), canonical(entity.get("name"))


def entity_full_key(entity: dict[str, Any]) -> tuple[str, str, str]:
    return (
        canonical(entity.get("type")),
        canonical(entity.get("name")),
        canonical_json(entity.get("properties") or {}),
    )


def relation_key(relation: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        canonical(relation.get("from_type")),
        canonical(relation.get("from_name")),
        canonical(relation.get("rel_type")),
        canonical(relation.get("to_type")),
        canonical(relation.get("to_name")),
    )


def prf1(predicted: set[Any], golden: set[Any]) -> dict[str, float]:
    tp = len(predicted & golden)
    fp = len(predicted - golden)
    fn = len(golden - predicted)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": float(tp), "fp": float(fp), "fn": float(fn),
            "precision": precision, "recall": recall, "f1": f1}


def aggregate(items: list[dict[str, Any]]) -> dict[str, float]:
    if not items:
        return {"tp": 0.0, "fp": 0.0, "fn": 0.0,
                "precision": 0.0, "recall": 0.0, "f1": 0.0}
    tp = sum(item["tp"] for item in items)
    fp = sum(item["fp"] for item in items)
    fn = sum(item["fn"] for item in items)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": precision, "recall": recall, "f1": f1}


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def get_embeddings(base_url: str, texts: list[str], timeout: float = 180.0) -> list[list[float]]:
    if not texts:
        return []
    if requests is None:
        raise RuntimeError("semantic 后端向量匹配需要 requests，请先 pip install requests，或使用 --local")

    vectors: list[list[float]] = []
    for start in range(0, len(texts), 64):
        chunk = texts[start:start + 64]
        response = requests.post(
            f"{base_url.rstrip('/')}{EMBEDDING_ENDPOINT}",
            json={"items": [{"text": text} for text in chunk]},
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"embedding 接口失败 HTTP {response.status_code}: {response.text[:1000]}"
            )
        body = response.json()
        if body.get("code") not in (0, None):
            raise RuntimeError(
                f"embedding 接口返回错误 code={body.get('code')} info={body.get('info')}"
            )
        items = ((body.get("data") or {}).get("items") or [])
        if len(items) != len(chunk):
            raise RuntimeError(f"embedding 数量不一致: {len(items)} != {len(chunk)}")
        for item in items:
            vector = item.get("vector")
            if not isinstance(vector, list):
                vector = []
            vectors.append([float(value) for value in vector])
    return vectors
def get_local_embeddings(texts: list[str]) -> list[list[float]]:
    """Local embedding fallback for ``--local`` evaluation without a backend."""
    if not texts:
        return []
    from algorithm.NL_to_cypher.vector import batch_text_to_vectors

    vectors = batch_text_to_vectors(texts)
    return [list(vector) for vector in vectors]


def _greedy_pairs(golden_texts: list[str], predicted_texts: list[str],
                  threshold: float, embeddings: list[list[float]],
                  offset: int = 0) -> list[tuple[int, int, float]]:
    pairs: list[tuple[int, int, float]] = []
    if not golden_texts or not predicted_texts:
        return pairs
    golden_vecs = embeddings[0:len(golden_texts)]
    predicted_vecs = embeddings[offset:offset + len(predicted_texts)]
    candidates: list[tuple[float, int, int]] = []
    for gi, gv in enumerate(golden_vecs):
        for pi, pv in enumerate(predicted_vecs):
            sim = cosine_similarity(gv, pv)
            if sim >= threshold:
                candidates.append((sim, gi, pi))
    candidates.sort(reverse=True, key=lambda item: item[0])
    used_golden: set[int] = set()
    used_predicted: set[int] = set()
    for sim, gi, pi in candidates:
        if gi in used_golden or pi in used_predicted:
            continue
        used_golden.add(gi)
        used_predicted.add(pi)
        pairs.append((gi, pi, sim))
    return pairs


def semantic_pairs(*, golden_items: list[dict[str, Any]],
                   predicted_items: list[dict[str, Any]],
                   base_url: str, threshold: float,
                   group_key, text_key, embedding_fn=None) -> list[tuple[int, int, float]]:
    golden_groups: dict[tuple[str, ...], list[int]] = {}
    predicted_groups: dict[tuple[str, ...], list[int]] = {}
    for index, item in enumerate(golden_items):
        golden_groups.setdefault(group_key(item), []).append(index)
    for index, item in enumerate(predicted_items):
        predicted_groups.setdefault(group_key(item), []).append(index)

    all_pairs: list[tuple[int, int, float]] = []
    for group, golden_indexes in golden_groups.items():
        predicted_indexes = predicted_groups.get(group, [])
        if not predicted_indexes:
            continue
        golden_texts = [text_key(golden_items[i]) for i in golden_indexes]
        predicted_texts = [text_key(predicted_items[i]) for i in predicted_indexes]
        embeddings = embedding_fn(golden_texts + predicted_texts) if embedding_fn else get_embeddings(base_url, golden_texts + predicted_texts)
        for local_gi, local_pi, sim in _greedy_pairs(
            golden_texts, predicted_texts, threshold, embeddings, len(golden_texts)
        ):
            all_pairs.append((golden_indexes[local_gi], predicted_indexes[local_pi], sim))
    return all_pairs


def _semantic_metrics(pairs: list[tuple[int, int, float]],
                      golden_count: int, predicted_count: int) -> dict[str, float]:
    tp = len(pairs)
    fp = max(0, predicted_count - tp)
    fn = max(0, golden_count - tp)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": float(tp), "fp": float(fp), "fn": float(fn),
            "precision": precision, "recall": recall, "f1": f1}


def semantic_entity_metrics(golden_entities: list[dict[str, Any]],
                            predicted_entities: list[dict[str, Any]],
                            *, base_url: str, threshold: float) -> dict[str, float]:
    pairs = semantic_pairs(
        golden_items=golden_entities,
        predicted_items=predicted_entities,
        base_url=base_url,
        threshold=threshold,
        group_key=lambda item: (canonical(item.get("type")),),
        text_key=lambda item: canonical(item.get("name")),
    )
    return _semantic_metrics(pairs, len(golden_entities), len(predicted_entities))


def semantic_relation_metrics(golden_relations: list[dict[str, Any]],
                              predicted_relations: list[dict[str, Any]],
                              *, base_url: str, threshold: float) -> dict[str, float]:
    def group_key(relation: dict[str, Any]) -> tuple[str, str, str]:
        return (
            canonical(relation.get("rel_type")),
            canonical(relation.get("from_type")),
            canonical(relation.get("to_type")),
        )

    def text_key(relation: dict[str, Any]) -> str:
        return f"{canonical(relation.get('from_name'))} -> {canonical(relation.get('to_name'))}"

    pairs = semantic_pairs(
        golden_items=golden_relations,
        predicted_items=predicted_relations,
        base_url=base_url,
        threshold=threshold,
        group_key=group_key,
        text_key=text_key,
    )
    return _semantic_metrics(pairs, len(golden_relations), len(predicted_relations))
def local_semantic_entity_metrics(golden_entities: list[dict[str, Any]],
                                   predicted_entities: list[dict[str, Any]],
                                   *, threshold: float) -> dict[str, float]:
    pairs = semantic_pairs(
        golden_items=golden_entities,
        predicted_items=predicted_entities,
        base_url="",
        threshold=threshold,
        group_key=lambda item: (canonical(item.get("type")),),
        text_key=lambda item: canonical(item.get("name")),
        embedding_fn=get_local_embeddings,
    )
    return _semantic_metrics(pairs, len(golden_entities), len(predicted_entities))


def local_semantic_relation_metrics(golden_relations: list[dict[str, Any]],
                                     predicted_relations: list[dict[str, Any]],
                                     *, threshold: float) -> dict[str, float]:
    def group_key(relation: dict[str, Any]) -> tuple[str, str, str]:
        return (
            canonical(relation.get("rel_type")),
            canonical(relation.get("from_type")),
            canonical(relation.get("to_type")),
        )

    def text_key(relation: dict[str, Any]) -> str:
        return f"{canonical(relation.get('from_name'))} -> {canonical(relation.get('to_name'))}"

    pairs = semantic_pairs(
        golden_items=golden_relations,
        predicted_items=predicted_relations,
        base_url="",
        threshold=threshold,
        group_key=group_key,
        text_key=text_key,
        embedding_fn=get_local_embeddings,
    )
    return _semantic_metrics(pairs, len(golden_relations), len(predicted_relations))


def parse_uid(uid: Any) -> tuple[str, str] | None:
    text = str(uid or "")
    if "::" not in text:
        return None
    entity_type, entity_name = text.split("::", 1)
    return entity_type, entity_name


def merge_graphs(graphs: list[dict[str, Any]]) -> dict[str, Any]:
    nodes_by_uid: dict[str, dict[str, Any]] = {}
    relationships: dict[tuple[str, str, str], dict[str, Any]] = {}
    for graph in graphs:
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            uid = str(node.get("uid") or "")
            if not uid:
                continue
            current = nodes_by_uid.setdefault(uid, dict(node))
            current["properties"] = dict(current.get("properties") or {})
            current["properties"].update(node.get("properties") or {})
        for relation in graph.get("relationships") or []:
            if not isinstance(relation, dict):
                continue
            key = (
                str(relation.get("from_uid") or ""),
                str(relation.get("to_uid") or ""),
                str(relation.get("type") or ""),
            )
            relationships.setdefault(key, dict(relation))
    return {"nodes": list(nodes_by_uid.values()),
            "relationships": list(relationships.values())}


def graph_to_raw(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = graph.get("nodes") or []
    relationships = graph.get("relationships") or graph.get("relations") or []

    entities: list[dict[str, Any]] = []
    node_lookup: dict[str, tuple[str, str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        label = str(node.get("label") or "")
        properties = node.get("properties") or {}
        if not isinstance(properties, dict):
            properties = {}
        uid = str(node.get("uid") or "")
        name = str(properties.get("name") or "")
        if not name:
            parsed = parse_uid(uid)
            if parsed:
                if not label:
                    label = parsed[0]
                name = parsed[1]
        node_lookup[uid] = (label, name)
        entity_properties = {
            str(k): v for k, v in properties.items()
            if k not in {"name", "chunk_indices"}
        }
        entities.append({"type": label, "name": name, "properties": entity_properties})

    relations: list[dict[str, Any]] = []
    for relation in relationships:
        if not isinstance(relation, dict):
            continue
        from_uid = str(relation.get("from_uid") or relation.get("from") or "")
        to_uid = str(relation.get("to_uid") or relation.get("to") or "")
        rel_type = str(relation.get("type") or relation.get("rel_type") or "")
        from_type, from_name = node_lookup.get(from_uid, ("", ""))
        to_type, to_name = node_lookup.get(to_uid, ("", ""))
        if not from_name:
            parsed = parse_uid(from_uid)
            if parsed:
                from_type, from_name = parsed
        if not to_name:
            parsed = parse_uid(to_uid)
            if parsed:
                to_type, to_name = parsed
        relations.append({
            "from_type": from_type,
            "from_name": from_name,
            "rel_type": rel_type,
            "to_type": to_type,
            "to_name": to_name,
        })
    return {"entities": entities, "relations": relations}


def load_golden(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return data


def find_graph(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    extraction = data.get("extraction")
    if not isinstance(extraction, dict):
        return None
    graph = extraction.get("graph")
    if isinstance(graph, dict):
        return graph
    graphs = extraction.get("graphs")
    if isinstance(graphs, list) and graphs:
        return merge_graphs([g for g in graphs if isinstance(g, dict)])
    return None


class ApiClient:
    def __init__(self, base_url: str, wait_seconds: float, timeout: float,
                 deadline: float, poll_interval: float):
        if requests is None:
            raise SystemExit("缺少 requests，请先 pip install requests，或使用 --local 直连算法")
        base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            base_url = f"http://{base_url}"
        self.base_url = base_url
        self.url = f"{base_url}{ENDPOINT}"
        self.wait_seconds = wait_seconds
        self.timeout = timeout
        self.deadline = deadline
        self.poll_interval = poll_interval
        self.session = requests.Session()

    def close(self) -> None:
        self.session.close()

    def health(self) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/health", timeout=min(self.timeout, 30))
        if response.status_code >= 400:
            raise RuntimeError(f"/health HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    def _result_for_job(self, job_id: str, start: float) -> dict[str, Any]:
        job_url = f"{self.base_url}/api/v1/jobs/{job_id}"
        result_url = f"{job_url}/result"
        while time.monotonic() - start < self.deadline:
            status_body = self.session.get(job_url, timeout=min(self.timeout, 60)).json()
            job = status_body.get("data") or {}
            status = job.get("status") or (status_body.get("execution") or {}).get("status")
            if status == "succeeded":
                result_body = self.session.get(result_url, timeout=min(self.timeout, 60)).json()
                return result_body.get("data") or {}
            if status in {"failed", "cancelled"}:
                raise RuntimeError(
                    f"任务 {job_id} 执行失败：{json.dumps(job.get('error') or status_body.get('error') or {}, ensure_ascii=False)}"
                )
            time.sleep(self.poll_interval)
        raise TimeoutError(f"等待任务 {job_id} 超时")

    def ingest(self, *, ontology: str, docs: list[Path],
               options: dict[str, Any], case_name: str) -> dict[str, Any]:
        files = [
            ("files", (doc.name, doc.read_bytes(), "text/markdown" if doc.suffix.lower() == ".md" else "application/pdf"))
            for doc in docs
        ]
        data = {"ontology": ontology, "options": json.dumps(options, ensure_ascii=False)}
        headers = {}
        if self.wait_seconds > 0:
            headers["Prefer"] = f"wait={self.wait_seconds:g}"
        response = self.session.post(self.url, data=data, files=files,
                                     headers=headers, timeout=self.timeout)
        if response.status_code >= 400:
            raise RuntimeError(
                f"{case_name}: HTTP {response.status_code}: {response.text[:2000]}"
            )
        body = response.json()
        if response.status_code == 202 or (
            isinstance(body.get("data"), dict) and body["data"].get("job_id")
        ):
            job_id = body["data"].get("job_id")
            if not job_id:
                raise RuntimeError(f"{case_name}: 异步响应缺少 job_id")
            print(f"  {case_name}: 已入队 job_id={job_id}，轮询等待结果 ...")
            body = {
                "code": 0,
                "info": "ok",
                "data": self._result_for_job(job_id, time.monotonic()),
                "error": None,
            }
        if body.get("code") not in (0, None) and find_graph(body.get("data")) is None:
            raise RuntimeError(
                f"{case_name}: code={body.get('code')} info={body.get('info')} "
                f"error={json.dumps(body.get('error') or {}, ensure_ascii=False)}"
            )
        return body.get("data") or {}


def _read_text_document(doc: Path) -> str:
    """Read a Markdown/plain-text benchmark document with the same fallback as the backend text parser."""
    data = doc.read_bytes()
    decode_errors: list[str] = []
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            decode_errors.append(f"{encoding}: {exc}")
    raise RuntimeError(f"{doc.name}: 文本文件编码无法识别（{'；'.join(decode_errors)}）")


class LocalExtractionClient:
    """Directly invoke ``algorithm.knowledge_extract_v2`` without starting the backend."""

    def __init__(self, *, chunk_size: int = 500, merge: bool = True,
                 model: str | None = None):
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        self.chunk_size = chunk_size
        self.merge = merge
        self.model = model

    def ingest(self, *, ontology: str, docs: list[Path],
               options: dict[str, Any] | None = None,
               case_name: str) -> dict[str, Any]:
        del options  # 本地模式只使用 md/txt 文本，不执行文档解析器
        from algorithm.knowledge_extract_v2 import (
            deduplicate_chunk_graphs,
            extract_harness_items,
        )
        from algorithm.knowledge_pipeline import chunk_document_items, md_to_text

        ordered_docs = sorted(docs, key=lambda path: path.name)
        parts: list[str] = []
        document_stats: list[dict[str, Any]] = []
        for doc in ordered_docs:
            markdown = _read_text_document(doc)
            if not markdown.strip():
                raise RuntimeError(f"{case_name}: 文档 {doc.name} 解析结果为空")
            parts.append(f"\n## 文件: {doc.name}\n\n{markdown}")
            document_stats.append({
                "file_name": doc.name,
                "file_size": doc.stat().st_size,
                "file_type": doc.suffix.lower().lstrip("."),
                "content_length": len(markdown),
                "parser": {"preferred_engine": "text", "final_engine": "text"},
            })

        combined_markdown = "\n".join(parts)
        chunk_items = chunk_document_items(combined_markdown, chunk_size=self.chunk_size)
        if not chunk_items:
            raise RuntimeError(f"{case_name}: 文档切片结果为空")
        text_chunks = [md_to_text(item["text"]) for item in chunk_items]

        print(f"  本地直连算法：{len(ordered_docs)} 个 md/txt -> "
              f"{len(text_chunks)} 个切片，chunk_size={self.chunk_size}，merge={self.merge}")
        results = extract_harness_items(
            ontology=ontology,
            chunks=text_chunks,
            model=self.model,
            document_type="auto",
        )
        failed_items = [item for item in results if item.get("error")]
        if failed_items and len(failed_items) == len(results):
            messages = "; ".join(
                f"position={item.get('index')}: {item.get('error')}"
                for item in failed_items
            )
            raise RuntimeError(f"{case_name}: 全部文本切片知识抽取失败：{messages}")

        clean_items = [item for item in results if not item.get("error")]
        failures = [
            {
                "position": item.get("index"),
                "error_code": item.get("error_code"),
                "info": item.get("error"),
                "attempts": item.get("attempts", []),
                "raw_response": item.get("raw_response", ""),
            }
            for item in failed_items
        ]
        extraction: dict[str, Any] = {
            "ranges": [item.get("range") for item in chunk_items],
            "failures": failures,
        }
        if self.merge:
            graph = (
                deduplicate_chunk_graphs(clean_items)
                if clean_items
                else {"nodes": [], "relationships": []}
            )
            extraction["graph"] = graph
            node_count = len(graph.get("nodes", []))
            relationship_count = len(graph.get("relationships", []))
        else:
            graphs = [
                item.get("graph") or {"nodes": [], "relationships": []}
                for item in clean_items
            ]
            extraction["graphs"] = graphs
            node_count = sum(len(graph.get("nodes", [])) for graph in graphs)
            relationship_count = sum(len(graph.get("relationships", [])) for graph in graphs)

        attempts = [
            {"position": item.get("index"), **attempt}
            for item in results
            for attempt in item.get("attempts", [])
        ]
        return {
            "name": case_name,
            "summary": "",
            "artifacts": None,
            "extraction": extraction,
            "stats": {
                "documents": document_stats,
                "chunking": {
                    "chunk_count": len(chunk_items),
                    "strategy": "recursive",
                    "size": self.chunk_size,
                    "vector_storage_enabled": False,
                },
                "extraction": {
                    "chunks_extracted": len(text_chunks),
                    "merge": self.merge,
                    "nodes_extracted": node_count,
                    "relationships_extracted": relationship_count,
                    "attempts": attempts,
                },
            },
        }



@dataclass
class Case:
    name: str
    docs_dir: Path
    ontology_dir: Path
    golden_path: Path


def _case_key(path: Path) -> tuple[str, int]:
    match = re.search(r"(\d+)$", path.name)
    return path.name, int(match.group(1)) if match else 0


def discover_cases(benchmark_dir: Path, pattern: str,
                   only: set[str] | None) -> list[Path]:
    if not benchmark_dir.is_dir():
        raise SystemExit(f"benchmark 目录不存在：{benchmark_dir}")
    cases = [p for p in sorted(benchmark_dir.glob(pattern), key=_case_key)
             if p.is_dir() and (not only or p.name in only)]
    if only:
        missing = only - {p.name for p in cases}
        if missing:
            print(f"警告：以下 case 不存在，已忽略：{', '.join(sorted(missing))}")
    return cases


def select_docs(docs_dir: Path, all_docs: bool, *, prefer_md: bool = True) -> list[Path]:
    if not docs_dir.is_dir():
        return []
    candidates = sorted(p for p in docs_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in DOC_SUFFIXES)
    if all_docs:
        return candidates
    if prefer_md:
        md_by_stem = {p.stem: p for p in candidates if p.suffix.lower() == ".md"}
        chosen: list[Path] = []
        for path in candidates:
            if path.suffix.lower() != ".md" and path.stem in md_by_stem:
                continue
            chosen.append(path)
        return chosen
    source_stems = {p.stem for p in candidates if p.suffix.lower() != ".md"}
    return [p for p in candidates
            if not (p.suffix.lower() == ".md" and p.stem in source_stems)]


def score_case(case_name: str, predicted: dict[str, Any], golden: dict[str, Any],
               match_mode: str, *, semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
               embedding_base_url: str | None = None,
               local_embeddings: bool = False) -> dict[str, Any]:
    golden_entities = golden.get("entities") or []
    golden_relations = golden.get("relations") or golden.get("relationships") or []
    predicted_entities = predicted.get("entities") or []
    predicted_relations = predicted.get("relations") or []

    valid_golden_entities = [x for x in golden_entities if isinstance(x, dict)]
    valid_predicted_entities = [x for x in predicted_entities if isinstance(x, dict)]
    valid_golden_relations = [x for x in golden_relations if isinstance(x, dict)]
    valid_predicted_relations = [x for x in predicted_relations if isinstance(x, dict)]

    entity_key = entity_full_key if match_mode == "full" else entity_identity_key
    golden_entity_keys = {entity_key(x) for x in valid_golden_entities}
    predicted_entity_keys = {entity_key(x) for x in valid_predicted_entities}
    golden_relation_keys = {relation_key(x) for x in valid_golden_relations}
    predicted_relation_keys = {relation_key(x) for x in valid_predicted_relations}

    identity_golden = {entity_identity_key(x) for x in valid_golden_entities}
    identity_predicted = {entity_identity_key(x) for x in valid_predicted_entities}
    full_golden = {entity_full_key(x) for x in valid_golden_entities}
    full_predicted = {entity_full_key(x) for x in valid_predicted_entities}

    if match_mode == "semantic":
        if local_embeddings:
            entity_metric = local_semantic_entity_metrics(
                valid_golden_entities, valid_predicted_entities,
                threshold=semantic_threshold,
            )
            relation_metric = local_semantic_relation_metrics(
                valid_golden_relations, valid_predicted_relations,
                threshold=semantic_threshold,
            )
        else:
            if not embedding_base_url:
                raise ValueError("semantic 匹配需要 embedding_base_url")
            entity_metric = semantic_entity_metrics(
                valid_golden_entities, valid_predicted_entities,
                base_url=embedding_base_url, threshold=semantic_threshold,
            )
            relation_metric = semantic_relation_metrics(
                valid_golden_relations, valid_predicted_relations,
                base_url=embedding_base_url, threshold=semantic_threshold,
            )
    else:
        entity_metric = prf1(predicted_entity_keys, golden_entity_keys)
        relation_metric = prf1(predicted_relation_keys, golden_relation_keys)

    return {
        "case": case_name,
        "golden_entities": len(valid_golden_entities),
        "predicted_entities": len(valid_predicted_entities),
        "golden_relations": len(valid_golden_relations),
        "predicted_relations": len(valid_predicted_relations),
        "entity": entity_metric,
        "relation": relation_metric,
        "entity_identity": prf1(identity_predicted, identity_golden),
        "entity_full": prf1(full_predicted, full_golden),
    }


def print_report(rows: list[dict[str, Any]], match_mode: str,
                 semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD) -> None:
    print("\n==================== 分 case 结果 ====================")
    print(f"{'case':<14} {'goldE':>6} {'predE':>6} {'E-P':>7} {'E-R':>7} {'E-F1':>7} "
          f"{'goldR':>6} {'predR':>6} {'R-P':>7} {'R-R':>7} {'R-F1':>7}")
    for row in rows:
        e, r = row["entity"], row["relation"]
        print(f"{row['case']:<14} {row['golden_entities']:>6} {row['predicted_entities']:>6} "
              f"{e['precision'] * 100:>6.2f}% {e['recall'] * 100:>6.2f}% "
              f"{e['f1'] * 100:>6.2f}% "
              f"{row['golden_relations']:>6} {row['predicted_relations']:>6} "
              f"{r['precision'] * 100:>6.2f}% {r['recall'] * 100:>6.2f}% "
              f"{r['f1'] * 100:>6.2f}%")
    entity = aggregate([row["entity"] for row in rows])
    relation = aggregate([row["relation"] for row in rows])
    identity = aggregate([row["entity_identity"] for row in rows])
    full = aggregate([row["entity_full"] for row in rows])
    print("\n==================== micro 汇总 ====================")
    if match_mode == "semantic":
        label = f"semantic(cosine>={semantic_threshold:.2f})"
    elif match_mode == "full":
        label = "full(含属性)"
    else:
        label = "identity(type+name)"
    print(f"实体匹配口径：{label}")
    print("实体  P={:>6.2f}%  R={:>6.2f}%  F1={:>6.2f}%  "
          "(TP={:.0f}, FP={:.0f}, FN={:.0f})".format(
              entity["precision"] * 100, entity["recall"] * 100, entity["f1"] * 100,
              entity["tp"], entity["fp"], entity["fn"]))
    print("关系  P={:>6.2f}%  R={:>6.2f}%  F1={:>6.2f}%  "
          "(TP={:.0f}, FP={:.0f}, FN={:.0f})".format(
              relation["precision"] * 100, relation["recall"] * 100, relation["f1"] * 100,
              relation["tp"], relation["fp"], relation["fn"]))
    print("实体(参考) identity P={:>6.2f}% R={:>6.2f}% F1={:>6.2f}% | "
          "full P={:>6.2f}% R={:>6.2f}% F1={:>6.2f}%".format(
              identity["precision"] * 100, identity["recall"] * 100, identity["f1"] * 100,
              full["precision"] * 100, full["recall"] * 100, full["f1"] * 100))


def parse_args(default_benchmark: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="knowledge_extract_v2 benchmark 评测",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--benchmark", choices=("official", "simple", "standard"),
                        default=default_benchmark, help="评测哪个 benchmark")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="Backend 地址，默认 http://127.0.0.1:8006")
    parser.add_argument("--local", action="store_true",
                        help="不启动后端，直接调用 algorithm.knowledge_extract_v2 算法；"
                             "official benchmark 默认启用")
    parser.add_argument("--backend", action="store_true",
                        help="强制走 Backend HTTP 接口（覆盖 official 默认的本地直连模式）")
    parser.add_argument("--only", default="", help="只跑指定 case，逗号分隔")
    parser.add_argument("--all-docs", action="store_true",
                        help="上传 docs 下所有支持文档，包括与原始文档同名的 md")
    parser.add_argument("--match", choices=("identity", "full", "semantic"),
                        default="identity",
                        help="identity=type+name；full=type+name+properties；semantic=向量相似度")
    parser.add_argument("--semantic-threshold", type=float,
                        default=DEFAULT_SEMANTIC_THRESHOLD, help="语义匹配余弦阈值")
    parser.add_argument("--chunk-size", type=int, default=None, help="覆盖切片大小")
    parser.add_argument("--parser-engine", default="auto",
                        choices=("docling", "mineru", "vlm", "auto"),
                        help="文档解析引擎；默认 auto，由后端按 PDF 文本/扫描特征自动选择")
    parser.add_argument("--no-preparsed-md", action="store_true",
                        help="不使用 build_preparsed_md.py 生成的同名 md，仍上传原始 PDF/Word 重新解析")
    parser.add_argument("--no-merge", action="store_true", help="不合并分片图谱")
    parser.add_argument("--wait", type=float, default=60.0, help="Prefer wait 秒数")
    parser.add_argument("--timeout", type=float, default=3600.0, help="HTTP 超时秒数")
    parser.add_argument("--deadline", type=float, default=4 * 3600.0,
                        help="异步任务最长等待秒数")
    parser.add_argument("--poll-interval", type=float, default=5.0,
                        help="异步任务轮询间隔秒数")
    parser.add_argument("--out-dir", default="",
                        help="结果输出目录；默认 qasystem/eval_results")
    parser.add_argument("--no-save", action="store_true", help="不保存结果")
    parser.add_argument("--skip-health", action="store_true", help="跳过 /health 预检")
    return parser.parse_args()


def load_local_dotenv() -> bool:
    """Load repository-root .env for local direct evaluation.

    Process environment variables always win; ``.env`` is only a convenience for
    running ``eval_knowledge_extract_v2_official.py`` without starting the backend.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return bool(load_dotenv(ROOT_ENV_FILE, override=False))


def main(default_benchmark: str = "official") -> int:
    args = parse_args(default_benchmark)
    if args.local and args.backend:
        raise SystemExit("--local 与 --backend 不能同时使用")
    # official 的 docs 已全部替换为 md，默认不启动后端，直接调用 knowledge_extract_v2 算法。
    local_mode = args.local or (not args.backend and args.benchmark == "official")
    base_url = args.base_url or DEFAULT_BASE_URL
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if load_local_dotenv():
        print(f"已从 {ROOT_ENV_FILE} 加载环境变量（不覆盖已有进程变量）")

    benchmark_dir = BENCHMARK_ROOTS[args.benchmark]
    only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None

    if args.benchmark == "simple":
        cases = [Case("simple", benchmark_dir / "docs",
                      benchmark_dir / "ontology",
                      benchmark_dir / "extract" / "golden.json")]
    else:
        pattern = "official_*" if args.benchmark == "official" else "standard_*"
        discovered = discover_cases(benchmark_dir, pattern, only)
        cases = [Case(p.name, p / "docs", p / "ontology",
                      p / "extract" / "golden.json") for p in discovered]
    if not cases:
        raise SystemExit(f"在 {benchmark_dir} 下没有找到评测 case")

    parser_config: dict[str, Any] = {"engine": args.parser_engine}
    if args.parser_engine != "auto":
        parser_config["force"] = True
    options: dict[str, Any] = {
        "parser": parser_config,
        "extraction": {"merge": not args.no_merge},
    }
    if args.chunk_size is not None:
        options["chunking"] = {"size": args.chunk_size}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 测试结果统一放到仓库根目录 qasystem/eval_results 下，
    # 文件名包含 benchmark 类型、实际测试文档类型（如 pdf/docx/md）和匹配口径。
    out_dir = Path(args.out_dir).resolve() if args.out_dir else ROOT / "eval_results"
    if not args.no_save:
        out_dir.mkdir(parents=True, exist_ok=True)

    local_client = LocalExtractionClient(
        chunk_size=args.chunk_size or 500,
        merge=not args.no_merge,
    )
    client: ApiClient | None = None
    if not local_mode:
        client = ApiClient(base_url, max(0.0, args.wait), max(1.0, args.timeout),
                           max(1.0, args.deadline), max(0.5, args.poll_interval))
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    predictions_store: dict[str, Any] = {}
    document_types: set[str] = set()

    try:
        if local_mode:
            print(f"Mode: local-direct（{LOCAL_ENDPOINT}）")
        else:
            print(f"Backend: {client.url}")
        print(f"Benchmark: {benchmark_dir}")
        print(f"共发现 {len(cases)} 个评测 case，匹配口径：{args.match}\n")
        if not local_mode and not args.skip_health:
            try:
                print(f"/health: {client.health()}\n")
            except Exception as exc:
                raise SystemExit(
                    f"无法连接 Backend /health：{exc}。请确认服务地址和端口，"
                    f"或使用 --skip-health / --local。"
                ) from exc

        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] 处理 {case.name} ...")
            ontology_files = sorted(
                f for suffix in ONTOLOGY_SUFFIXES
                for f in case.ontology_dir.glob(f"*{suffix}") if f.is_file()
            )
            if local_mode:
                # 本地直连算法只接收纯文本：md 已替换原始 PDF/Word，直接读取 md/txt。
                docs = select_docs(case.docs_dir, args.all_docs, prefer_md=True)
                docs = [doc for doc in docs if doc.suffix.lower() in {".md", ".txt"}]
            else:
                docs = select_docs(case.docs_dir, args.all_docs,
                                   prefer_md=not args.no_preparsed_md)
            using_md = any(doc.suffix.lower() == ".md" for doc in docs)
            if local_mode:
                if using_md:
                    print(f"  使用本地 md/txt：{[d.name for d in docs]}")
            elif using_md and not args.no_preparsed_md:
                print(f"  使用预解析 md：{[d.name for d in docs]}")
            document_types.update(
                doc.suffix.lower().lstrip(".") for doc in docs if doc.suffix
            )

            if not docs:
                hint = "（local-direct 模式仅支持 md/txt，请先运行 build_preparsed_md.py 或使用 --backend）" if local_mode else ""
                print(f"  跳过：{case.name}/docs 下没有可读取文档{hint}")
                errors.append({"case": case.name,
                               "error": f"docs 下没有可{'直接读取' if local_mode else '上传'}文档"})
                continue
            if not ontology_files:
                print(f"  跳过：{case.name}/ontology 下没有本体文件")
                errors.append({"case": case.name, "error": "ontology 下没有本体文件"})
                continue
            if not case.golden_path.is_file():
                print(f"  跳过：{case.name}/extract/golden.json 不存在")
                errors.append({"case": case.name, "error": "golden.json 不存在"})
                continue

            try:
                golden = load_golden(case.golden_path)
                ontology = "\n\n".join(
                    f.read_text(encoding="utf-8-sig") for f in ontology_files
                )
                case_options = dict(options)
                if using_md and (local_mode or not args.no_preparsed_md):
                    case_options["parser"] = {"engine": "text", "force": True}
                if local_mode:
                    data = local_client.ingest(
                        ontology=ontology, docs=docs,
                        options=case_options, case_name=case.name,
                    )
                else:
                    data = client.ingest(ontology=ontology, docs=docs,
                                         options=case_options, case_name=case.name)
                graph = find_graph(data)
                if graph is None:
                    raise RuntimeError(f"{case.name}: 响应中找不到 extraction.graph/graphs")
                predicted_raw = graph_to_raw(graph)
                result = score_case(
                    case.name, predicted_raw, golden, args.match,
                    semantic_threshold=args.semantic_threshold,
                    embedding_base_url=None if local_mode else base_url,
                    local_embeddings=local_mode,
                )
                rows.append(result)
                predictions_store[case.name] = {
                    "docs": [d.name for d in docs],
                    "ontology": [f.name for f in ontology_files],
                    "api_data": data,
                    "predicted_raw": predicted_raw,
                }
                e, r = result["entity"], result["relation"]
                print(f"  实体 P={e['precision'] * 100:.2f}% R={e['recall'] * 100:.2f}% "
                      f"F1={e['f1'] * 100:.2f}% | "
                      f"关系 P={r['precision'] * 100:.2f}% R={r['recall'] * 100:.2f}% "
                      f"F1={r['f1'] * 100:.2f}%")
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                print(f"  失败：{message}")
                errors.append({"case": case.name, "error": message})

        if rows:
            print_report(rows, args.match, args.semantic_threshold)
        if errors:
            print(f"\n共 {len(errors)} 个 case 失败/跳过：")
            for item in errors:
                print(f"  - {item['case']}: {item['error']}")

        if not args.no_save:
            summary = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "mode": "local_direct" if local_mode else "backend",
                "base_url": None if local_mode else (client.url if client else None),
                "endpoint": LOCAL_ENDPOINT if local_mode else ENDPOINT,
                "benchmark": args.benchmark,
                "benchmark_dir": str(benchmark_dir),
                "document_types": sorted(document_types),
                "options": options,
                "chunk_size": local_client.chunk_size,
                "match_mode": args.match,
                "semantic_threshold": args.semantic_threshold,
                "case_results": rows,
                "errors": errors,
                "micro": {
                    "entity": aggregate([x["entity"] for x in rows]),
                    "relation": aggregate([x["relation"] for x in rows]),
                    "entity_identity": aggregate([x["entity_identity"] for x in rows]),
                    "entity_full": aggregate([x["entity_full"] for x in rows]),
                },
            }
            doc_type_part = "_".join(sorted(document_types)) or "unknown"
            file_stem = (
                f"{args.benchmark}_{doc_type_part}_{args.match}_{timestamp}"
            )
            summary_path = out_dir / f"evaluation_{file_stem}.json"
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            predictions_path = out_dir / f"predictions_{file_stem}.json"
            predictions_path.write_text(
                json.dumps(predictions_store, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            print(f"\n结果已保存：{summary_path}")
            print(f"预测明细已保存：{predictions_path}")
        return 1 if errors else 0
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
