"""
从切块 JSON 中取出文本片段，逐段调用抽取并合并为与 extract() 一致的 {raw, graph}。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def extract_chunk_texts_from_payload(payload: Any) -> list[str]:
    """
    标准切块格式见 algorithm/uie/chunks.json：顶层 {"chunks": ["...", "...", ...]}，元素为整段文本字符串。

    另兼容：
    - {"chunks": [{"text": "..."}, ...]}
    - {"items": [...]}（元素为 str 或 {"text": ...}）
    - 根为字符串列表
    - 单段 {"text": "..."}
    """
    if payload is None:
        return []

    if isinstance(payload, str):
        return [payload.strip()] if payload.strip() else []

    if isinstance(payload, list):
        out: list[str] = []
        for item in payload:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                t = item.get("text") or item.get("content") or item.get("body")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
        return out

    if not isinstance(payload, dict):
        return []

    if "text" in payload and isinstance(payload["text"], str) and payload["text"].strip():
        return [payload["text"].strip()]

    for key in ("chunks", "items", "segments", "blocks"):
        if key not in payload:
            continue
        val = payload[key]
        if not isinstance(val, list):
            continue
        out = []
        for item in val:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                t = item.get("text") or item.get("content") or item.get("body") or item.get("page_content")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
        if out:
            return out

    return []


def merge_raw_extractions(raws: list[Any]) -> Any:
    """将多段 extract 的 raw 合并为与单段相同风格的结构。"""
    logger.info("merge_raw_extractions: start chunks=%d", len(raws))
    if not raws:
        logger.info("merge_raw_extractions: empty input, return {}")
        return {}
    first = raws[0]

    # 1) 若每段都是 list：直接合并 list
    if isinstance(first, list):
        merged: list[Any] = []
        for r in raws:
            if isinstance(r, list):
                merged.extend(r)
            elif r is not None:
                merged.append(r)
        logger.info("merge_raw_extractions: branch=list merged_items=%d", len(merged))
        return merged

    # 2) 若每段都是 dict：按顶层 key（Term/SystemElement/...）分别合并 value
    if all((r is None) or isinstance(r, dict) for r in raws):
        merged_by_key: dict[str, list[Any]] = {}
        for r in raws:
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                merged_by_key.setdefault(k, [])
                if isinstance(v, list):
                    merged_by_key[k].extend(v)
                elif v is not None:
                    merged_by_key[k].append(v)

        if merged_by_key:
            top_keys = ",".join(sorted(merged_by_key.keys()))
            merged_items = sum(len(v) for v in merged_by_key.values())
            logger.info(
                "merge_raw_extractions: branch=dict-by-topkey keys=%s merged_items=%d",
                top_keys,
                merged_items,
            )
            # 如果只有一个顶层键，尽量保持输出格式与旧版一致：{Term:[...]}
            if len(merged_by_key) == 1:
                only_key = next(iter(merged_by_key.keys()))
                return {only_key: merged_by_key[only_key]}
            return merged_by_key

    # 3) 结构不一致时保留为带索引的列表，避免丢失
    logger.warning(
        "merge_raw_extractions: branch=fallback inconsistent-raw-structure chunks=%d",
        len(raws),
    )
    return {"chunks": [{"index": i, "raw": r} for i, r in enumerate(raws)]}


def _dedupe_relationships(rels: list[dict]) -> list[dict]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict] = []
    for r in rels:
        t = (r.get("from_uid"), r.get("to_uid"), r.get("type"))
        if t in seen:
            continue
        seen.add(t)
        out.append(r)
    return out


def _dedupe_ontology(onto: list[dict]) -> list[dict]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict] = []
    for r in onto:
        t = (r.get("from_uid"), r.get("class_name"), r.get("type"))
        if t in seen:
            continue
        seen.add(t)
        out.append(r)
    return out


def merge_graph_structures(graphs: list[dict]) -> dict:
    nodes_by_uid: dict[Any, dict] = {}
    all_rels: list[dict] = []
    all_onto: list[dict] = []

    for g in graphs:
        if not g:
            continue
        for n in g.get("nodes") or []:
            uid = n.get("uid")
            if uid is None:
                continue
            if uid not in nodes_by_uid:
                nodes_by_uid[uid] = {
                    "uid": n.get("uid"),
                    "label": n.get("label"),
                    "properties": dict(n.get("properties") or {}),
                }
            else:
                nodes_by_uid[uid]["properties"].update(n.get("properties") or {})
                if not nodes_by_uid[uid].get("label") and n.get("label"):
                    nodes_by_uid[uid]["label"] = n.get("label")
        all_rels.extend(g.get("relationships") or [])
        all_onto.extend(g.get("ontology_relations") or [])

    return {
        "nodes": list(nodes_by_uid.values()),
        "relationships": _dedupe_relationships(all_rels),
        "ontology_relations": _dedupe_ontology(all_onto),
    }


def extract_from_chunked_payload(
    payload: Any,
    extract_fn: Callable[..., dict],
    *,
    main_object: str,
    use_templates: bool = True,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
) -> dict:
    """
    对切块 JSON 解析出的每段文本调用 extract_fn（与 extraction_api.extract 同签名），合并结果。

    返回与 extract() 相同：{"raw": ..., "graph": ...}，另含 "meta": {"chunk_count", "chunks_nonempty"}。
    """
    texts = extract_chunk_texts_from_payload(payload)
    if not texts:
        raise ValueError("切块 JSON 中未找到有效文本片段（支持 chunks/items 数组或字符串列表等）")
    merged_source_text = "\n\n".join(texts)
    preview = merged_source_text[:500].replace("\n", "\\n")
    logger.info(
        "chunked source merged: chunks=%d merged_len=%d preview=%s",
        len(texts),
        len(merged_source_text),
        preview,
    )

    raws: list[Any] = []
    graphs: list[dict] = []

    for i, text in enumerate(texts):
        logger.info("chunked extract: %d/%d len=%d", i + 1, len(texts), len(text))
        part = extract_fn(
            main_object=main_object,
            text=text,
            use_templates=use_templates,
            store_to_neo4j=False,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
        )
        raws.append(part.get("raw"))
        graphs.append(part.get("graph") or {})

    merged_raw = merge_raw_extractions(raws)
    merged_graph = merge_graph_structures(graphs)

    return {
        "raw": merged_raw,
        "graph": merged_graph,
        "meta": {
            "chunk_count": len(texts),
            "chunks_nonempty": len(texts),
        },
    }


def parse_chunked_json_bytes(data: bytes) -> Any:
    """解析上传文件为 Python 对象。"""
    if not data or not data.strip():
        raise ValueError("文件为空")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError("文件须为 UTF-8 编码的 JSON") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}") from e
