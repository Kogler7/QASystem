"""Infer table extraction intent without equating JSON structure with rules.

The ontology bounds what may be expressed. Nonempty source records must also
support conditional/diagnostic semantics before rule-specific prompts apply.
Decisions are local to each table and record, never inherited from a filename.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .structured_text import extract_json_payload, is_structured_payload


def _symbol(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", unicodedata.normalize("NFKC", str(value)).casefold())


_CONDITIONS = frozenset({
    "antecedent", "precondition", "condition", "if", "前件", "前提", "前提条件",
    "条件", "触发条件", "适用条件", "判断条件",
})
_OUTCOMES = frozenset({
    "consequent", "conclusion", "outcome", "result", "then", "action",
    "后件", "结论", "结果", "动作", "处置", "处理方法", "处理措施",
})
_SYMPTOMS = frozenset({
    "symptom", "fault", "problem", "故障状况", "故障现象", "故障描述",
    "故障症状", "异常现象", "现象",
})
_CAUSES_ACTIONS = frozenset({
    "cause", "rootcause", "solution", "remedy", "resolution", "action",
    "故障原因", "原因", "诊断结论", "处理方法", "处理措施", "处置",
    "解决方法", "纠正措施", "修复方法",
})
_CATEGORIES = frozenset({"category", "type", "分类", "类别", "类型", "故障详细分类"})
_CONDITIONAL = re.compile(
    r"(?:如果|若|当[^。\n]{1,80}时|\bif\b).{1,180}(?:则|那么|应|需|必须|不得|\bthen\b)"
    r"|.{1,100}(?:导致|引起|造成|触发).{1,100}", re.IGNORECASE,
)
_DIRECTIVE = re.compile(r".{1,100}(?:必须|不得|不应|应当|严禁|shall\b|must\b).{1,100}", re.IGNORECASE)
_RECOVERY = re.compile(r"恢复正常|更换|重启|复位|停机|停车|报警|replace\b|restart\b", re.IGNORECASE)
_PREDICATE = re.compile(
    r"[<>≤≥]|超过|超出|不足|低于|高于|大于|小于|异常|失效|中断|断开|不能|无法|过高|过低|电压低|温度高|\b(?:above|below|fails?)\b",
    re.IGNORECASE,
)
_RULE_TEXT_FIELDS = frozenset({
    "rule", "rulebody", "ruletemplate", "constraint", "requirement",
    "规则", "规则正文", "规则内容", "规则模板", "要求", "约束", "约束条件",
})
_FORM_FIELDS = frozenset({"收文号", "来文字号", "来文单位", "收文日期", "拟办意见", "领导批示"})
_MEASUREMENT_FIELDS = frozenset({
    "参数名称", "参数值", "测量值", "实测值", "检测值", "检测项目", "测量项目", "测量对象",
    "parametername", "parametervalue", "measuredvalue", "measurement",
})


@dataclass(frozen=True)
class TableSemantics:
    mode: str = "facts"
    rule_types: tuple[str, ...] = ()
    # One-based positions in the JSON blocks and their source record arrays.
    rule_rows: tuple[tuple[int, int], ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "rule_types": list(self.rule_types),
            "rule_rows": [list(position) for position in self.rule_rows],
            "reasons": list(self.reasons),
        }


def rule_type_ids(plan: Any) -> tuple[str, ...]:
    """Recognize ontology roles, including semantic fields and inherited fields.

    RuleID alone is deliberately insufficient. A rule engine or a equipment
    category is not a rule merely because its name contains the word 'rule'.
    """
    found = []
    for type_id in sorted(plan.allowed_types):
        spec = plan.class_specs.get(type_id)
        if spec is None:
            continue
        labels = [_symbol(value) for value in (type_id, spec.display_name, *spec.stereotypes)]
        fields = {
            _symbol(value)
            for prop in spec.properties
            for value in (prop.identifier, prop.display_name)
        }
        named_rule = any(label.endswith(("rule", "ruletype", "规则")) for label in labels)
        # Generic condition/result fields also describe measurements. Only
        # explicit rule roles qualify an otherwise neutrally named class.
        conditional_fields = bool(
            fields & {"antecedent", "前件", "前提条件", "触发条件"}
            and fields & {"consequent", "后件", "结论", "动作", "action"}
        )
        if named_rule or conditional_fields or fields & {"ruletemplate", "规则模板"}:
            found.append(type_id)
    return tuple(found)


def _records(block: Any) -> list[dict[str, Any]]:
    if isinstance(block, list):
        return [row if isinstance(row, dict) else {} for row in block]
    if not isinstance(block, dict):
        return []
    if "rows" not in block:
        return [block]
    columns = block.get("columns") or []
    rows = block.get("rows")
    if not isinstance(rows, list):
        return []
    return [
        row if isinstance(row, dict) else dict(zip(columns, row)) if isinstance(row, list) else {}
        for row in rows
    ]


def _values(row: dict[str, Any]) -> dict[str, str]:
    return {
        _symbol(key): str(value).strip()
        for key, value in row.items()
        if value is not None and not isinstance(value, (dict, list)) and str(value).strip()
    }


def _rule_evidence(values: dict[str, str]) -> str | None:
    keys = set(values)
    if keys & _MEASUREMENT_FIELDS and not keys & (
        {"antecedent", "前件", "触发条件"} | _RULE_TEXT_FIELDS | _SYMPTOMS
    ):
        return None
    conditions = keys & _CONDITIONS
    outcomes = keys & _OUTCOMES
    # Generic condition/result headers occur in observational test records.
    # Require a stated predicate unless either side has an explicit rule role.
    explicit_pair = conditions - {"condition", "条件", "适用条件"} or outcomes - {"result", "结果"}
    if conditions and outcomes and (explicit_pair or any(_PREDICATE.search(values[key]) for key in conditions)):
        return "source.condition_and_outcome"
    symptoms = [values[key] for key in keys & _SYMPTOMS]
    if symptoms and keys & _CAUSES_ACTIONS:
        return "source.symptom_and_cause_or_action"
    if any(_CONDITIONAL.search(value) for value in values.values()):
        return "source.explicit_conditional_statement"
    if any(_DIRECTIVE.search(values[key]) for key in keys & _RULE_TEXT_FIELDS):
        return "source.explicit_constraint"
    # A diagnostic record may carry its resolution in the observation cell.
    # An alarm alone is only an observation, not a condition/result pair.
    if any(("恢复正常" in text or "后恢复" in text) and _RECOVERY.search(text) for text in symptoms):
        return "source.observation_with_resolution"
    return None


def analyze_table_semantics(plan: Any, chunk: Any) -> TableSemantics:
    payload = extract_json_payload(chunk.text or "", min_ratio=0.0)
    if payload is None:
        return TableSemantics(reasons=("source.not_table",))
    types = rule_type_ids(plan)
    if not types:
        return TableSemantics(reasons=("ontology.no_rule_semantics",))
    selected: list[tuple[int, int]] = []
    reasons: set[str] = set()
    total = 0
    ambiguous = False
    for table_index, block in enumerate(payload.blocks, start=1):
        rows = [_values(row) for row in _records(block)]
        total += len(rows)
        # Registry forms can contain conditional instructions in approval
        # cells. Those are form activities/constraints, not diagnostic rules.
        # Detect labels in either column names or cells (vertical PDF forms).
        labels = {
            re.sub(r"[:：;；*]", "", label)
            for row in rows
            for label in (*row.keys(), *(_symbol(value) for value in row.values()))
        }
        if len(labels & _FORM_FIELDS) >= 2 or labels & {"拟办意见", "领导批示"}:
            reasons.add("source.official_form")
            continue
        qualified: set[int] = set()
        supported_categories: set[tuple[str, str]] = set()
        for row_index, values in enumerate(rows, start=1):
            reason = _rule_evidence(values)
            if reason:
                qualified.add(row_index)
                reasons.add(reason)
                supported_categories.update((key, values[key]) for key in values.keys() & _CATEGORIES)
            elif values.keys() & (_CONDITIONS | _SYMPTOMS | _RULE_TEXT_FIELDS) and not values.keys() & _MEASUREMENT_FIELDS:
                ambiguous = True
        for row_index, values in enumerate(rows, start=1):
            # Include other observations of the same diagnostic class only
            # within this table, and only if they contain a condition/symptom.
            # A category by itself must never turn an inventory into rules.
            if values.keys() & (_CONDITIONS | _SYMPTOMS) and any(
                (key, values[key]) in supported_categories for key in values.keys() & _CATEGORIES
            ):
                qualified.add(row_index)
            if row_index in qualified:
                selected.append((table_index, row_index))
    if not selected:
        reason = "source.insufficient_rule_evidence" if ambiguous else "source.no_rule_evidence"
        return TableSemantics("uncertain" if ambiguous else "facts", types, reasons=tuple(sorted(reasons | {reason})))
    return TableSemantics(
        "rules" if len(selected) == total else "mixed", types, tuple(selected), tuple(sorted(reasons)),
    )


def table_entity_types(plan: Any, chunk: Any) -> frozenset[str]:
    """Do not offer rule types for a table lacking source rule evidence."""
    relevant = plan.relevant_types(chunk.text)
    decision = analyze_table_semantics(plan, chunk)
    if "source.not_table" in decision.reasons:
        return relevant
    prose = re.sub(r"(?ms)^\s*```(?:json)?\s*\n.*?^\s*```\s*$", "", chunk.text or "")
    if prose != (chunk.text or "") and _CONDITIONAL.search(prose):
        return frozenset(relevant | set(decision.rule_types))
    if not is_structured_payload(chunk.text or ""):
        # The table decision scopes JSON rows, not surrounding prose that may
        # independently support a rule. Keep the prose's ontology surface.
        return relevant
    if decision.rule_rows:
        return frozenset(relevant | set(decision.rule_types))
    return frozenset((relevant - set(decision.rule_types)) or (plan.allowed_types - set(decision.rule_types)))


__all__ = ["TableSemantics", "analyze_table_semantics", "rule_type_ids", "table_entity_types"]
