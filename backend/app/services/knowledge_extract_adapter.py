"""Adapter exposing the configured Knowledge Extract version through the V1 batch contract.

``GraphExtractionService`` and the MCP extraction tool were written against
``algorithm.knowledge_extract`` (V1), whose batch entry point takes one plain
string per chunk and returns one result item per chunk.  V2/V3 are aggregate
pipelines: they consume ``ExtractionChunk`` objects, run one pass over the whole
document, and return a single merged result.

This module bridges the two without touching the versioned packages.  The
environment variable ``KG_EXTRACT_ALGO_VERSION`` (see
``algorithm.knowledge_extract.version``) selects the concrete algorithm:

* ``latest`` (default): runs the independent ``latest`` package through the
  same per-chunk projection.
* ``v2`` / ``v3.1``: build V2 ``ExtractionChunk`` objects, run the selected package's
  ``extract_aggregate`` once, then project the document-level result back onto the
  individual chunks using each entity's evidence anchor, so the caller keeps seeing
  one item per chunk and ``deduplicate_chunk_graphs`` can still rebuild the merged
  graph (and its ``chunk_indices``) exactly as before.
* ``v3``: run ``v3.extract_aggregate`` with the V2-configured LLM client; it
  reshapes the flat ``entities``/``relations`` result into the V2 aggregate
  contract, then shares the same projection. ``latest`` also accepts chunk field
  dictionaries.
* ``jev``: run the ``jev_extract`` copy. This label is opt-in and is not the
  default. The package is imported only when the label is selected.

Two deliberate choices worth knowing about for the V2 path:

* ``validation_mode="permissive"``.  In strict mode V2 turns *any* verification
  hard issue (a single unlocatable evidence quote, one duplicate entity) into a
  terminal failure for the whole run.  V1 only ever reported those as warnings,
  so strict mode would convert ordinary documents into failed requests.
* ``best_effort=True``.  This only affects the reported ``status``; it lets a
  partially successful run stay ``partial`` instead of ``failed`` so chunk-level
  failure attribution remains meaningful.
"""
from __future__ import annotations

import json
import logging
import os
import re
from copy import deepcopy
from typing import Any, Callable, Iterable, Sequence

from algorithm.knowledge_extract import latest, v2, v3, v3_1, version as ke_version

from backend.app import observability

logger = logging.getLogger(__name__)

CHUNK_ID_PREFIX = "chunk-"
_JSON_FENCE = re.compile(
    r"(?ims)^[ \t]*```[ \t]*json[ \t]*\r?\n(?P<body>.*?)^[ \t]*```[ \t]*$"
)
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")
_BATCH_FILE_HEADING = re.compile(r"^文件\s*[:：]\s*(?P<name>.+?)\s*$")
_SPREADSHEET_TYPES = frozenset({"csv", "excel", "xls", "xlsx"})

#: V2 only raises these as terminal failures for the runs we care about; a
#: terminal ``repair_*`` or ``verification`` attempt is not attributed to a
#: chunk because it is a document-level concern.
_FATAL_ATTEMPT_TYPES = frozenset(
    {"incomplete", "empty", "timeout", "request", "json", "schema", "schema_validation"}
)
#: Only the entity pass is load-bearing -- if it fails there is genuinely
#: nothing to report.  Every other pass *adds* to a result that already
#: exists, so a terminal failure there means "less data", not "no data"; V2
#: itself still calls such a run ``partial``.  Treating them as fatal failed
#: the whole batch and discarded the entities that had already been
#: extracted, which is why an ontology with fewer classes (nothing left for
#: the relation pass to find) appeared to extract nothing at all.
_NON_FATAL_PASSES = frozenset(
    {"property", "relation", "repair_entity", "repair_relation", "verification"}
)


def _empty_graph() -> dict[str, list[dict[str, Any]]]:
    return {"nodes": [], "relationships": []}


def publish_best_effort_graph() -> bool:
    """verification 未通过时，抽取响应是否仍返回 best-effort 图谱。

    默认开启（1）：``graph``/``graphs`` 保留已抽取的节点与关系，
    ``graph_publishable=false`` 仅作为下游沉淀/入库环节的"不可信"信号；
    设为 0 恢复旧行为（响应 ``graph`` 置空，图谱仅出现在 ``isolated_graph``）。
    读取方式与 ``KG_EXTRACT_STRICT_CHUNK_FAILURES`` 一致：调用时读环境变量。
    """
    value = str(os.environ.get("KG_EXTRACT_PUBLISH_BEST_EFFORT", "1")).strip().lower()
    return value not in {"0", "false", "no"}


def _quality_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Keep aggregate verification visible across the per-chunk adapter."""
    profile = result.get("profile") or {}
    verification = result.get("verification") or {}
    status = str(
        result.get("status")
        or ("failed" if result.get("error_code") else "succeeded")
    )
    # A graph that failed terminal verification may still be useful for
    # diagnostics/manual review, but it must never be treated as publishable.
    # Keep the decision in the public quality contract so every adapter path
    # applies the same gate.
    graph_publishable = verification.get("valid") is not False and status not in {
        "failed",
    }
    if not graph_publishable and status == "succeeded":
        # 校验未过的图谱不得标 succeeded：降级为 partial，与 best-effort 发布语义一致。
        status = "partial"
    summary = {
        "status": status,
        "error_code": result.get("error_code") or None,
        "verification": deepcopy(verification) if isinstance(verification, dict) else {},
        "graph_publishable": graph_publishable,
        "repair_stop_reason": (
            profile.get("repair_stop_reason") if isinstance(profile, dict) else None
        ),
    }
    # 评测侧需要知道本次结果出自哪版算法；由 run_aggregate 盖戳。
    if result.get("extract_version"):
        summary["extract_version"] = result["extract_version"]
    if result.get("effective_config"):
        summary["effective_config"] = deepcopy(result["effective_config"])
    return summary


def _chunk_id(position: int) -> str:
    return f"{CHUNK_ID_PREFIX}{position:04d}"


def _table_payload(value: Any) -> tuple[list[str], list[Any]] | None:
    if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
        return None
    columns = value.get("columns")
    if not isinstance(columns, list):
        columns = []
    return [str(item) for item in columns], list(value["rows"])


def _nearest_heading(text: str, offset: int) -> str | None:
    headings = [match.group("title").strip() for match in _HEADING.finditer(text, 0, offset)]
    for title in reversed(headings):
        if not re.match(r"^文件\s*[:：]", title):
            return title
    return None


def _nearest_source_file(text: str, offset: int) -> str | None:
    headings = [match.group("title").strip() for match in _HEADING.finditer(text, 0, offset)]
    for title in reversed(headings):
        matched = _BATCH_FILE_HEADING.match(title)
        if matched:
            return matched.group("name").strip()
    return None


def _infer_table_metadata(text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Infer compact table provenance without changing the caller's text.

    Spreadsheet-specific row numbers are only emitted when the trusted caller
    identifies the source as Excel/CSV.  Other JSON tables still get generic
    table counts and columns, avoiding the false claim that every heading is a
    worksheet name.
    """
    tables: list[dict[str, Any]] = []
    for index, match in enumerate(_JSON_FENCE.finditer(text or "")):
        try:
            parsed = json.loads(match.group("body"))
        except (TypeError, ValueError):
            continue
        table = _table_payload(parsed)
        if table is None:
            continue
        columns, rows = table
        tables.append(
            {
                "table_index": index,
                "title": _nearest_heading(text, match.start()),
                "source_file": _nearest_source_file(text, match.start()),
                "columns": columns,
                "row_count": len(rows),
            }
        )

    if not tables:
        try:
            parsed = json.loads((text or "").strip())
        except (TypeError, ValueError):
            parsed = None
        table = _table_payload(parsed)
        if table is not None:
            columns, rows = table
            tables.append(
                {
                    "table_index": 0,
                    "title": None,
                    "source_file": None,
                    "columns": columns,
                    "row_count": len(rows),
                }
            )

    if not tables:
        return metadata

    source_type = str(metadata.get("source_type") or metadata.get("file_type") or "")
    source_type = source_type.lower().lstrip(".")
    if source_type == "batch":
        source_type = str(metadata.get("source_file_type") or "").lower().lstrip(".")
    spreadsheet = source_type in _SPREADSHEET_TYPES
    for table in tables:
        table_source_type = str(table.get("source_file") or "").rsplit(".", 1)[-1].lower()
        table_is_spreadsheet = spreadsheet or table_source_type in _SPREADSHEET_TYPES
        if table_is_spreadsheet:
            table["sheet_name"] = table.pop("title", None)
            if table["row_count"]:
                table["source_row_start"] = 2
                table["source_row_end"] = 1 + int(table["row_count"])
        elif table.get("title") is None:
            table.pop("title", None)
        if table.get("source_file") is None:
            table.pop("source_file", None)
    metadata.setdefault("content_kind", "record_table")
    metadata["table_count"] = len(tables)
    metadata["tables"] = tables
    if len(tables) == 1 and tables[0].get("sheet_name"):
        if tables[0].get("source_file"):
            metadata.setdefault("source_file", tables[0]["source_file"])
        metadata.setdefault("sheet_name", tables[0]["sheet_name"])
        if tables[0].get("source_row_start") is not None:
            metadata.setdefault("source_row_start", tables[0]["source_row_start"])
            metadata.setdefault("source_row_end", tables[0]["source_row_end"])
    return metadata


def _chunk_payloads(
    texts: Sequence[str],
    ranges: Sequence[Sequence[int] | None] | None = None,
    metadata: Sequence[dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """Build the per-chunk field payloads shared by every extraction version."""
    payloads = []
    for position, text in enumerate(texts):
        span = ranges[position] if ranges is not None and position < len(ranges) else None
        supplied = metadata[position] if metadata is not None and position < len(metadata) else None
        chunk_metadata = _infer_table_metadata(text or "", deepcopy(supplied or {}))
        document_id = chunk_metadata.get("document_id") or chunk_metadata.get("source_file")
        payloads.append(
            {
                "chunk_id": _chunk_id(position),
                "text": text or "",
                "document_id": str(document_id) if document_id else None,
                "section_id": chunk_metadata.get("section_id"),
                "section_title": chunk_metadata.get("section_title") or chunk_metadata.get("sheet_name"),
                "clause_number": chunk_metadata.get("clause_number"),
                "page_no": chunk_metadata.get("page_no"),
                # Explicit TextChunk ranges are authoritative; metadata offsets
                # retain provenance when an upstream caller has no ranges field.
                "start_offset": span[0] if span is not None else chunk_metadata.get("start_offset"),
                "end_offset": span[1] if span is not None else chunk_metadata.get("end_offset"),
                "metadata": chunk_metadata,
            }
        )
    return payloads


def build_chunks(
    texts: Sequence[str],
    ranges: Sequence[Sequence[int] | None] | None = None,
    metadata: Sequence[dict[str, Any] | None] | None = None,
) -> list[Any]:
    """Wrap plain chunk texts as the ``ExtractionChunk`` objects V2 requires.

    Chunk text is passed through untouched.  Callers must not pre-strip Markdown
    headings: V2's structural extractors rely on the ``#`` markers.
    """
    from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk

    return [ExtractionChunk(**payload) for payload in _chunk_payloads(texts, ranges, metadata)]


def run_aggregate(
    ontology: str,
    texts: Sequence[str],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    target_types: Iterable[str] | None = None,
    max_workers: int | None = None,
    ranges: Sequence[Sequence[int] | None] | None = None,
    metadata: Sequence[dict[str, Any] | None] | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
    extraction_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the configured extraction version once over every chunk and return its raw result."""
    label = extraction_config["version"] if extraction_config else ke_version.get_extract_version_label()
    concrete_version = label
    if runner is None:
        if label == ke_version.JEV:
            from algorithm.knowledge_extract import jev_extract

            runner = jev_extract.extract_aggregate
        else:
            runner = {
                ke_version.V2: v2.extract_aggregate,
                ke_version.V3: v3.extract_aggregate,
                ke_version.V31: v3_1.extract_aggregate,
                ke_version.LATEST: latest.extract_aggregate,
            }[label]
    # v2 / v3.1 入口要求 v2 的 ExtractionChunk 实例；v3/latest 的入口只认各自包内的
    # ExtractionChunk 或字段 dict，这里给它们字段 dict，由版本包自行 from_dict。
    # jev_extract 仍是旧窗口抽取副本，入口要求 v2 的 ExtractionChunk。
    chunks = (
        build_chunks(texts, ranges, metadata)
        if label in {ke_version.V2, ke_version.V31, ke_version.JEV}
        else _chunk_payloads(texts, ranges, metadata)
    )
    configured: dict[str, Any] = {}
    if extraction_config and label != ke_version.JEV:
        from algorithm.knowledge_extract.request_runtime import create_request_llm

        configured = {"llm": create_request_llm(extraction_config), "use_scheduler": False}
        model = extraction_config["model"]
        max_workers = extraction_config["max_concurrency"]
        if label == ke_version.V31:
            configured.update(request_timeout=extraction_config["request_timeout"],
                              document_timeout=extraction_config["document_timeout"])
    result = runner(
        ontology=ontology,
        chunks=chunks,
        target_types=list(target_types) if target_types is not None else None,
        validation_mode="permissive",
        best_effort=True,
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_workers=max_workers,
        **configured,
    )
    # 盖戳供 _quality_summary 透传，评测侧据此区分结果出自哪版算法。
    result["extract_version"] = concrete_version
    if extraction_config:
        from backend.app.services.extraction_config import public_config

        result["effective_config"] = public_config(extraction_config)
    return result


def _uid_for(type_id: Any, identity: Any) -> str:
    """Reproduce ``GraphAssembler.uid_for`` so objects can be matched to nodes."""
    from algorithm.knowledge_extract.v2.graph_utils import entity_uid

    return entity_uid(type_id, identity, None)


def _positions_from_evidence(
    evidence: Iterable[Any] | None,
    positions: dict[str, int],
) -> set[int]:
    found: set[int] = set()
    for item in evidence or ():
        if not isinstance(item, dict):
            continue
        for block_id in item.get("source_block_ids") or ():
            position = positions.get(str(block_id))
            if position is not None:
                found.add(position)
    return found


def _object_buckets(
    result: dict[str, Any],
    positions: dict[str, int],
) -> tuple[dict[str, set[int]], dict[str, str]]:
    """Map every accepted object to the chunk positions that anchor it."""
    model = result.get("intermediate_model") or {}
    object_positions: dict[str, set[int]] = {}
    uid_by_object: dict[str, str] = {}
    for obj in model.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        object_id = str(obj.get("id") or "")
        uid_by_object[object_id] = _uid_for(
            obj.get("type"),
            obj.get("identity") or obj.get("display_name"),
        )
        bucket = _positions_from_evidence(obj.get("evidence"), positions)
        # A deterministic supplement can produce an entity whose evidence sits
        # outside the scanned chunks; anchor it to the first chunk rather than
        # dropping it from every subgraph.
        object_positions[object_id] = bucket or {0}
    return object_positions, uid_by_object


def _resolve_position(raw_chunk: Any, positions: dict[str, int]) -> int | None:
    """Attribute an attempt's ``chunk`` field to an input position.

    V2 reports per-chunk attempts as bare chunk ids but the property pass as
    ``f"{chunk_id}:batch-{n}"``, so an exact match is tried first and the
    longest matching chunk-id prefix second.
    """
    value = str(raw_chunk or "")
    if not value:
        return None
    if value in positions:
        return positions[value]
    best: int | None = None
    best_length = -1
    for chunk_id, position in positions.items():
        if value.startswith(chunk_id) and len(chunk_id) > best_length:
            best, best_length = position, len(chunk_id)
    return best


def _compressed_attempts(attempts: Iterable[Any]) -> list[dict[str, Any]]:
    """Keep the attempt fields ``_extraction_error`` inspects."""
    compressed: list[dict[str, Any]] = []
    for attempt in attempts or ():
        if not isinstance(attempt, dict):
            continue
        compressed.append(
            {
                "attempt": attempt.get("attempt"),
                "type": attempt.get("type"),
                "pass": attempt.get("pass"),
                "chunk": attempt.get("chunk"),
                "message": attempt.get("message"),
                "response_status": attempt.get("response_status"),
            }
        )
    return compressed


def _failure_plan(
    result: dict[str, Any],
    positions: dict[str, int],
    total: int,
) -> tuple[set[int], str, str | None]:
    """Decide which item positions failed and with which code.

    ``status == "succeeded"`` means no position failed.  Otherwise every
    terminal failure is attributed to the chunk it names; a failure that cannot
    be attributed to any chunk (for example ``chunk="final"``) fails the whole
    batch, which keeps the documented "any chunk failing fails the request"
    behaviour.
    """
    status = str(result.get("status") or "")
    error_code = str(result.get("error_code") or "")
    if status == "succeeded" and not error_code:
        return set(), "", None

    failed: set[int] = set()
    unattributed = False
    for attempt in result.get("attempts") or ():
        if not isinstance(attempt, dict) or attempt.get("terminal") is not True:
            continue
        if str(attempt.get("type") or "") not in _FATAL_ATTEMPT_TYPES:
            continue
        if str(attempt.get("pass") or "") in _NON_FATAL_PASSES:
            continue
        position = _resolve_position(attempt.get("chunk"), positions)
        if position is None:
            unattributed = True
        else:
            failed.add(position)

    # An attempt that names no chunk is a document-level failure, and so is a
    # run that reports ``failed`` outright: both fail the whole batch, which is
    # the documented contract.  "No qualifying attempt at all" is *not* the
    # same thing -- it means nothing fatal happened.  Treating it as
    # unattributable used to fail every chunk of a merely ``partial`` run and
    # discard the entities that run did extract, so an ontology with fewer
    # classes (no relations to find, hence no relation pass output) produced
    # nothing at all instead of producing less.
    if unattributed or status == "failed":
        return set(range(total)), "", error_code or "EXTRACTION_EXCEPTION"
    return failed, "", error_code or "EXTRACTION_EXCEPTION"


def project_items(
    result: dict[str, Any],
    *,
    texts: Sequence[str],
    ranges: Sequence[Sequence[int] | None] | None = None,
    metadata: Sequence[dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """Project the document-level V2 result back onto one item per chunk."""
    total = len(texts)
    positions = {_chunk_id(position): position for position in range(total)}
    for child, parent in (result.get("profile", {}).get("chunk_source_ids") or {}).items():
        if parent in positions:
            positions[child] = positions[parent]

    graph = result.get("graph") or {}
    node_by_uid = {
        str(node.get("uid") or ""): node
        for node in graph.get("nodes") or []
        if isinstance(node, dict) and node.get("uid")
    }
    relationships = [rel for rel in graph.get("relationships") or [] if isinstance(rel, dict)]

    object_positions, uid_by_object = _object_buckets(result, positions)

    # A node belongs to every chunk that anchors one of the objects mapping to
    # it; nodes with no known origin fall back to the first chunk.
    node_positions: dict[str, set[int]] = {uid: set() for uid in node_by_uid}
    for object_id, object_uid in uid_by_object.items():
        if object_uid in node_positions:
            node_positions[object_uid] |= object_positions.get(object_id) or {0}
    for uid, buckets in node_positions.items():
        if not buckets:
            node_positions[uid] = {0}

    # Prefer the relationship's own evidence for attribution.  Legacy graphs
    # without relationship provenance retain endpoint-based projection, marked
    # explicitly below as inferred.  Endpoint nodes are added later as closure
    # context, without rewriting their evidence-derived source positions.
    relationship_buckets: list[set[int]] = []
    relationship_sources: list[set[int]] = []
    for relationship in relationships:
        evidence = relationship.get("evidences")
        if not isinstance(evidence, list):
            singular = relationship.get("evidence")
            evidence = [singular] if isinstance(singular, dict) else []
        source_buckets = _positions_from_evidence(evidence, positions)
        inferred_buckets = node_positions.get(
            str(relationship.get("from_uid") or ""), set()
        ) | node_positions.get(str(relationship.get("to_uid") or ""), set())
        relationship_sources.append(source_buckets)
        relationship_buckets.append(source_buckets or inferred_buckets or {0})

    failures, _, error_code = _failure_plan(result, positions, total)
    attempts = _compressed_attempts(result.get("attempts"))
    # Attribute attempts by the chunk each one names, and report them whether or
    # not that chunk ended up failed.  Reporting them only on failed chunks made
    # a lost property pass indistinguishable from a run that never called the
    # model at all -- ``attempts: []`` on a ``partial`` run means "nothing
    # failed", not "nothing was tried".
    attempts_by_position: dict[int, list[dict[str, Any]]] = {position: [] for position in range(total)}
    document_attempts: list[dict[str, Any]] = []
    for attempt in attempts:
        position = _resolve_position(attempt.get("chunk"), positions)
        if position is None:
            document_attempts.append(attempt)
        else:
            attempts_by_position[position].append(attempt)
    warnings = result.get("warnings") or []
    profile = result.get("profile") or {}
    raw = result.get("raw") or {}
    quality = _quality_summary(result)

    items: list[dict[str, Any]] = []
    for position in range(total):
        projected_relationships: list[dict[str, Any]] = []
        for relationship, buckets, sources in zip(
            relationships,
            relationship_buckets,
            relationship_sources,
        ):
            if position not in buckets:
                continue
            projected = deepcopy(relationship)
            projected["chunk_indices"] = sorted(sources)
            projected["provenance_status"] = (
                "evidence" if sources else "inferred_from_endpoints"
            )
            projected_relationships.append(projected)
        required_uids = {
            str(endpoint)
            for relationship in projected_relationships
            for endpoint in (relationship.get("from_uid"), relationship.get("to_uid"))
            if endpoint
        }
        projected_nodes: list[dict[str, Any]] = []
        for uid, node in node_by_uid.items():
            if position not in node_positions.get(uid, set()) and uid not in required_uids:
                continue
            projected = deepcopy(node)
            # Closure copies must not rewrite provenance when the per-chunk
            # graphs are merged again.  Preserve the evidence-derived source
            # positions explicitly instead of treating projection presence as
            # a new source observation.
            projected["chunk_indices"] = sorted(node_positions.get(uid) or {0})
            projected_nodes.append(projected)
        graph_for_chunk = {
            "nodes": projected_nodes,
            "relationships": projected_relationships,
        }
        failed = position in failures
        items.append(
            {
                "index": position,
                "text": texts[position],
                "ranges": list(ranges[position]) if ranges is not None and position < len(ranges) and ranges[position] else None,
                "metadata": _infer_table_metadata(
                    texts[position],
                    deepcopy(metadata[position] or {}) if metadata is not None and position < len(metadata) else {},
                ),
                "raw": raw,
                "graph": graph_for_chunk,
                "attempts": attempts_by_position[position] + document_attempts,
                "warnings": warnings,
                "profile": profile,
                "quality": deepcopy(quality),
                "error": (result.get("_error") or "知识抽取失败") if failed else None,
                "error_code": error_code if failed else None,
                # V2 does not retain raw provider responses.
                "raw_response": "",
            }
        )
    logger.info(
        "%sknowledge extract %s run: status=%s error_code=%s chunks=%d failed_chunks=%d nodes=%d",
        observability.context_prefix(request_id=observability.get_request_id()),
        result.get("extract_version") or "v2",
        result.get("status"),
        result.get("error_code"),
        total,
        len(failures),
        len(node_by_uid),
    )
    return items


def extract_chunks(
    ontology: str,
    chunks: list[str],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    target_types: Iterable[str] | None = None,
    max_workers: int | None = None,
    ranges: Sequence[Sequence[int] | None] | None = None,
    metadata: Sequence[dict[str, Any] | None] | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
    extraction_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Version-switched replacement for V1's ``extract_chunks``.

    Keeps V1's guarantees: one item per input chunk, and never raises.
    A misconfigured ``KG_EXTRACT_ALGO_VERSION`` is the one exception: it is a
    configuration error, so it propagates instead of becoming per-chunk failures.
    """
    if not chunks:
        return []
    version = extraction_config["version"] if extraction_config else ke_version.get_extract_version()
    try:
        result = run_aggregate(
            ontology,
            chunks,
            model=model,
            base_url=base_url,
            api_key=api_key,
            target_types=target_types,
            max_workers=max_workers,
            ranges=ranges,
            metadata=metadata,
            runner=runner,
            extraction_config=extraction_config,
        )
    except Exception as exc:  # noqa: BLE001 - mirror V1's per-call error containment
        logger.warning(
            "%sknowledge extract %s call failed: %s",
            observability.context_prefix(request_id=observability.get_request_id()),
            version,
            exc,
        )
        # V2 的结构化错误（如 ONTOLOGY_EMPTY）带 error_code，原样透传避免被泛化。
        error_code = str(getattr(exc, "error_code", "") or "EXTRACTION_EXCEPTION")
        message = str(exc).strip() or "知识抽取调用异常"
        return [
            {
                "index": position,
                "text": text,
                "ranges": list(ranges[position]) if ranges is not None and position < len(ranges) and ranges[position] else None,
                "metadata": _infer_table_metadata(
                    text,
                    deepcopy(metadata[position] or {}) if metadata is not None and position < len(metadata) else {},
                ),
                "raw": {},
                "graph": _empty_graph(),
                "attempts": [{"attempt": 1, "type": "exception", "message": message}],
                "warnings": [],
                "profile": {},
                "quality": {
                    "status": "failed",
                    "error_code": error_code,
                    "verification": {},
                    "graph_publishable": False,
                    "repair_stop_reason": None,
                    "extract_version": version,
                },
                "error": message,
                "error_code": error_code,
                "raw_response": "",
            }
            for position, text in enumerate(chunks)
        ]
    return project_items(result, texts=chunks, ranges=ranges, metadata=metadata)


def run_single_document(
    ontology: str,
    text: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Run the configured extraction version over one text for the MCP tool.

    Success returns the ``raw`` / ``graph`` / ``warnings`` / ``attempts`` /
    ``profile`` keys V1 exposed.  Failure returns ``_error`` so
    ``normalize_tool_result`` still maps it onto ``KNOWLEDGE_FAILED`` --
    except a verification-failed (non-``failed``) run with
    ``KG_EXTRACT_PUBLISH_BEST_EFFORT`` on, which publishes the best-effort
    graph plus quality markers, matching the HTTP extraction contract.
    """
    result = run_aggregate(
        ontology,
        [text],
        model=model,
        base_url=base_url,
        api_key=api_key,
    )
    status = str(result.get("status") or "")
    quality = _quality_summary(result)
    if status != "succeeded" or result.get("error_code"):
        verification_failed = quality["graph_publishable"] is False and status != "failed"
        if verification_failed and publish_best_effort_graph():
            # 与 HTTP 抽取响应一致的发布语义：verification 未过仍返回 best-effort
            # 图谱 + 质量标记（graph_publishable=false 供调用方判断可信度），
            # 而不是把整篇作废成工具错误；status=failed（无可用结果）维持 _error。
            return {
                "raw": result.get("raw") or {},
                "graph": result.get("graph") or _empty_graph(),
                "warnings": result.get("warnings") or [],
                "attempts": _compressed_attempts(result.get("attempts")),
                "profile": result.get("profile") or {},
                "status": quality["status"],
                "error_code": result.get("error_code") or None,
                "quality": quality,
                "verification": deepcopy(quality["verification"]),
            }
        return {
            "_error": result.get("_error") or "知识抽取失败",
            "error_code": result.get("error_code") or "EXTRACTION_EXCEPTION",
            "status": status,
            "attempts": _compressed_attempts(result.get("attempts")),
            "warnings": result.get("warnings") or [],
            "quality": quality,
            "verification": deepcopy(quality["verification"]),
        }
    return {
        "raw": result.get("raw") or {},
        "graph": result.get("graph") or _empty_graph(),
        "warnings": result.get("warnings") or [],
        "attempts": _compressed_attempts(result.get("attempts")),
        "profile": result.get("profile") or {},
        "status": status,
        "quality": quality,
        "verification": deepcopy(quality["verification"]),
    }


__all__ = [
    "build_chunks",
    "extract_chunks",
    "project_items",
    "publish_best_effort_graph",
    "run_aggregate",
    "run_single_document",
]
