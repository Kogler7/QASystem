"""Property support is independent of entity-name/source binding."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from .chunk_context import ChunkContextManager, ExtractionChunk
from .evidence import locate_quote
from .graph_utils import append_warning
from .request_budget import fits
from .semantic_evidence import build_verification_messages
from .output_contracts import verification_schema, validate_verification_items


def locate_in_sources(quote, sources):
    for source in sources:
        found = locate_quote(ChunkContextManager([source]), quote)
        if found is not None:
            payload = found.to_dict()
            offset = int(source.metadata.get("_v31_window_start", 0))
            payload["start_char"] += offset
            payload["end_char"] += offset
            payload["span_id"] = f"{source.chunk_id}:{payload['start_char']}:{payload['end_char']}"
            return payload
    return None


def merge_evidences(*collections):
    unique = {}
    for collection in collections:
        for evidence in collection:
            if evidence and evidence.get("span_id"):
                unique[evidence["span_id"]] = evidence
    return list(unique.values())


def bind_to_sources(rows, sources):
    for row in rows:
        evidence = row.get("evidence") or {}
        located = locate_in_sources(evidence.get("quote", ""), sources)
        if located is None:
            located = locate_in_sources(row.get("name", ""), sources)
        if located is not None:
            row["evidence"] = located
        row["evidences"] = merge_evidences(row.get("evidences", []), [located],
            [span for receipt in row.get("property_evidence", {}).values() for span in receipt["sources"]])
    return rows


def _verification_context(batch, sources):
    """Keep source coordinates and surrounding conditions, not entire documents.

    A quote locates evidence; it is not proof of subject/property entailment.
    Neighbouring text remains available to resolve pronouns and conditions.
    Oversized individual evidence is rejected by the common request budget.
    """
    excerpts = {}
    for _, _, _, spans, _ in batch:
        for span in spans:
            cid = span['source_block_ids'][0]
            for source in sources:
                base = int(source.metadata.get('_v31_window_start', 0))
                start, end = span['start_char'] - base, span['end_char'] - base
                if source.chunk_id != cid or not (0 <= start < end <= len(source.text)):
                    continue
                left, right = max(0, start - 200), min(len(source.text), end + 200)
                excerpts[(cid, base + left, base + right)] = replace(source,
                    text=source.text[left:right], metadata={**source.metadata, "_v31_window_start": base + left})
                break
    return list(excerpts.values())


def ground_properties(rows, sources, llm, *, attempts, warnings, use_scheduler=False,
                      max_attempts=3, retry_base_seconds=0.05, call_metrics=None, prior=None, plan=None,
                      route=None, assembly_planner=None):
    """Remove unsupported values; accept semantic values only after explicit yes.

    Receipts are created here, never supplied by the extraction model. A forged
    quote cannot be repaired by falling back to the entity's name.
    """
    from .candidate_generation import generate_structured
    pending = []
    for index, row in enumerate(rows):
        row["property_evidence"] = {}
        specs = {p.identifier: p for p in plan.class_specs[row["type"]].properties} if plan else {}
        evidence = row.get("evidence") or {}
        property_quotes = evidence.get("property_quotes") or {}
        for prop, value in list((row.get("properties") or {}).items()):
            if value in (None, "", []):
                row["properties"].pop(prop)
                continue
            quotes = property_quotes.get(prop) or [evidence.get("quote", "")]
            spans = [locate_in_sources(q, sources) for q in quotes]
            supported = bool(spans) and all(spans)
            spans = merge_evidences([span for span in spans if span])
            meaningful = any(span["quote"].strip() != row["name"] or len(span["quote"]) > 32
                             or value == row["name"] for span in spans)
            previous = (prior or {}).get("properties", {}).get(prop)
            changed = previous is not None and previous != value
            # Only a receipt produced earlier in this run can avoid another
            # semantic call. Textual cooccurrence says nothing about the field.
            receipt = ((prior or {}).get('property_evidence') or {}).get(prop) or {}
            if (previous == value and receipt.get('value') == value
                    and receipt.get('support') == 'llm' and receipt.get('sources')):
                row['property_evidence'][prop] = deepcopy(receipt)
                continue
            if supported and meaningful:
                pending.append((row, prop, value, spans, {
                    "item_id": f"{index}:{prop}", "kind": "property", "type": row["type"],
                    "name": row["name"], "property": prop, "value": value,
                    "property_description": ({"display_name": specs[prop].display_name, "comment": (specs[prop].comment or "")[:500]} if prop in specs else {}),
                    "evidence_quotes": [s["quote"] for s in spans],
                    "source_refs": [{"chunk_id": s["source_block_ids"][0],
                                     "start_char": s["start_char"], "end_char": s["end_char"]} for s in spans],
                    **({"previous_value": previous} if changed else {}),
                }))
            else:
                row["properties"].pop(prop)
                append_warning(warnings, "PROPERTY_EVIDENCE_UNSUPPORTED",
                    "属性引文缺失、无法定位或仅为实体名，已移除该属性", f"{row['type']}::{row['name']}.{prop}")

    def verify(batch):
        context = _verification_context(batch, sources)
        items = [item[-1] for item in batch]
        messages = build_verification_messages(chunk=context[0], source_chunks=context, items=items,
            route=route, assembly_planner=assembly_planner)
        if len(batch) > 1 and (len(batch) > 12 or not fits(messages, llm)):
            middle = len(batch) // 2
            verify(batch[:middle])
            verify(batch[middle:])
            return
        parsed = generate_structured(llm, messages, verification_schema(items),
            use_scheduler=use_scheduler, attempts=attempts, pass_name="property_verification",
            chunk_id=sources[0].chunk_id, max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds, call_metrics=call_metrics, validator=lambda payload: validate_verification_items(payload, items))
        verdicts = {v["item_id"]: v["supported"] for v in (parsed or {}).get("verdicts", [])}
        for row, prop, value, spans, item in batch:
            if verdicts.get(item["item_id"]) == "yes":
                row["property_evidence"][prop] = {"value": deepcopy(value), "support": "llm", "sources": merge_evidences(spans,
                    (((prior or {}).get("property_evidence", {}).get(prop) or {}).get("sources", [])
                     if (prior or {}).get("properties", {}).get(prop) == value else []))}
            else:
                row["properties"].pop(prop, None)
                append_warning(warnings, "PROPERTY_EVIDENCE_UNSUPPORTED",
                    "属性语义核验未确认支持，已移除该属性", f"{row['type']}::{row['name']}.{prop}")
    if pending:
        verify(pending)
    return bind_to_sources(rows, sources)
