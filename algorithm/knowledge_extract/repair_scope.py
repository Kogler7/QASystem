"""Align relation repair tasks with the actual endpoint catalog and contract."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RelationRepairScope:
    issues: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    schema: dict[str, Any]
    signatures: str


def relation_repair_scope(plan, catalog, issues, schema) -> RelationRepairScope:
    """Never invent endpoints or widen enums to satisfy impossible verifier issues.

    Caller owns catalog budgeting. Renderers must display this same catalog without
    a second hidden truncation. Skipped issues remain unresolved in the verifier.
    """
    output = deepcopy(schema)
    item = output['properties']['relations']['items']['properties']
    existing = set(item['rel_type'].get('enum', []))
    types = {str(e.get('type') or '') for e in catalog or []}
    ids = sorted({str(e['id']) for e in catalog or [] if e.get('id')})
    specs = [s for group in plan.relation_specs.values() for s in group
             if s.source in types and s.target in types
             and s.source in plan.allowed_types and s.target in plan.allowed_types
             and s.relation_id in existing]
    allowed = {s.relation_id for s in specs}
    executable, skipped = [], []
    for issue in issues or []:
        context = issue.get('context') or {}
        expected = set(context.get('expected_relations') or [])
        missing = expected - allowed
        target = context.get('target_type')
        if not ids or not allowed or missing or (target and target not in types):
            skipped.append({'issue': deepcopy(issue), 'reason': 'repair endpoint/signature unavailable',
                            'unavailable_relations': sorted(missing),
                            'unavailable_target_type': target if target and target not in types else None})
        else:
            executable.append(deepcopy(issue))
    # Keep the same catalog-derived contract across issue batching. Narrowing
    # per batch inside the renderer would disagree with the caller's validator.
    item['rel_type']['enum'] = sorted(allowed)
    for field in ('from_id', 'to_id'):
        item[field]['enum'] = ids
    lines = set()
    for spec in specs:
        line = f'`{spec.source}` -[`{spec.relation_id}`]-> `{spec.target}`'
        comment = ' '.join((getattr(spec, 'comment', '') or '').split())
        if comment:
            line += f' // {comment[:500]}'
        lines.add(line)
    return RelationRepairScope(executable, skipped, output,
                               '\n'.join(sorted(lines)) if lines else '（无可执行关系签名）')
