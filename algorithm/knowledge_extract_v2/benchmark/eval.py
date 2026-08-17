"""Phase 1 Step 1：基准集自检、指标计算与 extract_v2() 基线冻结。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：
- §37 Step 1：先冻结旧 extract_v2() baseline，基准集覆盖 标准/公文/术语/
  ontology class extraction/简单规则/复合规则；
- §35：统一抽取准确率口径未冻结前，同时报告 Entity P/R/F1、Property
  Exact/Normalized Accuracy、Relation P/R/F1、Composite Accuracy、
  Schema Validity、Evidence Coverage（Step 1 为 N/A）、Coverage Proxy
  以及失败归因。

命令：
  python benchmark/eval.py check
  python benchmark/eval.py run   --mode live|stub:gold|stub:empty|stub:error
                                [--module knowledge_extract_v2] [--out results/baseline.json]
  python benchmark/eval.py report [--run results/baseline.json]

说明：
- 本工具不依赖 openai 包；只有 --mode live 才加载真实 extractor。
- stub 模式仅用于验证评测链路（gold=理想输出、empty=零召回、error=解析失败路径），
  输出文件会明确标记 stub，不能作为正式基线。
- 正式基线：配置 KG_EXTRACT_LLM_BASE_URL / KG_EXTRACT_LLM_API_KEY /
  KG_EXTRACT_LLM_MODEL 后执行 --mode live。
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import importlib.util
import json
import os
import re
import sys
import types
from collections import Counter
from pathlib import Path
from typing import Any, Callable


BENCHMARK_ROOT = Path(__file__).resolve().parent
CASES_DIR = BENCHMARK_ROOT / "cases"
ONTOLOGIES_DIR = BENCHMARK_ROOT / "ontologies"
RESULTS_DIR = BENCHMARK_ROOT / "results"
RAW_DIR = RESULTS_DIR / "raw"

_WS = re.compile(r"\s+")

# 与本体验证器告警码一致的本体/schema 类错误
SCHEMA_WARNING_CODES = frozenset(
    {
        "UNKNOWN_ENTITY_TYPE",
        "UNKNOWN_PROPERTY",
        "UNKNOWN_RELATION_ENDPOINT_TYPE",
        "UNKNOWN_RELATION_TYPE",
        "RELATION_ENDPOINT_TYPE_MISMATCH",
        "ENTITY_OUT_OF_SCOPE",
        "UNKNOWN_TARGET_TYPE",
        "INVALID_ENTITIES",
        "INVALID_ENTITY",
        "INVALID_ENTITY_PROPERTIES",
        "INVALID_RELATIONS",
        "INVALID_RELATION",
        "RELATION_ENDPOINT_NAME_MISSING",
    }
)

COMPOSITE_CATEGORIES = {"composite_rule"}

UNIFIED_FORMULA = (
    "候选统一指标（未冻结）：0.3*entity_f1 + 0.2*property_normalized_accuracy "
    "+ 0.3*relation_f1；复合用例再计入 0.2*composite_accuracy；"
    "某分量不适用（如无 gold 关系）时按剩余权重重新归一化。"
)


# ─────────────────────────── 模块加载（不依赖 openai） ───────────────────────────


def load_package_modules(package_name: str, directory: Path, files: list[str]) -> dict[str, Any]:
    """通过文件加载目标包子模块，绕过包 __init__（避免 openai 依赖）。"""
    parent = types.ModuleType(package_name)
    parent.__path__ = [str(directory)]
    parent.__package__ = package_name
    sys.modules[package_name] = parent
    loaded: dict[str, Any] = {}
    for filename in files:
        path = directory / filename
        if not path.exists():
            continue
        module_name = f"{package_name}.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            loaded[path.stem] = module
        except Exception as exc:  # noqa: BLE001 - 记录加载失败供 live 模式提示
            loaded[path.stem] = exc
    return loaded


def _module_dir(module: str) -> Path:
    return BENCHMARK_ROOT.parents[1] / module


def _non_llm_modules(module: str) -> dict[str, Any]:
    return load_package_modules(
        module,
        _module_dir(module),
        ["graph_utils.py", "config.py", "scheduler.py", "ontology_schema.py"],
    )


# ─────────────────────────── 数据集加载与自检 ───────────────────────────


def load_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_dataset(cases_dir: Path = CASES_DIR) -> list[dict[str, Any]]:
    return [load_case(path) for path in sorted(cases_dir.glob("*.json"))]


def ontology_text(case: dict[str, Any]) -> str:
    return (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")


def collapse(value: Any) -> str:
    return _WS.sub("", str(value or "")).strip()


def check_dataset(cases: list[dict[str, Any]], modules: dict[str, Any]) -> list[str]:
    """校验基准集：本体可解析、gold schema 合法、端点存在、证据可定位。"""
    schema_mod = modules["ontology_schema"]
    graph_utils = modules["graph_utils"]
    canonical = graph_utils.canonical_name
    issues: list[str] = []

    for case in cases:
        case_id = case["id"]
        try:
            schema = schema_mod.OntologySchema.parse(ontology_text(case))
        except Exception as exc:  # noqa: BLE001
            issues.append(f"[{case_id}] ontology parse failed: {exc}")
            continue
        if not schema.classes:
            issues.append(f"[{case_id}] ontology 未解析到 class")

        expanded = set(schema.target_identifiers(case.get("target_types") or []))
        expected = case.get("expected") or {}
        gold_entities = expected.get("entities", [])
        gold_relations = expected.get("relations", [])
        gold_keys: Counter[tuple[str, str]] = Counter()

        for entity in gold_entities:
            key = (str(entity.get("type") or ""), canonical(entity.get("name")))
            gold_keys[key] += 1
            if gold_keys[key] > 1:
                issues.append(f"[{case_id}] gold 实体重复: {key}")
            ontology_class = schema.resolve_class(entity.get("type"))
            if ontology_class is None:
                issues.append(f"[{case_id}] gold 实体类型未知: {entity.get('type')!r}")
                continue
            if case.get("target_types") and ontology_class.identifier not in expanded:
                issues.append(
                    f"[{case_id}] gold 实体类型超出目标范围: {ontology_class.identifier}"
                )
            for prop_name, value in (entity.get("properties") or {}).items():
                if schema.resolve_property(ontology_class.identifier, prop_name) is None:
                    issues.append(
                        f"[{case_id}] gold 属性未知: {ontology_class.identifier}.{prop_name}"
                    )

        for relation in gold_relations:
            from_key = (str(relation.get("from_type") or ""), canonical(relation.get("from_name")))
            to_key = (str(relation.get("to_type") or ""), canonical(relation.get("to_name")))
            rel_type = str(relation.get("rel_type") or "")
            if from_key not in gold_keys:
                issues.append(f"[{case_id}] gold 关系源端点缺失: {from_key} -> {rel_type}")
            if to_key not in gold_keys:
                issues.append(f"[{case_id}] gold 关系目标端点缺失: {rel_type} -> {to_key}")
            candidates = schema.resolve_relation(rel_type)
            if not candidates:
                issues.append(f"[{case_id}] gold 关系类型未知: {rel_type!r}")
                continue
            from_class = schema.resolve_class(relation.get("from_type"))
            to_class = schema.resolve_class(relation.get("to_type"))
            if from_class and to_class and not any(
                schema.is_assignable(from_class.identifier, cand.source)
                and schema.is_assignable(to_class.identifier, cand.target)
                for cand in candidates
            ):
                issues.append(
                    f"[{case_id}] gold 关系端点类型不匹配: {from_class.identifier} -[{rel_type}]-> "
                    f"{to_class.identifier}"
                )

        text_norm = collapse(case.get("text") or "")
        for entity in gold_entities:
            quote = (entity.get("evidence") or {}).get("quote")
            if quote and collapse(quote) not in text_norm:
                issues.append(
                    f"[{case_id}] gold 实体证据不在原文中: "
                    f"{entity.get('type')}::{entity.get('name')} quote={quote[:40]!r}"
                )
        for relation in gold_relations:
            quote = (relation.get("evidence") or {}).get("quote")
            if quote and collapse(quote) not in text_norm:
                issues.append(
                    f"[{case_id}] gold 关系证据不在原文中: {relation.get('rel_type')} quote={quote[:40]!r}"
                )
    return issues


# ─────────────────────────── 指标 ───────────────────────────


def _norm_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple, set)):
        return tuple(sorted(_norm_value(item) for item in value))
    if isinstance(value, dict):
        return tuple(sorted((key, _norm_value(item)) for key, item in value.items()))
    return str(value or "").strip()


def values_equal(left: Any, right: Any) -> bool:
    return _norm_value(left) == _norm_value(right)


def entity_key(entity: dict[str, Any], canonical: Callable[[Any], str]) -> tuple[str, str]:
    return (str(entity.get("type") or ""), canonical(entity.get("name")))


def relation_key(relation: dict[str, Any], canonical: Callable[[Any], str]) -> tuple[str, str, str, str, str]:
    return (
        str(relation.get("from_type") or ""),
        canonical(relation.get("from_name")),
        str(relation.get("rel_type") or ""),
        str(relation.get("to_type") or ""),
        canonical(relation.get("to_name")),
    )


def _f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _prf(gold: list[dict[str, Any]], pred: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    gold_counts = Counter(key_fn(item) for item in gold)
    pred_counts = Counter(key_fn(item) for item in pred)
    tp = sum(min(gold_counts[key], pred_counts[key]) for key in gold_counts)
    fp = sum(max(0, pred_counts[key] - gold_counts[key]) for key in pred_counts)
    fn = sum(max(0, gold_counts[key] - pred_counts[key]) for key in gold_counts)
    precision, recall, f1 = _f1(tp, fp, fn)
    applicable = bool(gold or pred)
    if not applicable:
        precision = recall = f1 = None
    return {
        "gold": len(gold),
        "pred": len(pred),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "applicable": applicable,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _property_metrics(
    gold_entities: list[dict[str, Any]],
    pred_entities: list[dict[str, Any]],
    canonical: Callable[[Any], str],
) -> dict[str, Any]:
    """在匹配到的 gold 实体上比较属性（排除合成 name 键）。"""
    gold_index: dict[tuple[str, str], dict[str, Any]] = {}
    for entity in gold_entities:
        gold_index.setdefault(entity_key(entity, canonical), entity)
    gold_props_total = 0
    pred_props_total = 0
    hits = 0
    exact_hits = 0
    matched = 0
    for entity in pred_entities:
        gold = gold_index.get(entity_key(entity, canonical))
        if gold is None:
            continue
        matched += 1
        gold_props = {k: v for k, v in (gold.get("properties") or {}).items() if k != "name"}
        pred_props = {k: v for k, v in (entity.get("properties") or {}).items() if k != "name"}
        gold_props_total += len(gold_props)
        pred_props_total += len(pred_props)
        for key, value in gold_props.items():
            if key in pred_props and values_equal(pred_props[key], value):
                hits += 1
                if pred_props[key] == value:
                    exact_hits += 1
    return {
        "matched_entities": matched,
        "gold_props": gold_props_total,
        "pred_props": pred_props_total,
        "hits": hits,
        "exact_hits": exact_hits,
        "precision": hits / pred_props_total if pred_props_total else 0.0,
        "recall": hits / gold_props_total if gold_props_total else 0.0,
        "exact_accuracy": exact_hits / gold_props_total if gold_props_total else 0.0,
        "normalized_accuracy": hits / gold_props_total if gold_props_total else 0.0,
    }


def _schema_metrics(result: dict[str, Any], pred_entities: list[dict[str, Any]], pred_relations: list[dict[str, Any]]) -> dict[str, Any]:
    warnings = result.get("warnings") or []
    codes = Counter(str(warning.get("code")) for warning in warnings if isinstance(warning, dict))
    schema_error_count = sum(count for code, count in codes.items() if code in SCHEMA_WARNING_CODES)
    total_items = len(pred_entities) + len(pred_relations)
    return {
        "warning_count": len(warnings),
        "schema_error_count": schema_error_count,
        "schema_error_rate": schema_error_count / total_items if total_items else 0.0,
        "code_counts": dict(codes),
        "scope_violations": int(codes.get("ENTITY_OUT_OF_SCOPE", 0)),
        "unknown_entity_types": int(codes.get("UNKNOWN_ENTITY_TYPE", 0)),
        "unknown_properties": int(codes.get("UNKNOWN_PROPERTY", 0)),
        "unknown_relation_types": int(codes.get("UNKNOWN_RELATION_TYPE", 0)),
        "relation_endpoint_mismatches": int(codes.get("RELATION_ENDPOINT_TYPE_MISMATCH", 0)),
    }


def _failure_metrics(
    result: dict[str, Any],
    pred_entities: list[dict[str, Any]],
    pred_relations: list[dict[str, Any]],
    canonical: Callable[[Any], str],
) -> dict[str, Any]:
    attempts = result.get("attempts") or []
    json_parse_failures = sum(1 for attempt in attempts if isinstance(attempt, dict) and attempt.get("type") == "json")
    llm_request_failures = sum(1 for attempt in attempts if isinstance(attempt, dict) and attempt.get("type") == "request")
    entity_keys = Counter(entity_key(entity, canonical) for entity in pred_entities)
    duplicate_entities = sum(max(0, count - 1) for count in entity_keys.values())
    known_entity_keys = set(entity_keys)
    dangling_relations = 0
    for relation in pred_relations:
        from_key = (str(relation.get("from_type") or ""), canonical(relation.get("from_name")))
        to_key = (str(relation.get("to_type") or ""), canonical(relation.get("to_name")))
        if from_key not in known_entity_keys or to_key not in known_entity_keys:
            dangling_relations += 1
    return {
        "json_parse_failures": json_parse_failures,
        "llm_request_failures": llm_request_failures,
        "duplicate_entities": duplicate_entities,
        "dangling_relations": dangling_relations,
        "error_code": result.get("error_code"),
    }


def _composite_metrics(case: dict[str, Any], gold: dict[str, Any], pred: dict[str, Any], canonical: Callable[[Any], str]) -> dict[str, Any] | None:
    if case.get("category") not in COMPOSITE_CATEGORIES:
        return None
    membership_relation = "hasSubRule"
    gold_memberships = {
        relation_key(relation, canonical)
        for relation in gold.get("relations", [])
        if relation.get("rel_type") == membership_relation
    }
    pred_memberships = {
        relation_key(relation, canonical)
        for relation in pred.get("relations", [])
        if relation.get("rel_type") == membership_relation
    }
    membership_recall = (
        len(gold_memberships & pred_memberships) / len(gold_memberships) if gold_memberships else 1.0
    )
    gold_composites = {
        entity_key(entity, canonical): (entity.get("properties") or {}).get("combinationType")
        for entity in gold.get("entities", [])
        if entity.get("type") == "CompositeRule"
    }
    correct = 0
    for entity in pred.get("entities", []):
        key = entity_key(entity, canonical)
        if key not in gold_composites:
            continue
        if values_equal((entity.get("properties") or {}).get("combinationType"), gold_composites[key]):
            correct += 1
    combination_accuracy = correct / len(gold_composites) if gold_composites else 1.0
    composite_accuracy = (membership_recall + combination_accuracy) / 2
    return {
        "membership_relation": membership_relation,
        "membership_recall": membership_recall,
        "combination_accuracy": combination_accuracy,
        "composite_accuracy": composite_accuracy,
    }


def unified_score(metrics: dict[str, Any]) -> dict[str, Any]:
    weighted: list[tuple[float, float]] = []
    if metrics["entity"]["f1"] is not None:
        weighted.append((0.3, metrics["entity"]["f1"]))
    if metrics["property"]["normalized_accuracy"] is not None:
        weighted.append((0.2, metrics["property"]["normalized_accuracy"]))
    if metrics["relation"]["f1"] is not None:
        weighted.append((0.3, metrics["relation"]["f1"]))
    composite = metrics.get("composite")
    if composite is not None:
        weighted.append((0.2, composite["composite_accuracy"]))
    total_weight = sum(weight for weight, _ in weighted)
    score = sum(weight * value for weight, value in weighted) / total_weight if total_weight else 0.0
    return {"score": score, "frozen": False, "formula": UNIFIED_FORMULA}


def compute_case_metrics(case: dict[str, Any], result: dict[str, Any], modules: dict[str, Any]) -> dict[str, Any]:
    canonical = modules["graph_utils"].canonical_name
    gold = case.get("expected") or {}
    pred = result.get("raw") or {}
    gold_entities = gold.get("entities", [])
    gold_relations = gold.get("relations", [])
    pred_entities = pred.get("entities", []) if isinstance(pred, dict) else []
    pred_relations = pred.get("relations", []) if isinstance(pred, dict) else []
    metrics = {
        "id": case["id"],
        "category": case.get("category", ""),
        "name": case.get("name", ""),
        "entity": _prf(gold_entities, pred_entities, lambda item: entity_key(item, canonical)),
        "property": _property_metrics(gold_entities, pred_entities, canonical),
        "relation": _prf(gold_relations, pred_relations, lambda item: relation_key(item, canonical)),
        "schema": _schema_metrics(result, pred_entities, pred_relations),
        "failure": _failure_metrics(result, pred_entities, pred_relations, canonical),
        "composite": _composite_metrics(case, gold, pred, canonical),
        "evidence_coverage": None,
    }
    metrics["unified"] = unified_score(metrics)
    return metrics


def _pooled_prf(gold_items: list[dict[str, Any]], pred_items: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    return _prf(gold_items, pred_items, key_fn)


def compute_aggregate(cases: list[dict[str, Any]], per_case: list[dict[str, Any]], modules: dict[str, Any]) -> dict[str, Any]:
    canonical = modules["graph_utils"].canonical_name
    gold_entities = [e for case in cases for e in (case.get("expected") or {}).get("entities", [])]
    gold_relations = [r for case in cases for r in (case.get("expected") or {}).get("relations", [])]
    pred_entities = [e for metrics in per_case for e in metrics.get("_raw_pred_entities", [])]
    pred_relations = [r for metrics in per_case for r in metrics.get("_raw_pred_relations", [])]

    def entity_key_fn(item: dict[str, Any]) -> tuple[str, str]:
        return entity_key(item, canonical)

    def relation_key_fn(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return relation_key(item, canonical)

    micro = {
        "entity": _pooled_prf(gold_entities, pred_entities, entity_key_fn),
        "relation": _pooled_prf(gold_relations, pred_relations, relation_key_fn),
        "property": {
            "gold_props": sum(m["property"]["gold_props"] for m in per_case),
            "pred_props": sum(m["property"]["pred_props"] for m in per_case),
            "hits": sum(m["property"]["hits"] for m in per_case),
            "exact_hits": sum(m["property"]["exact_hits"] for m in per_case),
        },
        "schema_error_count": sum(m["schema"]["schema_error_count"] for m in per_case),
        "json_parse_failures": sum(m["failure"]["json_parse_failures"] for m in per_case),
        "llm_request_failures": sum(m["failure"]["llm_request_failures"] for m in per_case),
        "duplicate_entities": sum(m["failure"]["duplicate_entities"] for m in per_case),
        "dangling_relations": sum(m["failure"]["dangling_relations"] for m in per_case),
    }
    micro["property"]["precision"] = (
        micro["property"]["hits"] / micro["property"]["pred_props"] if micro["property"]["pred_props"] else 0.0
    )
    micro["property"]["recall"] = (
        micro["property"]["hits"] / micro["property"]["gold_props"] if micro["property"]["gold_props"] else 0.0
    )
    micro["property"]["exact_accuracy"] = (
        micro["property"]["exact_hits"] / micro["property"]["gold_props"] if micro["property"]["gold_props"] else 0.0
    )
    micro["property"]["normalized_accuracy"] = micro["property"]["recall"]

    def mean(values: list[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return sum(present) / len(present) if present else None

    macro_values = [m["unified"]["score"] for m in per_case]
    macro_unified = sum(macro_values) / len(macro_values) if macro_values else 0.0
    micro_unified = (
        0.3 * (micro["entity"]["f1"] or 0.0)
        + 0.2 * micro["property"]["normalized_accuracy"]
        + 0.3 * (micro["relation"]["f1"] or 0.0)
    )
    composite_values = [m["composite"]["composite_accuracy"] for m in per_case if m.get("composite")]
    if composite_values:
        micro_unified = (
            0.3 * (micro["entity"]["f1"] or 0.0)
            + 0.2 * micro["property"]["normalized_accuracy"]
            + 0.3 * (micro["relation"]["f1"] or 0.0)
            + 0.2 * (sum(composite_values) / len(composite_values))
        )
    return {
        "macro": {
            "entity_f1": mean([m["entity"]["f1"] for m in per_case]),
            "entity_precision": mean([m["entity"]["precision"] for m in per_case]),
            "entity_recall": mean([m["entity"]["recall"] for m in per_case]),
            "property_normalized_accuracy": mean([m["property"]["normalized_accuracy"] for m in per_case]),
            "property_exact_accuracy": mean([m["property"]["exact_accuracy"] for m in per_case]),
            "relation_f1": mean([m["relation"]["f1"] for m in per_case]),
            "relation_precision": mean([m["relation"]["precision"] for m in per_case]),
            "relation_recall": mean([m["relation"]["recall"] for m in per_case]),
            "schema_error_rate": mean([m["schema"]["schema_error_rate"] for m in per_case]),
            "composite_accuracy": sum(composite_values) / len(composite_values) if composite_values else None,
            "unified_candidate": macro_unified,
        },
        "micro": micro,
        "micro_unified_candidate": micro_unified,
        "formula": UNIFIED_FORMULA,
    }


# ─────────────────────────── 抽取执行 ───────────────────────────


def _build_graph(raw: dict[str, Any], modules: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """复刻 extract_v2._entities_to_graph 的图契约（stub 模式使用）。"""
    gu = modules["graph_utils"]
    coerce = gu.coerce_non_empty_name
    canonical = gu.canonical_name
    nodes: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    node_index: dict[str, dict[str, Any]] = {}

    for entity in raw.get("entities", []):
        if not isinstance(entity, dict):
            continue
        entity_type = coerce(entity.get("type")) or "Element"
        entity_name = coerce(entity.get("name"))
        properties = entity.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        uid = gu.entity_uid(entity_type, entity_name, properties)
        if uid not in node_index:
            node = {"uid": uid, "label": entity_type, "properties": gu.build_node_properties(entity_name, properties)}
            nodes.append(node)
            node_index[uid] = node
        else:
            node_index[uid]["properties"] = gu.merge_node_properties(
                node_index[uid].get("properties"), properties
            )

    relationship_keys: set[tuple[str, str, str]] = set()
    for relation in raw.get("relations", []):
        if not isinstance(relation, dict):
            continue
        from_type = coerce(relation.get("from_type"))
        from_name = coerce(relation.get("from_name"))
        to_type = coerce(relation.get("to_type"))
        to_name = coerce(relation.get("to_name"))
        relation_type = coerce(relation.get("rel_type"))
        if not all((from_type, from_name, relation_type, to_type, to_name)):
            continue
        from_uid = gu.entity_uid(from_type, from_name, None)
        to_uid = gu.entity_uid(to_type, to_name, None)
        key = (from_uid, to_uid, relation_type)
        if key in relationship_keys:
            continue
        relationship_keys.add(key)
        relationships.append({"from_uid": from_uid, "to_uid": to_uid, "type": relation_type})

    relationships = gu.filter_relationships_with_known_nodes(relationships, set(node_index))
    return {"nodes": nodes, "relationships": relationships}


def _stub_extractor(case: dict[str, Any], mode: str, modules: dict[str, Any]) -> dict[str, Any]:
    if mode == "stub:gold":
        raw = copy.deepcopy(case.get("expected") or {"entities": [], "relations": []})
    elif mode == "stub:empty":
        raw = {"entities": [], "relations": []}
    else:  # stub:error
        return {
            "raw": {},
            "graph": {"nodes": [], "relationships": []},
            "warnings": [],
            "attempts": [{"attempt": 1, "type": "json", "message": "stub:error 注入的解析失败"}],
            "error_code": "LLM_JSON_PARSE_FAILED",
        }
    return {
        "raw": raw,
        "graph": _build_graph(raw, modules),
        "warnings": [],
        "attempts": [],
        "profile": {"document_type": case.get("document_type", "auto"), "stub": mode},
    }


def _live_extractor_factory(module: str) -> Callable[..., dict[str, Any]]:
    directory = _module_dir(module)
    loaded = load_package_modules(module, directory, ["config.py", "scheduler.py", "graph_utils.py", "ontology_schema.py", "extractor.py"])
    extractor = loaded.get("extractor")
    if isinstance(extractor, Exception):
        raise SystemExit(
            f"无法加载 {module}.extractor（需要安装 openai 且配置 LLM 端点）: {extractor}"
        )
    return extractor.extract_v2


def _run_case(case: dict[str, Any], extractor: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "ontology": ontology_text(case),
        "text": case.get("text", ""),
        "document_type": case.get("document_type", "auto"),
        "validation_mode": case.get("validation_mode", "strict"),
    }
    target_types = case.get("target_types") or []
    if target_types:
        kwargs["target_types"] = list(target_types)
    return extractor(**kwargs)


def run_baseline(
    cases: list[dict[str, Any]],
    mode: str,
    module: str,
    out_path: Path,
    raw_dir: Path,
) -> dict[str, Any]:
    if mode == "live":
        extractor = _live_extractor_factory(module)
    else:
        if mode not in {"stub:gold", "stub:empty", "stub:error"}:
            raise SystemExit(f"未知 mode: {mode}")

    raw_dir.mkdir(parents=True, exist_ok=True)
    per_case: list[dict[str, Any]] = []
    modules = _non_llm_modules(module)
    for case in cases:
        try:
            if mode == "live":
                result = _run_case(case, extractor)
            else:
                result = _stub_extractor(case, mode, modules)
        except Exception as exc:  # noqa: BLE001
            result = {
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "warnings": [],
                "attempts": [{"attempt": 1, "type": "exception", "message": str(exc)}],
                "error_code": "EXTRACTION_EXCEPTION",
            }
        (raw_dir / f"{case['id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        metrics = compute_case_metrics(case, result, modules)
        pred = result.get("raw") or {}
        metrics["_raw_pred_entities"] = pred.get("entities", []) if isinstance(pred, dict) else []
        metrics["_raw_pred_relations"] = pred.get("relations", []) if isinstance(pred, dict) else []
        metrics["_result_summary"] = {
            "raw_entities": len(metrics["_raw_pred_entities"]),
            "raw_relations": len(metrics["_raw_pred_relations"]),
            "graph_nodes": len((result.get("graph") or {}).get("nodes", [])),
            "graph_relationships": len((result.get("graph") or {}).get("relationships", [])),
            "error_code": result.get("error_code"),
            "attempts": result.get("attempts", []),
            "warnings": result.get("warnings", []),
            "profile": result.get("profile", {}),
        }
        per_case.append(metrics)

    aggregate = compute_aggregate(cases, per_case, modules)
    meta = {
        "purpose": "Phase 1 Step 1：冻结旧 extract_v2() baseline",
        "frozen_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "extractor_module": module,
        "mode": mode,
        "stub": mode.startswith("stub"),
        "llm": {
            "base_url_configured": bool(
                os.environ.get("KG_EXTRACT_LLM_BASE_URL") or os.environ.get("KG_EXTRACT_API_URL")
            ),
            "model": os.environ.get("KG_EXTRACT_LLM_MODEL") or os.environ.get("KG_EXTRACT_MODEL") or "deepseek-v4-flash-0731(default)",
        },
        "unified_metric_formula": UNIFIED_FORMULA,
        "note": (
            "stub 模式仅用于验证评测链路，不能作为正式基线。"
            "正式基线需配置 KG_EXTRACT_LLM_BASE_URL / KG_EXTRACT_LLM_API_KEY 后以 --mode live 重新冻结。"
            if mode.startswith("stub")
            else "正式 extract_v2() 基线。"
        ),
    }
    for metrics in per_case:
        metrics.pop("_raw_pred_entities", None)
        metrics.pop("_raw_pred_relations", None)
    payload = {"meta": meta, "cases": per_case, "aggregate": aggregate}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload


# ─────────────────────────── 报告 ───────────────────────────


def _pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def render_report(payload: dict[str, Any]) -> str:
    meta = payload.get("meta", {})
    aggregate = payload.get("aggregate", {}).get("macro", {})
    lines = [
        "# Phase 1 Step 1 基准集报告",
        "",
        f"- 冻结时间：{meta.get('frozen_at')}",
        f"- 抽取模块：`{meta.get('extractor_module')}`",
        f"- 模式：`{meta.get('mode')}`（stub={meta.get('stub')}）",
        f"- LLM 端点已配置：{meta.get('llm', {}).get('base_url_configured')}；模型：{meta.get('llm', {}).get('model')}",
        f"- 说明：{meta.get('note', '')}",
        "",
        "## 汇总（macro 平均）",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| Entity F1 | {_pct(aggregate.get('entity_f1'))} |",
        f"| Entity Precision | {_pct(aggregate.get('entity_precision'))} |",
        f"| Entity Recall | {_pct(aggregate.get('entity_recall'))} |",
        f"| Property Normalized Accuracy | {_pct(aggregate.get('property_normalized_accuracy'))} |",
        f"| Property Exact Accuracy | {_pct(aggregate.get('property_exact_accuracy'))} |",
        f"| Relation F1 | {_pct(aggregate.get('relation_f1'))} |",
        f"| Relation Precision | {_pct(aggregate.get('relation_precision'))} |",
        f"| Relation Recall | {_pct(aggregate.get('relation_recall'))} |",
        f"| Schema Error Rate | {_pct(aggregate.get('schema_error_rate'))} |",
        f"| Composite Accuracy | {_pct(aggregate.get('composite_accuracy'))} |",
        f"| 统一指标（候选，未冻结） | {_pct(aggregate.get('unified_candidate'))} |",
        "",
        f"统一指标口径：{meta.get('unified_metric_formula', '')}",
        "",
        "## 失败归因（汇总）",
        "",
        "| 失败类型 | 数量 |",
        "| --- | --- |",
    ]
    micro = payload.get("aggregate", {}).get("micro") or {}
    lines += [
        f"| JSON 解析失败 | {micro.get('json_parse_failures', 0)} |",
        f"| LLM 请求失败 | {micro.get('llm_request_failures', 0)} |",
        f"| 重复实体 | {micro.get('duplicate_entities', 0)} |",
        f"| 悬空关系 | {micro.get('dangling_relations', 0)} |",
        f"| Schema 错误 | {micro.get('schema_error_count', 0)} |",
        "",
        "## 分用例",
        "",
        "| 用例 | 类别 | Entity F1 | Prop Acc | Relation F1 | Schema Err | Composite | 统一指标 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for metrics in payload.get("cases", []):
        composite = metrics.get("composite")
        lines.append(
            "| "
            + " | ".join(
                [
                    metrics.get("id", ""),
                    metrics.get("category", ""),
                    _pct(metrics["entity"]["f1"]),
                    _pct(metrics["property"]["normalized_accuracy"]),
                    _pct(metrics["relation"]["f1"]),
                    f"{metrics['schema']['schema_error_count']}",
                    _pct(composite["composite_accuracy"] if composite else None),
                    _pct(metrics["unified"]["score"]),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## 失败归因（分用例）",
        "",
        "| 用例 | JSON 解析失败 | LLM 请求失败 | 重复实体 | 悬空关系 | Schema 错误 | error_code |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for metrics in payload.get("cases", []):
        failure = metrics["failure"]
        summary = metrics.get("_result_summary", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    metrics.get("id", ""),
                    str(failure["json_parse_failures"]),
                    str(failure["llm_request_failures"]),
                    str(failure["duplicate_entities"]),
                    str(failure["dangling_relations"]),
                    str(metrics["schema"]["schema_error_count"]),
                    str(summary.get("error_code") or ""),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────── CLI ───────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 1 Step 1 基准集评测与基线冻结")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="校验基准集（本体/gold/证据）")
    check.add_argument("--cases", default=str(CASES_DIR))

    run = sub.add_parser("run", help="运行抽取并冻结基线")
    run.add_argument("--mode", required=True, choices=["live", "stub:gold", "stub:empty", "stub:error"])
    run.add_argument("--module", default="knowledge_extract_v2", help="抽取模块名（knowledge_extract_v2 或 knowledge_extract）")
    run.add_argument("--cases", default=str(CASES_DIR))
    run.add_argument("--out", default=str(RESULTS_DIR / "baseline.json"))

    baseline = sub.add_parser("baseline", help="run 的别名")
    baseline.add_argument("--mode", required=True, choices=["live", "stub:gold", "stub:empty", "stub:error"])
    baseline.add_argument("--module", default="knowledge_extract_v2")
    baseline.add_argument("--cases", default=str(CASES_DIR))
    baseline.add_argument("--out", default=str(RESULTS_DIR / "baseline.json"))

    report = sub.add_parser("report", help="从 baseline.json 生成 Markdown 报告")
    report.add_argument("--run", dest="run_file", default=str(RESULTS_DIR / "baseline.json"))
    report.add_argument("--out", default=str(RESULTS_DIR / "baseline_report.md"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "check":
        modules = _non_llm_modules("knowledge_extract_v2")
        cases = load_dataset(Path(args.cases))
        issues = check_dataset(cases, modules)
        if issues:
            print(f"基准集自检发现 {len(issues)} 个问题：")
            for issue in issues:
                print(f"  - {issue}")
            return 1
        print(f"基准集自检通过：{len(cases)} 个用例（{', '.join(sorted({c['category'] for c in cases}))}）")
        return 0

    if args.command in {"run", "baseline"}:
        cases = load_dataset(Path(args.cases))
        payload = run_baseline(cases, args.mode, args.module, Path(args.out), RAW_DIR)
        print(f"基线已写入: {Path(args.out)}")
        aggregate = payload["aggregate"]["macro"]
        print(
            "汇总: entity_f1=%.4f property_acc=%.4f relation_f1=%.4f unified=%.4f"
            % (
                aggregate["entity_f1"],
                aggregate["property_normalized_accuracy"],
                aggregate["relation_f1"],
                aggregate["unified_candidate"],
            )
        )
        report_path = Path(args.out).with_name("baseline_report.md")
        report_path.write_text(render_report(payload), encoding="utf-8")
        print(f"报告已写入: {report_path}")
        return 0

    if args.command == "report":
        payload = json.loads(Path(args.run_file).read_text(encoding="utf-8"))
        report_path = Path(args.out)
        report_path.write_text(render_report(payload), encoding="utf-8")
        print(f"报告已写入: {report_path}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
