"""Pre-extraction structural skeleton for standard documents.

The skeleton is built deterministically *before* the LLM passes, so that:

* the entity/relation prompts can carry high-level naming conventions and the
  canonical heading/clause inventory (instead of the model inventing names);
* the canonical Standard/Section/Subclause/Clause/Annex/Element entities and
  structural relations can be added to the pool early;
* the final alignment pass only needs to do global merge, summary naming and a
  small amount of normalization.

The skeleton reuses the convention builder in ``standard_alignment`` but keeps
only structural entity/relation types; semantic entities (Activity/Property/
Constrain/Terminology/...) remain owned by the LLM passes.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .candidate_models import EntityCandidate, RelationCandidate
from .evidence import Evidence
from .graph_utils import canonical_name
from .standard_alignment import align_standard_graph, norm

SKELETON_ENTITY_TYPES = {
    "Standard", "Section", "Subclause", "Clause", "Annex",
    "Normative Element", "Informative Element", "Additional Information",
    "ReferencedDocuments", "Reference", "InformationUnit", "Figure", "Table",
}
SKELETON_RELATION_TYPES = {
    "hasStructuralElement", "hasSubClause", "hasClause", "hasNormativeElement",
    "hasNote", "hasRepresentationForm", "referencesExternalResource",
}

_HINT_RULES = """【文档结构骨架与命名约定（本文件的规范名，请优先直接使用）】
- 整个文档只保留一个 Standard 实体，名称必须使用上面的主标准名；
- Section/Subclause/Clause/Annex/Normative Element/Informative Element 直接使用上面的规范名；
  条款命名规则为 "<编号> <标题>条款"，附加说明命名规则为 "<章节号>：<不超过12字的概括>"；
- Activity 命名：技术标准（有性能要求章节）用 "<项目>检验/试验"；条款式/管理类标准用 "<条款>规定的活动"；
- Property 只写被测量/被陈述的特性名（不超过8字），不要带要求/条件/方法等后缀；
- Constrain 只收规范性要求（应/不应/不超过/不低于/范围/条件），命名尽量为 "<特性><上限|下限|极差|范围>"；
- 要求条款指向对应活动用 describesAction；条款内部引用试验方法用 referencesClause 指向具体子条款（不要指向章号）；
- 规范性引用文件使用 ReferencedDocuments，参考文献使用 Reference + InformationUnit，图/表/公式用 Figure/Table/Equation。"""


def _resolve_type(plan: Any, name: str) -> str | None:
    specs = getattr(plan, "class_specs", {}) or {}
    target = canonical_name(name)
    for key in specs:
        if canonical_name(key) == target:
            return key
    for declared in getattr(plan, "allowed_types", ()) or ():
        if canonical_name(declared) == target:
            return declared
    return None


def _resolve_relation(plan: Any, name: str) -> str | None:
    specs = getattr(plan, "relation_specs", {}) or {}
    target = canonical_name(name)
    for key in specs:
        if canonical_name(key) == target:
            return key
    for specs_list in specs.values():
        for spec in specs_list or ():
            rel_id = canonical_name(getattr(spec, "relation_id", "") or getattr(spec, "canonical_relation", ""))
            if rel_id == target:
                return str(getattr(spec, "relation_id", "") or name)
    return None


def _find_quote(texts: list[str], quote: str) -> tuple[int, int, int] | None:
    quote = str(quote or "").strip()
    if not quote:
        return None
    for index, text in enumerate(texts):
        position = text.find(quote)
        if position >= 0:
            return index, position, position + len(quote)
        compact_quote = norm(quote)
        if compact_quote:
            compact_text = norm(text)
            compact_position = compact_text.find(compact_quote)
            if compact_position >= 0:
                return index, 0, len(text)
    return None


def _spec_quote(entity: dict[str, Any]) -> str:
    properties = entity.get("properties") or {}
    for key in ("内容描述", "quote", "evidence_quote"):
        if str(properties.get(key) or "").strip():
            return str(properties[key])
    return str(entity.get("name") or "")


def build_standard_skeleton(
    texts: Iterable[str],
    *,
    llm: Any | None = None,
) -> dict[str, Any] | None:
    """Build the canonical structural graph + prompt hint for one standard doc."""
    text_list = [str(item or "") for item in texts]
    joined = "\n\n".join(item for item in text_list if item.strip())
    if not joined.strip():
        return None
    canonical = align_standard_graph({"entities": [], "relations": []}, text_list, llm=llm)
    entities = [
        entity for entity in canonical.get("entities", [])
        if entity.get("type") in SKELETON_ENTITY_TYPES
    ]
    keys = {(entity["type"], norm(entity["name"])) for entity in entities}
    relations = [
        relation for relation in canonical.get("relations", [])
        if relation.get("rel_type") in SKELETON_RELATION_TYPES
        and (relation.get("from_type"), norm(relation.get("from_name"))) in keys
        and (relation.get("to_type"), norm(relation.get("to_name"))) in keys
    ]
    standard = next((entity for entity in entities if entity.get("type") == "Standard"), None)
    standard_name = str((standard or {}).get("name") or "")

    sections = [
        entity["name"] for entity in entities
        if entity.get("type") == "Section"
    ]
    clauses = [
        entity["name"] for entity in entities
        if entity.get("type") == "Clause"
    ]
    inventory = "；".join(sections[:16])
    clause_inventory = "；".join(clauses[:80])
    hint_lines = [_HINT_RULES]
    if standard_name:
        hint_lines.append(f"主标准：{standard_name}")
    if inventory:
        hint_lines.append(f"章节：{inventory}")
    if clause_inventory:
        hint_lines.append(f"条款：{clause_inventory}")
    return {
        "entities": entities,
        "relations": relations,
        "prompt_hint": "\n".join(hint_lines),
        "standard_name": standard_name,
        "texts": text_list,
    }


def add_standard_skeleton(
    pool: Any,
    manager: Any,
    plan: Any,
    skeleton: dict[str, Any],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Add skeleton entities/relations into the candidate pool (early)."""
    chunks = list(manager)
    texts = [str(getattr(chunk, "text", "") or "") for chunk in chunks]
    chunk_ids = [str(getattr(chunk, "chunk_id", "")) for chunk in chunks]
    entity_ids: dict[tuple[str, str], str] = {}
    added = 0
    try:
        existing_standard = any(
            canonical_name(getattr(item, "canonical_type", "") or getattr(item, "proposed_type", "")) == canonical_name("Standard")
            for item in pool.accepted_entities()
        )
    except Exception:
        existing_standard = False
    for entity in skeleton.get("entities") or []:
        if existing_standard and canonical_name(str(entity.get("type") or "")) == canonical_name("Standard"):
            continue
        type_id = _resolve_type(plan, str(entity.get("type") or ""))
        if not type_id:
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        quote = _spec_quote(entity)
        located = _find_quote(texts, quote)
        if located is None:
            located = _find_quote(texts, name)
        if located is None:
            continue
        chunk_index, start, end = located
        evidence = Evidence(
            span_id=f"{chunk_ids[chunk_index]}:{start}:{end}",
            source_block_ids=[chunk_ids[chunk_index]],
            quote=texts[chunk_index][start:end] if end > start else quote,
            start_char=start,
            end_char=end,
            evidence_type="structural",
        )
        candidate = EntityCandidate(
            candidate_id="",
            proposed_type=type_id,
            canonical_type=type_id,
            name=name,
            properties=dict(entity.get("properties") or {}),
            evidence=[evidence],
            source="standard_skeleton",
        )
        before = pool.entity_candidate_count
        result = pool.add_entity(candidate, warnings=warnings)
        if result is not None and pool.entity_candidate_count > before:
            added += 1
        candidate_id = str(getattr(result, "candidate_id", "") or "")
        if candidate_id:
            entity_ids[(str(entity.get("type")), norm(name))] = candidate_id
    relation_added = 0
    for relation in skeleton.get("relations") or []:
        relation_id = _resolve_relation(plan, str(relation.get("rel_type") or ""))
        if not relation_id:
            continue
        from_id = entity_ids.get((str(relation.get("from_type")), norm(relation.get("from_name"))))
        to_id = entity_ids.get((str(relation.get("to_type")), norm(relation.get("to_name"))))
        if not from_id or not to_id:
            continue
        target = next(
            (item for item in (skeleton.get("entities") or [])
             if item.get("type") == relation.get("to_type") and norm(item.get("name")) == norm(relation.get("to_name"))),
            None,
        )
        quote = _spec_quote(target or {}) or str(relation.get("to_name") or "")
        located = _find_quote(texts, quote)
        if located is None:
            continue
        chunk_index, start, end = located
        evidence = Evidence(
            span_id=f"{chunk_ids[chunk_index]}:{start}:{end}",
            source_block_ids=[chunk_ids[chunk_index]],
            quote=texts[chunk_index][start:end] if end > start else quote,
            start_char=start,
            end_char=end,
            evidence_type="structural",
        )
        pool.add_relation(
            RelationCandidate(
                candidate_id="",
                from_candidate_id=from_id,
                to_candidate_id=to_id,
                proposed_relation=relation_id,
                canonical_relation=None,
                evidence=[evidence],
            ),
            warnings=warnings,
        )
        relation_added += 1
    return {"entities": added, "relations": relation_added}


__all__ = ["build_standard_skeleton", "add_standard_skeleton"]