"""Shadow reports for the jev extract copy. Facts are never rewritten here."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from algorithm.graph_align import shortlist
from algorithm.jev import CONTRACT_VERSION, DecisionService
from algorithm.jev.blocking import iter_coref_pairs
from algorithm.jev.config import get_jev_config
from algorithm.jev.contract import DOCUMENT_COREF, GRAPH_ALIGN, RELATION_SUPPORT
from algorithm.jev.routing import route
from algorithm.jev.service import Judgment


def attach_jev_shadow(
    result: dict[str, Any],
    *,
    existing_nodes: Sequence[Mapping[str, Any]] | None = None,
    service: DecisionService | None = None,
) -> dict[str, Any]:
    """Return ``result`` unchanged unless the master switch and a task flag are on.

    An attached ``jev`` object records proposals only. Entities, relations and
    the graph are the same objects as in ``result``.
    """
    config = get_jev_config()
    if not config["enabled"]:
        return result
    selected = {
        DOCUMENT_COREF: config["document_coref"],
        RELATION_SUPPORT: config["relation_support"],
        GRAPH_ALIGN: config["graph_align"],
    }
    if not any(selected.values()):
        return result
    resolved = service or DecisionService()
    tasks: dict[str, Any] = {}
    raw = result.get("raw") if isinstance(result.get("raw"), Mapping) else {}
    if selected[DOCUMENT_COREF]:
        entities = [item for item in (raw.get("entities") or []) if isinstance(item, Mapping)]
        tasks[DOCUMENT_COREF] = _score(DOCUMENT_COREF, iter_coref_pairs(entities), resolved)
    if selected[RELATION_SUPPORT]:
        relations = [item for item in (raw.get("relations") or []) if isinstance(item, Mapping)]
        tasks[RELATION_SUPPORT] = _score(RELATION_SUPPORT, _located_relations(relations), resolved)
    if selected[GRAPH_ALIGN]:
        if not existing_nodes:
            tasks[GRAPH_ALIGN] = {
                "status": "skipped",
                "reason": "no_existing_nodes",
                "candidate_count": 0,
                "proposals": [],
            }
        else:
            nodes = ((result.get("graph") or {}).get("nodes") or []) if isinstance(result.get("graph"), Mapping) else []
            payloads = shortlist([item for item in nodes if isinstance(item, Mapping)], list(existing_nodes))
            tasks[GRAPH_ALIGN] = _score(GRAPH_ALIGN, payloads, resolved)
    reported = dict(result)
    reported["jev"] = {"contract": CONTRACT_VERSION, "tasks": tasks}
    return reported


def _score(task_id: str, payloads: list[dict[str, Any]], service: DecisionService) -> dict[str, Any]:
    if not payloads:
        return {"status": "empty", "reason": "no_candidates", "candidate_count": 0, "proposals": []}
    prediction = service.predict(task_id, payloads)
    if prediction.status != "decided":
        return {
            "status": prediction.status,
            "reason": prediction.reason,
            "candidate_count": len(payloads),
            "proposals": [],
        }
    by_id = {item.state_id: item for item in prediction.judgments}
    routes = {state_id: route(item, task_id) for state_id, item in by_id.items()}
    annotations = {
        item["state_id"]: item
        for item in (_annotate_payloads(payloads, by_id, routes) if task_id == GRAPH_ALIGN else [])
    }
    proposals = [_proposal(task_id, payload, by_id.get(payload["state_id"]), routes, annotations) for payload in payloads]
    return {"status": "decided", "reason": "", "candidate_count": len(payloads), "proposals": proposals}


def _annotate_payloads(payloads, judgments, routes):
    from algorithm.graph_align.align import conflicting_properties

    rows = []
    for payload in payloads:
        judgment = judgments.get(payload["state_id"])
        action = routes.get(payload["state_id"], "keep")
        conflicts = conflicting_properties(payload["new_properties"], payload["existing_properties"])
        if judgment is None or action != "link":
            annotation = "new"
        elif conflicts:
            annotation = "conflict"
        else:
            annotation = "link"
        rows.append(
            {
                "state_id": payload["state_id"],
                "annotation": annotation,
                "existing_uid": payload["existing_uid"],
                "property_conflicts": conflicts,
            }
        )
    return rows


def _proposal(task_id, payload, judgment: Judgment | None, routes, annotations) -> dict[str, Any]:
    action = routes.get(payload["state_id"], "keep") if judgment is not None else "keep"
    proposal: dict[str, Any] = {
        "state_id": payload["state_id"],
        "choice": judgment.choice if judgment is not None else "",
        "confidence": judgment.confidence if judgment is not None else 0.0,
        "route": action,
    }
    if task_id == DOCUMENT_COREF:
        proposal["left"] = {"type": payload["type"], "name": payload["left_name"]}
        proposal["right"] = {"type": payload["type"], "name": payload["right_name"]}
    elif task_id == RELATION_SUPPORT:
        proposal["left"] = {"type": payload["source_type"], "name": payload["source_name"]}
        proposal["right"] = {"type": payload["target_type"], "name": payload["target_name"]}
        proposal["relation"] = payload["relation"]
    else:
        proposal["left"] = {"type": payload["type"], "name": payload["new_name"]}
        proposal["right"] = {"type": payload["type"], "name": payload["existing_name"], "uid": payload["existing_uid"]}
        extra = annotations.get(payload["state_id"]) or {}
        proposal["annotation"] = extra.get("annotation", "new")
        proposal["existing_uid"] = payload["existing_uid"]
        proposal["property_conflicts"] = extra.get("property_conflicts", [])
    return proposal


def _located_relations(relations: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relation in relations:
        source = str(relation.get("from_name") or "")
        target = str(relation.get("to_name") or "")
        window = ""
        for ref in relation.get("source_refs") or []:
            if not isinstance(ref, Mapping):
                continue
            quote = str(ref.get("quote") or "")
            if source and target and source in quote and target in quote:
                window = quote
                break
        if not window:
            continue
        relation_id = str(relation.get("rel_type") or "")
        rows.append(
            {
                "state_id": f"{source}\x1f{relation_id}\x1f{target}",
                "window": window,
                "source_name": source,
                "source_type": str(relation.get("from_type") or ""),
                "target_name": target,
                "target_type": str(relation.get("to_type") or ""),
                "relation": relation_id,
                "relation_note": str(relation.get("relation_note") or ""),
            }
        )
    return rows
