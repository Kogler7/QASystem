"""Adapter exposing Knowledge Extract V2 through the V1 batch contract.

``GraphExtractionService`` and the MCP extraction tool were written against
``algorithm.knowledge_extract`` (V1), whose batch entry point takes one plain
string per chunk and returns one result item per chunk.  V2 is an aggregate
pipeline: it consumes ``ExtractionChunk`` objects, runs one pass over the whole
document, and returns a single merged graph.

This module bridges the two without touching V2.  It runs V2 once, then
projects the document-level result back onto the individual chunks using each
entity's evidence anchor, so the caller keeps seeing one item per chunk and
``deduplicate_chunk_graphs`` can still rebuild the merged graph (and its
``chunk_indices``) exactly as before.

Two deliberate choices worth knowing about:

* ``validation_mode="permissive"``.  In strict mode V2 turns *any* verification
  hard issue (a single unlocatable evidence quote, one duplicate entity) into a
  terminal failure for the whole run.  V1 only ever reported those as warnings,
  so strict mode would convert ordinary documents into failed requests.
* ``best_effort=True``.  This only affects the reported ``status``; it lets a
  partially successful run stay ``partial`` instead of ``failed`` so chunk-level
  failure attribution remains meaningful.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

CHUNK_ID_PREFIX = "chunk-"

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


def _chunk_id(position: int) -> str:
    return f"{CHUNK_ID_PREFIX}{position:04d}"


def build_chunks(
    texts: Sequence[str],
    ranges: Sequence[Sequence[int] | None] | None = None,
) -> list[Any]:
    """Wrap plain chunk texts as the ``ExtractionChunk`` objects V2 requires.

    Chunk text is passed through untouched.  Callers must not pre-strip Markdown
    headings: V2's structural extractors rely on the ``#`` markers.
    """
    from algorithm.knowledge_extract_v2.chunk_context import ExtractionChunk

    chunks = []
    for position, text in enumerate(texts):
        span = ranges[position] if ranges is not None and position < len(ranges) else None
        chunks.append(
            ExtractionChunk(
                chunk_id=_chunk_id(position),
                text=text or "",
                start_offset=span[0] if span else None,
                end_offset=span[1] if span else None,
            )
        )
    return chunks


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
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the V2 pipeline once over every chunk and return its raw result."""
    if runner is None:
        from algorithm.knowledge_extract_v2 import extract_harness

        runner = extract_harness
    return runner(
        ontology=ontology,
        chunks=build_chunks(texts, ranges),
        target_types=list(target_types) if target_types is not None else None,
        validation_mode="permissive",
        best_effort=True,
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_workers=max_workers,
    )


def _uid_for(type_id: Any, identity: Any) -> str:
    """Reproduce ``GraphAssembler.uid_for`` so objects can be matched to nodes."""
    from algorithm.knowledge_extract_v2.graph_utils import entity_uid

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
) -> list[dict[str, Any]]:
    """Project the document-level V2 result back onto one item per chunk."""
    total = len(texts)
    positions = {_chunk_id(position): position for position in range(total)}

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

    # Graph relationships are deduplicated triples and carry no back-reference
    # to the intermediate relation, so they are bucketed by their endpoints.
    # Both endpoints contribute, which keeps every projected subgraph closed:
    # it never references a node it does not contain.
    relationship_buckets: list[set[int]] = []
    for relationship in relationships:
        buckets = node_positions.get(str(relationship.get("from_uid") or ""), set()) | node_positions.get(
            str(relationship.get("to_uid") or ""), set()
        )
        relationship_buckets.append(buckets or {0})

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

    items: list[dict[str, Any]] = []
    for position in range(total):
        graph_for_chunk = {
            "nodes": [
                node for uid, node in node_by_uid.items() if position in node_positions.get(uid, set())
            ],
            "relationships": [
                relationship
                for relationship, buckets in zip(relationships, relationship_buckets)
                if position in buckets
            ],
        }
        failed = position in failures
        items.append(
            {
                "index": position,
                "text": texts[position],
                "ranges": list(ranges[position]) if ranges is not None and position < len(ranges) and ranges[position] else None,
                "raw": raw,
                "graph": graph_for_chunk,
                "attempts": attempts_by_position[position] + document_attempts,
                "warnings": warnings,
                "profile": profile,
                "error": (result.get("_error") or "知识抽取失败") if failed else None,
                "error_code": error_code if failed else None,
                # V2 does not retain raw provider responses.
                "raw_response": "",
            }
        )
    logger.info(
        "knowledge extract v2 run: status=%s error_code=%s chunks=%d failed_chunks=%d nodes=%d",
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
    runner: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """V2-backed replacement for V1's ``extract_chunks``.

    Keeps V1's guarantees: one item per input chunk, and never raises.
    """
    if not chunks:
        return []
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
            runner=runner,
        )
    except Exception as exc:  # noqa: BLE001 - mirror V1's per-call error containment
        logger.warning("knowledge extract v2 call failed: %s", exc)
        return [
            {
                "index": position,
                "text": text,
                "ranges": list(ranges[position]) if ranges is not None and position < len(ranges) and ranges[position] else None,
                "raw": {},
                "graph": _empty_graph(),
                "attempts": [{"attempt": 1, "type": "exception", "message": str(exc)}],
                "warnings": [],
                "profile": {},
                "error": "知识抽取调用异常",
                "error_code": "EXTRACTION_EXCEPTION",
                "raw_response": "",
            }
            for position, text in enumerate(chunks)
        ]
    return project_items(result, texts=chunks, ranges=ranges)


def run_single_document(
    ontology: str,
    text: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Run V2 over one text and return a compact result for the MCP tool.

    Success returns the ``raw`` / ``graph`` / ``warnings`` / ``attempts`` /
    ``profile`` keys V1 exposed.  Failure returns ``_error`` so
    ``normalize_tool_result`` still maps it onto ``KNOWLEDGE_FAILED``.
    """
    result = run_aggregate(
        ontology,
        [text],
        model=model,
        base_url=base_url,
        api_key=api_key,
    )
    status = str(result.get("status") or "")
    if status != "succeeded" or result.get("error_code"):
        return {
            "_error": result.get("_error") or "知识抽取失败",
            "error_code": result.get("error_code") or "EXTRACTION_EXCEPTION",
            "status": status,
            "attempts": _compressed_attempts(result.get("attempts")),
            "warnings": result.get("warnings") or [],
        }
    return {
        "raw": result.get("raw") or {},
        "graph": result.get("graph") or _empty_graph(),
        "warnings": result.get("warnings") or [],
        "attempts": _compressed_attempts(result.get("attempts")),
        "profile": result.get("profile") or {},
        "status": status,
    }


__all__ = [
    "build_chunks",
    "extract_chunks",
    "project_items",
    "run_aggregate",
    "run_single_document",
]
