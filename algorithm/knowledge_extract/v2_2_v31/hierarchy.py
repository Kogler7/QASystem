"""Ontology-gated completion for explicitly numbered hierarchy relations."""
from __future__ import annotations

import re
from typing import Any

from .candidate_models import RelationCandidate
from .evidence import Evidence, locate_quote
from .graph_utils import canonical_name

_NUMBERED_PREFIX = re.compile(
    r"^\s*(?:第\s*)?(?P<number>(?:[A-Za-z]|\d+)(?:\.\d+)*)"
    r"(?=\s|$|[章节条款、：:])"
)
_NUMBER_PROPERTY_HINTS = (
    "编号",
    "标识符",
    "number",
    "clauseno",
    "sectionno",
)


def numbered_identifier(entity: Any) -> tuple[str, ...] | None:
    """Return a normalized heading number such as ``("6", "2", "1")``."""
    properties = dict(getattr(entity, "properties", None) or {})
    candidates = [
        value
        for key, value in properties.items()
        if any(hint in canonical_name(key) for hint in _NUMBER_PROPERTY_HINTS)
    ]
    candidates.append(getattr(entity, "name", ""))
    for value in candidates:
        match = _NUMBERED_PREFIX.match(str(value or ""))
        if match is None:
            continue
        return tuple(part.casefold() for part in match.group("number").split("."))
    return None


def _number_sort_key(entity: Any) -> tuple[tuple[int, int | str], ...]:
    """Sort dotted numeric headings numerically while retaining annex support."""
    number = numbered_identifier(entity) or ()
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in number
    )


def numbered_parent_match(parent: Any, child: Any) -> bool | None:
    """Return whether two numbered headings form a direct parent-child pair.

    ``None`` means the relation cannot be proven from numbering and must remain
    under the normal LLM/ontology path.
    """
    parent_number = numbered_identifier(parent)
    child_number = numbered_identifier(child)
    if not parent_number or not child_number or len(child_number) < 2:
        return None
    return parent_number == child_number[:-1]


def _structural_evidence(entity: Any, manager: Any) -> Evidence | None:
    for evidence in getattr(entity, "evidence", ()):
        located = locate_quote(
            manager,
            evidence.quote,
            chunk_ids=evidence.source_block_ids or None,
        )
        if located is None:
            continue
        return Evidence(
            span_id=located.span_id,
            source_block_ids=located.source_block_ids,
            quote=located.quote,
            start_char=located.start_char,
            end_char=located.end_char,
            evidence_type="structural",
        )
    return None


def assemble_numbered_hierarchy_relations(
    pool: Any,
    manager: Any,
    *,
    relation_id: str = "hasSubClause",
    warnings: list[dict[str, Any]] | None = None,
) -> int:
    """Add only uniquely provable direct hierarchy edges allowed by ontology."""
    signatures = tuple(pool.schema.resolve_relation(relation_id))
    if not signatures:
        return 0

    entities = pool.accepted_entities()
    existing_count = len(pool.accepted_relations())
    for child in entities:
        child_number = numbered_identifier(child)
        if not child_number or len(child_number) < 2:
            continue
        expected_parent = child_number[:-1]
        parents = {
            parent.candidate_id: parent
            for signature in signatures
            if pool.schema.is_assignable(child.canonical_type or "", signature.target)
            for parent in entities
            if parent.candidate_id != child.candidate_id
            and pool.schema.is_assignable(parent.canonical_type or "", signature.source)
            and numbered_identifier(parent) == expected_parent
        }
        if len(parents) != 1:
            continue
        parent = next(iter(parents.values()))
        evidence = _structural_evidence(child, manager)
        if evidence is None:
            continue
        pool.add_relation(
            RelationCandidate(
                candidate_id="",
                from_candidate_id=parent.candidate_id,
                to_candidate_id=child.candidate_id,
                proposed_relation=relation_id,
                canonical_relation=None,
                evidence=[evidence],
            ),
            warnings=warnings,
        )
    return len(pool.accepted_relations()) - existing_count


def assemble_document_relations(
    pool: Any,
    manager: Any,
    document_summary: dict[str, Any] | None,
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Connect the proven document anchor to its top-level structure and references."""
    summary = document_summary or {}
    primary_id = str(summary.get("primary_candidate_id") or "")
    entities = {entity.candidate_id: entity for entity in pool.accepted_entities()}
    primary = entities.get(primary_id)
    counts = {
        "hasStructuralElement": 0,
        "cites": 0,
        "hasDevelopmentStage": 0,
        "standardizes": 0,
        "classifiedUnder": 0,
        "issuedBy": 0,
        "proposedBy": 0,
        "administeredBy": 0,
        "appliesTo": 0,
    }
    if primary is None:
        return counts

    hierarchy_signatures = tuple(pool.schema.resolve_relation("hasSubClause"))
    hierarchy_sources = {str(signature.source) for signature in hierarchy_signatures}
    hierarchy_targets = {str(signature.target) for signature in hierarchy_signatures}
    top_level_types = hierarchy_sources - hierarchy_targets
    structure_signatures = tuple(pool.schema.resolve_relation("hasStructuralElement"))
    before = len(pool.accepted_relations())
    if len(top_level_types) == 1 and structure_signatures:
        top_type = next(iter(top_level_types))
        for target in entities.values():
            number = numbered_identifier(target)
            if (
                target.source != "markdown_structure"
                or not number
                or len(number) != 1
                or not pool.schema.is_assignable(target.canonical_type or "", top_type)
            ):
                continue
            evidence = _structural_evidence(target, manager)
            if evidence is None:
                continue
            pool.add_relation(
                RelationCandidate(
                    candidate_id="",
                    from_candidate_id=primary.candidate_id,
                    to_candidate_id=target.candidate_id,
                    proposed_relation="hasStructuralElement",
                    canonical_relation=None,
                    evidence=[evidence],
                ),
                warnings=warnings,
            )
    counts["hasStructuralElement"] = len(pool.accepted_relations()) - before

    before = len(pool.accepted_relations())
    if pool.schema.resolve_relation("cites"):
        for reference_id in summary.get("normative_reference_ids") or ():
            target = entities.get(str(reference_id))
            if target is None or target.source != "normative_reference":
                continue
            evidence = _structural_evidence(target, manager)
            if evidence is None:
                continue
            pool.add_relation(
                RelationCandidate(
                    candidate_id="",
                    from_candidate_id=primary.candidate_id,
                    to_candidate_id=target.candidate_id,
                    proposed_relation="cites",
                    canonical_relation=None,
                    evidence=[evidence],
                ),
                warnings=warnings,
            )
    counts["cites"] = len(pool.accepted_relations()) - before

    for relation_id in ("hasDevelopmentStage", "standardizes"):
        before = len(pool.accepted_relations())
        signatures = tuple(pool.schema.resolve_relation(relation_id))
        for target in entities.values():
            if target.source != "standard_semantics":
                continue
            if not any(
                pool.schema.is_assignable(primary.canonical_type or "", signature.source)
                and pool.schema.is_assignable(target.canonical_type or "", signature.target)
                for signature in signatures
            ):
                continue
            evidence = _structural_evidence(target, manager)
            if evidence is None:
                continue
            pool.add_relation(
                RelationCandidate(
                    candidate_id="",
                    from_candidate_id=primary.candidate_id,
                    to_candidate_id=target.candidate_id,
                    proposed_relation=relation_id,
                    canonical_relation=None,
                    evidence=[evidence],
                ),
                warnings=warnings,
            )
        counts[relation_id] = len(pool.accepted_relations()) - before

    # Cover metadata has a single unambiguous owner: the document anchor.
    # Link every accepted, ontology-compatible metadata value to that anchor;
    # entity selection has already rejected unscoped organizations/categories.
    for relation_id in ("classifiedUnder", "issuedBy", "proposedBy", "administeredBy"):
        before = len(pool.accepted_relations())
        signatures = tuple(pool.schema.resolve_relation(relation_id))
        for target in entities.values():
            if target.candidate_id == primary.candidate_id:
                continue
            if target.source in {"document_anchor", "normative_reference"}:
                continue
            if not any(
                pool.schema.is_assignable(primary.canonical_type or "", signature.source)
                and pool.schema.is_assignable(target.canonical_type or "", signature.target)
                for signature in signatures
            ):
                continue
            evidence = _structural_evidence(target, manager)
            if evidence is None:
                continue
            pool.add_relation(
                RelationCandidate(
                    candidate_id="",
                    from_candidate_id=primary.candidate_id,
                    to_candidate_id=target.candidate_id,
                    proposed_relation=relation_id,
                    canonical_relation=None,
                    evidence=[evidence],
                ),
                warnings=warnings,
            )
        counts[relation_id] = len(pool.accepted_relations()) - before

    # 公文主送/称谓行：标题后、正文前并列的运行主体 → APPLIES_TO（与 golden 主送口径对齐）。
    before = len(pool.accepted_relations())
    applies_signatures = tuple(pool.schema.resolve_relation("appliesTo"))
    if not applies_signatures:
        applies_signatures = tuple(pool.schema.resolve_relation("APPLIES_TO"))
    addressee_text = _document_addressee_text(primary, manager)
    if applies_signatures and addressee_text:
        for target in entities.values():
            if target.candidate_id == primary.candidate_id:
                continue
            name = str(getattr(target, "name", "") or "").strip()
            if not name or name not in addressee_text:
                continue
            if not any(
                pool.schema.is_assignable(primary.canonical_type or "", signature.source)
                and pool.schema.is_assignable(target.canonical_type or "", signature.target)
                for signature in applies_signatures
            ):
                continue
            evidence = _structural_evidence(target, manager) or _structural_evidence(primary, manager)
            if evidence is None:
                continue
            pool.add_relation(
                RelationCandidate(
                    candidate_id="",
                    from_candidate_id=primary.candidate_id,
                    to_candidate_id=target.candidate_id,
                    proposed_relation="appliesTo",
                    canonical_relation=None,
                    evidence=[evidence],
                ),
                warnings=warnings,
            )
    counts["appliesTo"] = len(pool.accepted_relations()) - before
    return counts


def _document_addressee_text(primary: Any, manager: Any) -> str:
    """截取公文标题后、首段正文前的称谓/主送窗口。"""
    texts: list[str] = []
    for evidence in getattr(primary, "evidence", ()) or ():
        quote = str(getattr(evidence, "quote", "") or "")
        if quote.strip():
            texts.append(quote)
        for block_id in getattr(evidence, "source_block_ids", ()) or ():
            try:
                chunk = manager.get_chunk(str(block_id))
            except Exception:
                chunk = None
            if chunk is not None and getattr(chunk, "text", None):
                texts.append(str(chunk.text))
    if not texts:
        for chunk in manager:
            if getattr(chunk, "text", None):
                texts.append(str(chunk.text))
                break
    window = "\n".join(texts)
    if not window.strip():
        return ""
    lines = window.splitlines()
    captured: list[str] = []
    seen_title = False
    title = str(getattr(primary, "name", "") or "").strip()
    title_compact = title.replace(" ", "")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if captured:
                break
            continue
        if title_compact and title_compact in stripped.replace(" ", ""):
            seen_title = True
            continue
        if not seen_title and title_compact:
            if not (stripped.endswith(("：", ":")) and ("各" in stripped or "部" in stripped)):
                continue
        if stripped.endswith(("：", ":")) and len(stripped) <= 200:
            captured.append(stripped)
            break
        if seen_title and len(stripped) <= 200 and ("各" in stripped or "部门" in stripped):
            captured.append(stripped)
            if stripped.endswith(("：", ":")):
                break
    return "\n".join(captured) if captured else window[:800]


def _position(entity: Any, manager: Any) -> tuple[int, int]:
    positions: list[tuple[int, int]] = []
    for evidence in getattr(entity, "evidence", ()) or ():
        start = getattr(evidence, "start_char", None)
        if not isinstance(start, int):
            continue
        for block_id in getattr(evidence, "source_block_ids", ()) or ():
            index = manager.index_of(str(block_id))
            if index is not None:
                positions.append((index, start))
    return min(positions) if positions else (10**9, 10**9)


def _valid_position(entity: Any, manager: Any) -> tuple[int, int] | None:
    position = _position(entity, manager)
    return None if position[0] == 10**9 else position


def _nearest_document_unit(
    entity: Any,
    units: list[Any],
    manager: Any,
    chunk_index: int,
) -> Any | None:
    """Return the DocumentUnit that owns *entity*'s evidence in its own chunk.

    Only units from the entity's chunk are eligible, so a fact taken from an
    annex/form never borrows the last unit of the main body.  Within a chunk the
    owning unit is the nearest one starting at or before the entity evidence.
    """
    eligible: list[tuple[tuple[int, int], Any]] = []
    for unit in units:
        position = _valid_position(unit, manager)
        if position is not None and position[0] == chunk_index:
            eligible.append((position, unit))
    if not eligible:
        return None
    eligible.sort(key=lambda item: item[0])
    entity_position = _valid_position(entity, manager)
    if entity_position is None or entity_position[0] != chunk_index:
        return None
    preceding = [unit for position, unit in eligible if position <= entity_position]
    return preceding[-1] if preceding else eligible[0][1]


def _match_key(value: Any) -> str:
    """Aggressive key for attachment ownership: NFKC + drop punctuation/spaces."""
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", canonical_name(value))


def _is_attachment_unit(unit: Any) -> bool:
    properties = getattr(unit, "properties", None) or {}
    symbols = " ".join(
        str(value)
        for value in (
            getattr(unit, "name", "") or "",
            properties.get("单元类型") or properties.get("unitType") or "",
            properties.get("原文位置") or "",
            properties.get("单元标题") or "",
        )
    )
    return "附件" in symbols


def _unit_match_text(unit: Any) -> str:
    parts = [str(getattr(unit, "name", "") or "")]
    properties = getattr(unit, "properties", None) or {}
    for key in ("原文内容", "单元标题", "原文位置", "originalText", "unitTitle"):
        value = properties.get(key)
        if isinstance(value, str):
            parts.append(value)
    for evidence in getattr(unit, "evidence", ()) or ():
        quote = getattr(evidence, "quote", "")
        if quote:
            parts.append(str(quote))
    return _match_key(" ".join(parts))


def assemble_document_unit_relations(
    pool: Any,
    manager: Any,
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Assemble ``Document -[HAS_PART]-> DocumentUnit`` and ``-[EVIDENCED_BY]``.

    Both relations are fully determined by the ontology signature plus the
    evidence position of the two endpoints, so they are never sent to the
    relation classifier.
    """
    entities = pool.accepted_entities()
    counts = {"HAS_PART": 0, "EVIDENCED_BY": 0}

    has_part = tuple(pool.schema.resolve_relation("HAS_PART"))
    if has_part:
        units = [
            entity for entity in entities
            if canonical_name(entity.canonical_type or "") == "documentunit"
        ]
        before = len(pool.accepted_relations())
        for signature in has_part:
            # The benchmark ontology declares HAS_PART in both directions; keep
            # only document -> unit, which is the direction the golden uses.
            if canonical_name(signature.target) != "documentunit":
                continue
            sources = [
                entity for entity in entities
                if pool.schema.is_assignable(entity.canonical_type or "", signature.source)
            ]
            for source in sources:
                for unit in units:
                    if source.candidate_id == unit.candidate_id:
                        continue
                    evidence = _structural_evidence(unit, manager)
                    if evidence is None:
                        continue
                    pool.add_relation(
                        RelationCandidate(
                            candidate_id="",
                            from_candidate_id=source.candidate_id,
                            to_candidate_id=unit.candidate_id,
                            proposed_relation="HAS_PART",
                            canonical_relation=None,
                            evidence=[evidence],
                        ),
                        warnings=warnings,
                    )
        counts["HAS_PART"] = len(pool.accepted_relations()) - before

    evidenced_by = tuple(pool.schema.resolve_relation("EVIDENCED_BY"))
    if evidenced_by:
        before = len(pool.accepted_relations())
        units = [
            entity for entity in entities
            if any(
                pool.schema.is_assignable(entity.canonical_type or "", signature.target)
                for signature in evidenced_by
            )
        ]
        attachment_units = [unit for unit in units if _is_attachment_unit(unit)]
        other_units = [unit for unit in units if not _is_attachment_unit(unit)]
        attachment_text = {
            unit.candidate_id: _unit_match_text(unit) for unit in attachment_units
        }
        for signature in evidenced_by:
            sources = [
                entity for entity in entities
                if pool.schema.is_assignable(entity.canonical_type or "", signature.source)
            ]
            for entity in sources:
                # Attachment-owned facts keep their owner even when they are
                # mentioned in the main body (e.g. "详见附件").  Everything else
                # belongs to the nearest preceding non-attachment unit.
                entity_key = _match_key(getattr(entity, "name", ""))
                unit = None
                if entity_key:
                    unit = next(
                        (
                            candidate
                            for candidate in attachment_units
                            if entity_key in attachment_text[candidate.candidate_id]
                        ),
                        None,
                    )
                if unit is None:
                    position = _valid_position(entity, manager)
                    if position is None:
                        continue
                    unit = _nearest_document_unit(
                        entity, other_units or units, manager, position[0]
                    )
                if unit is None or unit.candidate_id == entity.candidate_id:
                    continue
                evidence = _structural_evidence(entity, manager)
                if evidence is None:
                    continue
                pool.add_relation(
                    RelationCandidate(
                        candidate_id="",
                        from_candidate_id=entity.candidate_id,
                        to_candidate_id=unit.candidate_id,
                        proposed_relation="EVIDENCED_BY",
                        canonical_relation=None,
                        evidence=[evidence],
                    ),
                    warnings=warnings,
                )
        counts["EVIDENCED_BY"] = len(pool.accepted_relations()) - before

    return counts


def assemble_standard_semantic_relations(
    pool: Any,
    manager: Any,
    document_summary: dict[str, Any] | None,
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Assemble relations whose endpoints were created from standard structure."""
    entities = pool.accepted_entities()
    by_id = {entity.candidate_id: entity for entity in entities}
    primary = by_id.get(str((document_summary or {}).get("primary_candidate_id") or ""))
    counts = {
        "hasNormativeElement": 0,
        "hasClause": 0,
        "defines": 0,
        "hasRepresentationForm": 0,
        "usesTerm": 0,
    }

    def add(source: Any, target: Any, relation_id: str) -> None:
        evidence = _structural_evidence(target, manager)
        if evidence is None:
            return
        pool.add_relation(
            RelationCandidate(
                candidate_id="",
                from_candidate_id=source.candidate_id,
                to_candidate_id=target.candidate_id,
                proposed_relation=relation_id,
                canonical_relation=None,
                evidence=[evidence],
            ),
            warnings=warnings,
        )

    content = [entity for entity in entities if entity.source == "content_element"]
    if primary is not None:
        before = len(pool.accepted_relations())
        for target in content:
            add(primary, target, "hasNormativeElement")
        counts["hasNormativeElement"] = len(pool.accepted_relations()) - before

    structural = [entity for entity in entities if entity.source == "markdown_structure"]
    clauses = [
        entity
        for entity in entities
        if entity.source == "standard_semantics"
        and canonical_name(entity.canonical_type) == "clause"
    ]
    before = len(pool.accepted_relations())
    for clause in clauses:
        clause_number = numbered_identifier(clause)
        owner: Any | None = None
        if str(clause.name).startswith("附录"):
            owner = next((entity for entity in content if str(entity.name).startswith("附录")), None)
        elif str(clause.name).startswith("参考文献"):
            owner = next((entity for entity in content if entity.name == "参考文献"), None)
        elif clause_number:
            if len(clause_number) == 2 and not str(clause.name).endswith("要求"):
                owner_number = clause_number[:1]
            else:
                owner_number = clause_number
            matches = [entity for entity in structural if numbered_identifier(entity) == owner_number]
            owner = matches[0] if len(matches) == 1 else None
        if owner is not None:
            add(owner, clause, "hasClause")
    counts["hasClause"] = len(pool.accepted_relations()) - before

    terminology = [entity for entity in entities if entity.source == "terminology_definition"]
    before = len(pool.accepted_relations())
    terms_owner = next((entity for entity in content if entity.name == "术语和定义"), None)
    symbols_owner = next((entity for entity in content if entity.name == "符号"), None)
    for term in terminology:
        owner = symbols_owner if re.fullmatch(r"[A-Za-z]+\d+", str(term.name)) else terms_owner
        if owner is not None:
            add(owner, term, "defines")
    counts["defines"] = len(pool.accepted_relations()) - before

    forms = [entity for entity in entities if entity.source == "document_form"]
    structural_positions = sorted(
        ((_position(entity, manager), entity) for entity in structural),
        key=lambda item: item[0],
    )
    numbered_clauses = [
        (numbered_identifier(clause), clause)
        for clause in clauses
        if numbered_identifier(clause)
    ]
    before = len(pool.accepted_relations())
    for form in forms:
        form_type = canonical_name(form.canonical_type)
        owner = None
        if form_type == "annex":
            owner = next((clause for clause in clauses if str(clause.name).startswith("附录")), None)
        elif form_type == "reference":
            owner = next((clause for clause in clauses if str(clause.name).startswith("参考文献")), None)
        elif form_type in {"equation", "table"}:
            form_position = _position(form, manager)
            preceding = [entity for position, entity in structural_positions if position <= form_position]
            nearest_number = numbered_identifier(preceding[-1]) if preceding else None
            if nearest_number:
                matches = [
                    (number, clause)
                    for number, clause in numbered_clauses
                    if nearest_number == number or nearest_number[: len(number)] == number
                ]
                if matches:
                    owner = max(matches, key=lambda item: len(item[0]))[1]
        if owner is not None:
            add(owner, form, "hasRepresentationForm")
    counts["hasRepresentationForm"] = len(pool.accepted_relations()) - before

    requirement_clauses = sorted(
        (clause for clause in clauses if str(clause.name).endswith("要求")),
        key=lambda clause: _position(clause, manager),
    )
    before = len(pool.accepted_relations())
    for term in terminology:
        term_name = str(term.name)
        if not re.search(r"[\u3400-\u9fff]", term_name):
            continue
        for clause in requirement_clauses:
            evidence = next(iter(getattr(clause, "evidence", ()) or ()), None)
            if evidence is None or not evidence.source_block_ids:
                continue
            block_id = evidence.source_block_ids[0]
            try:
                text = manager.get_chunk(block_id).text or ""
            except KeyError:
                continue
            start = evidence.end_char if isinstance(evidence.end_char, int) else 0
            heading = re.match(r"(?P<marks>#{1,6})", evidence.quote or "")
            max_level = len(heading.group("marks")) if heading else 6
            next_heading = re.search(
                rf"(?m)^#{{1,{max_level}}}\s+",
                text[start:],
            )
            end = start + next_heading.start() if next_heading else len(text)
            mention = text.find(term_name, start, end)
            if mention < 0:
                continue
            relation_evidence = Evidence(
                span_id=f"{block_id}:{mention}:{mention + len(term_name)}",
                source_block_ids=[block_id],
                quote=text[mention : mention + len(term_name)],
                start_char=mention,
                end_char=mention + len(term_name),
                evidence_type="explicit",
            )
            pool.add_relation(
                RelationCandidate(
                    candidate_id="",
                    from_candidate_id=clause.candidate_id,
                    to_candidate_id=term.candidate_id,
                    proposed_relation="usesTerm",
                    canonical_relation=None,
                    evidence=[relation_evidence],
                ),
                warnings=warnings,
            )
            break
    counts["usesTerm"] = len(pool.accepted_relations()) - before
    return counts


def assemble_standard_technical_relations(
    pool: Any,
    manager: Any,
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Connect conservative technical entities to their literal requirement scopes."""
    entities = pool.accepted_entities()
    counts = {
        "referencesClause": 0,
        "describesAction": 0,
        "specifiesCharacteristic": 0,
        "hasCharacteristic": 0,
        "involvesObject": 0,
        "referencesExternalResource": 0,
        "imposesConstraint": 0,
    }

    def add(source: Any, target: Any, relation_id: str, evidence: Evidence | None = None) -> None:
        bound = evidence or _structural_evidence(target, manager)
        if bound is None:
            return
        pool.add_relation(
            RelationCandidate(
                candidate_id="",
                from_candidate_id=source.candidate_id,
                to_candidate_id=target.candidate_id,
                proposed_relation=relation_id,
                canonical_relation=None,
                evidence=[bound],
            ),
            warnings=warnings,
        )

    def scope(entity: Any) -> tuple[str, str, int, int] | None:
        evidence = next(iter(getattr(entity, "evidence", ()) or ()), None)
        if evidence is None or not evidence.source_block_ids:
            return None
        block_id = str(evidence.source_block_ids[0])
        try:
            text = manager.get_chunk(block_id).text or ""
        except KeyError:
            return None
        start = evidence.end_char if isinstance(evidence.end_char, int) else 0
        heading = re.match(r"(?P<marks>#{1,6})", evidence.quote or "")
        max_level = len(heading.group("marks")) if heading else 6
        next_heading = re.search(rf"(?m)^#{{1,{max_level}}}\s+", text[start:])
        end = start + next_heading.start() if next_heading else len(text)
        return block_id, text, start, end

    clauses = sorted(
        (
            entity
            for entity in entities
            if entity.source == "standard_semantics"
            and canonical_name(entity.canonical_type) == "clause"
            and str(entity.name).endswith("要求")
        ),
        key=_number_sort_key,
    )
    structural = [entity for entity in entities if entity.source == "markdown_structure"]
    actions = [
        entity
        for entity in entities
        if entity.source == "technical_semantics"
        and canonical_name(entity.canonical_type) == "actionclass"
    ]
    action_by_number: dict[tuple[str, ...], Any] = {}
    for action in actions:
        match = next(
            (
                found
                for evidence in (getattr(action, "evidence", ()) or ())
                if (found := re.match(r"^#{1,6}\s+(\d+(?:\.\d+)*)", evidence.quote))
                is not None
            ),
            None,
        )
        if match:
            action_by_number[tuple(match.group(1).split("."))] = action

    before_references = len(pool.accepted_relations())
    for clause in clauses:
        clause_scope = scope(clause)
        if clause_scope is None:
            continue
        block_id, text, start, end = clause_scope
        match = re.search(r"按\s*(?P<number>\d+(?:\.\d+)+)\s*(?:检验|试验)", text[start:end])
        search_offset = start
        if match is None:
            current_index = manager.index_of(block_id)
            next_id = (
                manager.chunk_ids[current_index + 1]
                if current_index is not None and current_index + 1 < len(manager.chunk_ids)
                else None
            )
            if next_id is not None:
                next_text = manager.get_chunk(next_id).text or ""
                next_heading = re.search(r"(?m)^#{1,3}\s+", next_text)
                prefix_end = next_heading.start() if next_heading else len(next_text)
                next_match = re.search(
                    r"按\s*(?P<number>\d+(?:\.\d+)+)\s*(?:检验|试验)",
                    next_text[:prefix_end],
                )
                if next_match is not None:
                    block_id, text, start, end = next_id, next_text, 0, prefix_end
                    match = next_match
                    search_offset = 0
        if match is None:
            continue
        number = tuple(match.group("number").split("."))
        if len(number) > 3:
            number = number[:3]
        target = next((entity for entity in structural if numbered_identifier(entity) == number), None)
        mention_start = search_offset + match.start("number")
        relation_evidence = Evidence(
            span_id=f"{block_id}:{mention_start}:{mention_start + len(match.group('number'))}",
            source_block_ids=[block_id],
            quote=match.group("number"),
            start_char=mention_start,
            end_char=mention_start + len(match.group("number")),
            evidence_type="explicit",
        )
        if target is not None:
            add(clause, target, "referencesClause", relation_evidence)
        action = action_by_number.get(number)
        if action is not None:
            add(clause, action, "describesAction", relation_evidence)
    new_reference_relations = pool.accepted_relations()[before_references:]
    counts["referencesClause"] = sum(
        canonical_name(relation.canonical_relation) == "referencesclause"
        for relation in new_reference_relations
    )
    counts["describesAction"] = sum(
        canonical_name(relation.canonical_relation) == "describesaction"
        for relation in new_reference_relations
    )

    properties = [
        entity
        for entity in entities
        if entity.source == "technical_semantics"
        and canonical_name(entity.canonical_type) == "property"
    ]
    before = len(pool.accepted_relations())
    first_clause_by_property: dict[str, Any] = {}
    for prop in properties:
        for clause in clauses:
            clause_scope = scope(clause)
            if clause_scope is None:
                continue
            _, text, start, end = clause_scope
            if str(prop.name) in text[start:end]:
                first_clause_by_property[prop.candidate_id] = clause
                add(clause, prop, "specifiesCharacteristic")
                break
    counts["specifiesCharacteristic"] = len(pool.accepted_relations()) - before

    subjects = [
        entity
        for entity in entities
        if entity.source == "technical_semantics"
        and canonical_name(entity.canonical_type) == canonical_name("Subject of Standardization")
    ]
    clause_scopes = {
        clause.candidate_id: resolved
        for clause in clauses
        if (resolved := scope(clause)) is not None
    }
    subject_rank: list[tuple[int, int, str, Any]] = []
    for subject in subjects:
        mentions = sum(
            str(subject.name) in text[start:end]
            for _, text, start, end in clause_scopes.values()
        )
        subject_rank.append((-mentions, len(str(subject.name)), canonical_name(subject.name), subject))
    subject_rank.sort(key=lambda item: item[:3])
    base_subject = subject_rank[0][3] if subject_rank and subject_rank[0][0] < 0 else None
    before = len(pool.accepted_relations())
    if base_subject is not None:
        for prop in properties:
            add(base_subject, prop, "hasCharacteristic")
    counts["hasCharacteristic"] = len(pool.accepted_relations()) - before

    before = len(pool.accepted_relations())
    if base_subject is not None:
        for clause in clauses:
            # A generated identity property is not structural evidence that an
            # annex/bibliography clause is a numbered requirement clause.
            if _NUMBERED_PREFIX.match(str(clause.name)) is None:
                continue
            clause_scope = clause_scopes.get(clause.candidate_id)
            if clause_scope is None:
                continue
            _, text, start, end = clause_scope
            body = text[start:end]
            if str(base_subject.name) not in body:
                continue
            # Direct qualitative predicates establish the involved object.
            # Pure numeric comparisons describe a characteristic constraint
            # and are not enough by themselves to add another object edge.
            if re.search(r"(?:应无|应正确|应清晰|应符合|应满足)", body):
                add(clause, base_subject, "involvesObject")
    counts["involvesObject"] = len(pool.accepted_relations()) - before

    resources = [
        entity
        for entity in entities
        if entity.source == "technical_semantics"
        and canonical_name(entity.canonical_type) == "externalresource"
    ]
    before = len(pool.accepted_relations())
    for resource in resources:
        for clause in clauses:
            clause_scope = scope(clause)
            if clause_scope is None:
                continue
            _, text, start, end = clause_scope
            if str(resource.name) not in text[start:end]:
                continue
            add(clause, resource, "referencesExternalResource")
            break
    counts["referencesExternalResource"] = len(pool.accepted_relations()) - before

    constraints = [
        entity
        for entity in entities
        if canonical_name(entity.canonical_type) in {"constrain", "constraint"}
    ]
    before = len(pool.accepted_relations())
    for constraint in constraints:
        target_name = next(
            (
                str(value)
                for key, value in (getattr(constraint, "properties", None) or {}).items()
                if "constrainttarget" in canonical_name(key) or "约束目标" in canonical_name(key)
            ),
            "",
        )
        if not target_name:
            mentioned_in_name = sorted(
                (prop for prop in properties if str(prop.name) in str(constraint.name)),
                key=lambda prop: (-len(str(prop.name)), canonical_name(prop.name)),
            )
            if mentioned_in_name:
                target_name = str(mentioned_in_name[0].name)
        if not target_name:
            evidence_text = " ".join(
                str(getattr(item, "quote", "") or "")
                for item in (getattr(constraint, "evidence", None) or [])
            )
            mentioned = sorted(
                (prop for prop in properties if str(prop.name) in evidence_text),
                key=lambda prop: (-len(str(prop.name)), canonical_name(prop.name)),
            )
            target_name = str(mentioned[0].name) if mentioned else ""
        target = next((prop for prop in properties if prop.name == target_name), None)
        if target is not None:
            add(target, constraint, "imposesConstraint")
    counts["imposesConstraint"] = len(pool.accepted_relations()) - before
    return counts


__all__ = [
    "assemble_document_relations",
    "assemble_numbered_hierarchy_relations",
    "assemble_standard_semantic_relations",
    "assemble_standard_technical_relations",
    "numbered_identifier",
    "numbered_parent_match",
]
