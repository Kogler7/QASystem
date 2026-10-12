"""Property support is independent of entity-name/source binding."""
from __future__ import annotations

import re
from copy import deepcopy

from .chunk_context import ChunkContextManager, ExtractionChunk
from .evidence import locate_quote
from .graph_utils import append_warning
from .request_budget import fits
from .semantic_evidence import VERIFICATION_SCHEMA, build_verification_messages


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


def _literal_supported(value, name, quotes, other_names):
    # Only direct, positive, unambiguous literal claims use the cheap path.
    # Boolean/numeric normalization and paraphrases require a semantic verdict.
    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(v, str) or not v for v in values):
        return False
    for value in values:
        if not any(name in sentence and value in sentence
                   and not re.search(r"不|未|无|非|禁止|不得|并非|\b(?:not|never|no)\b", sentence, re.I)
                   and not any(other != name and other in sentence for other in other_names)
                   for quote in quotes for sentence in re.split(r"[。；;，,\n]", quote)):
            return False
    return True


def ground_properties(rows, sources, llm, *, attempts, warnings, use_scheduler=False,
                      max_attempts=3, retry_base_seconds=0.05, call_metrics=None, prior=None, plan=None):
    """Remove unsupported values; accept semantic values only after explicit yes.

    Receipts are created here, never supplied by the extraction model. A forged
    quote cannot be repaired by falling back to the entity's name.
    """
    from .candidate_generation import generate_structured
    pending = []
    names = {row["name"] for row in rows}
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
            meaningful = any(span["quote"].strip() != row["name"] or len(span["quote"]) > 32 for span in spans)
            previous = (prior or {}).get("properties", {}).get(prop)
            changed = previous is not None and previous != value
            if supported and not changed and _literal_supported(value, row["name"], [s["quote"] for s in spans], names):
                row["property_evidence"][prop] = {"value": deepcopy(value), "support": "literal", "sources": spans}
                continue
            if supported and meaningful:
                pending.append((row, prop, value, spans, {
                    "item_id": f"{index}:{prop}", "kind": "property", "type": row["type"],
                    "name": row["name"], "property": prop, "value": value,
                    "property_description": ({"display_name": specs[prop].display_name, "comment": (specs[prop].comment or "")[:500]} if prop in specs else {}),
                    "evidence_quotes": [s["quote"] for s in spans],
                    **({"previous_value": previous} if changed else {}),
                }))
            else:
                row["properties"].pop(prop)
                append_warning(warnings, "PROPERTY_EVIDENCE_UNSUPPORTED",
                    "属性引文缺失、无法定位或仅为实体名，已移除该属性", f"{row['type']}::{row['name']}.{prop}")

    context = ExtractionChunk("property-support", "\n\n".join(source.text for source in sources))

    def verify(batch):
        messages = build_verification_messages(chunk=context, items=[item[-1] for item in batch])
        if len(batch) > 1 and (len(batch) > 12 or not fits(messages, llm)):
            middle = len(batch) // 2
            verify(batch[:middle])
            verify(batch[middle:])
            return
        expected = {item[-1]["item_id"] for item in batch}

        def validate(payload):
            ids = [v["item_id"] for v in payload.get("verdicts", [])]
            return [] if set(ids) == expected and len(ids) == len(expected) else ["verdicts 必须逐项且仅一次覆盖所有 item_id"]

        parsed = generate_structured(llm, messages, VERIFICATION_SCHEMA,
            use_scheduler=use_scheduler, attempts=attempts, pass_name="property_verification",
            chunk_id=sources[0].chunk_id, max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds, call_metrics=call_metrics, validator=validate)
        verdicts = {v["item_id"]: v["supported"] for v in (parsed or {}).get("verdicts", [])}
        for row, prop, value, spans, item in batch:
            if verdicts.get(item["item_id"]) == "yes":
                row["property_evidence"][prop] = {"value": deepcopy(value), "support": "llm", "sources": merge_evidences(spans,
                    ((prior or {}).get("property_evidence", {}).get(prop) or {}).get("sources", []))}
            else:
                row["properties"].pop(prop, None)
                append_warning(warnings, "PROPERTY_EVIDENCE_UNSUPPORTED",
                    "属性语义核验未确认支持，已移除该属性", f"{row['type']}::{row['name']}.{prop}")
    if pending:
        verify(pending)
    return bind_to_sources(rows, sources)
