"""Compact, evidence-preserving exports, independent of the strict API boundary."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any


def _json(value: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, separators=(",", ":"), allow_nan=False)


def _message_records(calls):
    """Accept bounded runtime traces or transport captures without transport headers."""
    for number, call in enumerate(calls, 1):
        captured = "request" in call
        request = call.get("request") or {}
        response = call.get("response") or {}
        choices = response.get("choices") or []
        choice = choices[0] if choices else {}
        assistant = choice.get("message") or {}
        response_present = "content" in assistant if captured else "response_text" in call
        content = assistant.get("content") if captured else call.get("response_text")
        inputs = request.get("messages") if captured else call.get("messages")
        # COSEC: Copy only model role/content, never request headers, URL, keys or SDK objects.
        messages = [{"role": m["role"], "content": deepcopy(m["content"])} for m in inputs or []]
        if response_present:
            messages.append({"role": "assistant", "content": content})
        yield {
            "call": number, "stage": call.get("stage", call.get("pass")),
            "attempt": call.get("attempt"), "chunk": call.get("chunk"),
            "status": call.get("status", response.get("status")),
            "finish_reason": choice.get("finish_reason") if captured else call.get("finish_reason"),
            "elapsed_seconds": call.get("elapsed_seconds"),
            "usage": response.get("usage") if captured else call.get("usage"),
            "messages": messages,
            "request_available": inputs is not None,
            "response_available": response_present,
        }


def build_artifacts(result: dict[str, Any], *, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Project an aggregate without dropping partial facts or reclassifying quality.

    This is an export, not a validator or a publish gate. Failed runs also retain
    their facts for inspection; consumers must read summary.status/verification.
    """
    raw = result.get("raw")
    if not isinstance(raw, dict) or not isinstance(raw.get("entities"), list):
        raise ValueError("Expected a V3.1 aggregate with raw.entities")
    if raw.get("relations"):
        raise ValueError("V3.1 compact artifacts do not support relations")
    trace = result.get("run_trace") or {}
    status = result.get("status", trace.get("status"))
    if status not in {"succeeded", "partial", "failed"}:
        raise ValueError("Aggregate must have an explicit terminal status")
    evidence_by_key: dict[str, dict[str, Any]] = {}

    def evidence_refs(spans):
        refs = []
        for span in spans:
            if not span:
                continue
            # COSEC: Only evidence fields cross the export boundary; metadata stays internal.
            source = {k: deepcopy(span[k]) for k in
                      ("span_id", "source_block_ids", "quote", "start_char", "end_char", "evidence_type") if k in span}
            key = _json(source, sort_keys=True)
            if key not in evidence_by_key:
                evidence_by_key[key] = {"kind": "source", "id": "ev-" + hashlib.sha256(key.encode()).hexdigest()[:24], **source}
            ref = evidence_by_key[key]["id"]
            if ref not in refs:
                refs.append(ref)
        return refs

    entities = []
    bindings = []
    for entity in raw["entities"]:
        item = {"type": entity["type"], "name": entity["name"],
                "properties": deepcopy(entity.get("properties") or {})}
        binding = {"kind": "entity_evidence", "type": entity["type"], "name": entity["name"],
                   "evidence_ids": evidence_refs([entity.get("evidence"), *(entity.get("evidences") or [])])}
        entity_receipt = entity.get('entity_evidence')
        if entity_receipt:
            binding['entity_support'] = {k: deepcopy(entity_receipt[k])
                                         for k in ('name', 'type', 'support') if k in entity_receipt}
            binding['entity_support']['evidence_ids'] = evidence_refs(entity_receipt.get('sources') or [])
        receipts = {}
        for prop, receipt in (entity.get("property_evidence") or {}).items():
            entry = {"support": receipt.get("support"), "evidence_ids": evidence_refs(receipt.get("sources") or [])}
            # A failed aggregate can have a mismatched receipt; never silently bless its new value.
            if receipt.get("value") != item["properties"].get(prop):
                entry["value"] = deepcopy(receipt.get("value"))
            receipts[prop] = entry
        if receipts:
            binding["property_evidence"] = receipts
        entities.append(item)
        bindings.append(binding)

    source_calls = (trace.get("calls") or []) if calls is None else calls
    messages = list(_message_records(source_calls))
    recorded = len(messages)
    expected = max(recorded, len(trace.get("calls") or []), (result.get("profile") or {}).get("llm_calls", 0))
    complete = sum(m["request_available"] and m["response_available"] for m in messages)
    verification = deepcopy(result.get("verification", trace.get("verification")) or {})
    # Coverage diagnostics belong in one place, even on failed/incomplete runs.
    gaps = result.get("coverage_gaps") or verification.get("coverage_gaps") or []
    verification.pop("coverage_gaps", None)
    summary = {
        "format_version": "v3.1-artifacts-2", "run_id": trace.get("run_id"),
        "status": status, "error_code": result.get("error_code", trace.get("error_code")),
        "error": result.get("_error", result.get("error", trace.get("error"))),
        "verification": verification, "coverage_gaps": deepcopy(gaps),
        "coverage_ledger": deepcopy(trace.get("coverage_ledger") or []),
        "counts": {"entities": len(entities), "properties": sum(len(e["properties"]) for e in entities),
                   "evidence": len(evidence_by_key), "evidence_bindings": len(bindings)},
        "warnings": deepcopy(result.get("warnings") or []),
        "attempts": deepcopy(result.get("attempts", trace.get("attempts")) or []),
        "stop_reason": trace.get("stop_reason"), "elapsed_seconds": trace.get("elapsed_seconds"),
        "model": trace.get("model"), "prompt_version": trace.get("prompt_version"),
        "input_fingerprints": {k: trace[k] for k in ("ontology_sha256", "markdown_sha256") if k in trace},
        "message_trace": {"calls": expected, "recorded_calls": recorded, "complete_calls": complete,
                          "complete": recorded == expected == complete},
        "files": {"entities": "entities.jsonl", "evidence": "evidence.jsonl", "messages": "messages.jsonl"},
    }
    return {"entities": entities, "evidence": [*evidence_by_key.values(), *bindings],
            "summary": summary, "messages": messages}


def export_artifacts(result: dict[str, Any], output_dir: str | Path, *,
                     calls: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Write a fresh artifact directory; summary is the final completion marker.

    output_dir is a trusted caller-selected local destination, never model input.
    Existing directories (including symlinks) are rejected instead of overwritten.
    """
    artifacts = build_artifacts(result, calls=calls)
    # Serialize before touching disk; invalid values must not leave an apparent success.
    contents = {f"{key}.jsonl": "".join(_json(row) + "\n" for row in artifacts[key])
                for key in ("entities", "evidence", "messages")}
    contents["summary.json"] = json.dumps(artifacts["summary"], ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    destination = Path(output_dir).absolute()
    # COSEC: Fixed filenames in a newly created directory, with exclusive creation of each file.
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in contents.items():
        with (destination / name).open("x", encoding="utf-8") as handle:
            handle.write(content)
    return {name: str(destination / name) for name in contents}
