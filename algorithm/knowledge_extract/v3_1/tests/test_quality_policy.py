"""预算、阈值与"图谱可信 / 覆盖缺口"口径的回归。

这些测试锁定一次真实 mini 评测暴露的问题：Clef 预算被前面阶段吃光导致关系
阶段饿死、阈值把已经判成支持的候选丢掉、候选被拒被误当成整图不可信、空结果
被当成成功抽取。全部使用确定性模型替身，不依赖网络或 GPU。
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract_aggregate, extract_harness
from algorithm.knowledge_extract.v3_1 import harness as harness_module
from algorithm.knowledge_extract.v3_1.decisions import DecisionResult
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _catalog_id, _empty, _entity, _relation,
)


class FixedConfidenceDecider:
    """按固定概率作答的裁决器：用于验证阈值本身，而不是模型行为。"""

    def __init__(self, selected="supported", probability=.92, margin=.80, kinds=None):
        self.selected, self.probability, self.margin = selected, probability, margin
        self.kinds = set(kinds) if kinds else None

    def decide(self, tasks, *, deadline, trace):
        results = {}
        for task in tasks:
            probability = self.probability if self.kinds is None or task.kind in self.kinds else .995
            margin = self.margin if probability == self.probability else .99
            selected = self.selected if self.selected in task.criteria else next(iter(task.criteria))
            remaining = max(0.0, 1 - probability)
            others = [key for key in task.criteria if key != selected]
            probabilities = {key: (probability if key == selected
                                   else remaining / len(others) if others else 0.0)
                             for key in task.criteria}
            results[task.task_id] = DecisionResult(task_id=task.task_id, selected=selected,
                                                   probabilities=probabilities, margin=margin)
        return results


def _relation_model(text, *, fail_times=0, evidence_chunk="source-A", bad_evidence=False):
    calls = {"relations": 0}

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text), _entity("b", "B", "乙", text)]}
        if stage == "relations":
            calls["relations"] += 1
            if calls["relations"] <= fail_times:
                raise TimeoutError("provider timeout")
            relation = _relation(payload, "甲", "assists", "乙", text, chunk=evidence_chunk)
            if bad_evidence:
                relation["evidence"] = [{"chunk_id": evidence_chunk, "quote": "原文里没有这句话"}]
            return {"relations": [relation]}
        return _empty(stage)

    return model, calls


# ── Clef 预算 ──────────────────────────────────────────────────────────────

def test_clef_call_budget_defaults_to_512_and_follows_env(monkeypatch):
    """128 次/文档在真实公文上被前面阶段吃光，关系阶段必然饿死。"""
    seen = {}

    class RecordingDecider:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def decide(self, tasks, *, deadline, trace):
            return {}

    monkeypatch.setattr(harness_module, "ClefDecider", RecordingDecider)
    monkeypatch.delenv("KG_EXTRACT_V31_CLEF_MAX_CALLS", raising=False)
    text = "甲存在。"
    llm = ScriptedLLM(lambda stage, _payload, _context: {"entities": [_entity("a", "A", "甲", text)]}
                      if stage == "entities" else _empty(stage))
    extract_harness("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)], llm=llm)
    assert seen["max_calls"] == 512

    seen.clear()
    monkeypatch.setenv("KG_EXTRACT_V31_CLEF_MAX_CALLS", "1024")
    extract_harness("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)], llm=llm)
    assert seen["max_calls"] == 1024


def test_clef_call_budget_must_stay_positive(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_V31_CLEF_MAX_CALLS", "0")
    text = "甲存在。"
    llm = ScriptedLLM(lambda stage, _payload, _context: {"entities": []} if stage == "entities" else _empty(stage))
    with pytest.raises(ValueError, match="call limits must be positive"):
        extract_harness("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)], llm=llm)


# ── 阈值 ───────────────────────────────────────────────────────────────────

def test_support_threshold_accepts_calibrated_near_threshold_decision():
    """Clef 给出的 supported/margin 很高但概率不足旧 0.95 时不得被丢弃。"""
    text = "甲协助乙。"
    ontology = "@startuml\nclass A\nclass B\nA --> B : assists\n@enduml"
    model, _ = _relation_model(text)
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=FixedConfidenceDecider(probability=.92), use_scheduler=False)
    assert [relation["rel_type"] for relation in result["raw"]["relations"]] == ["assists"]
    assert result["verification"]["coverage_complete"] is True


def test_support_threshold_remains_configurable_and_still_rejects_low_confidence():
    text = "甲协助乙。"
    ontology = "@startuml\nclass A\nclass B\nA --> B : assists\n@enduml"
    model, _ = _relation_model(text)
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=FixedConfidenceDecider(probability=.92, kinds=["relation_support"]),
                               support_threshold=.95, use_scheduler=False, max_repair_rounds=0)
    assert result["raw"]["relations"] == []
    assert [entity["name"] for entity in result["raw"]["entities"]] == ["甲", "乙"]
    # 阈值过滤属于召回缺口，不改图谱可信度。
    assert result["verification"]["valid"] is True
    assert result["status"] == "succeeded"


def test_invalid_threshold_setting_is_rejected(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_V31_SUPPORT_THRESHOLD", "1.5")
    text = "甲存在。"
    llm = ScriptedLLM(lambda stage, _payload, _context: {"entities": []} if stage == "entities" else _empty(stage))
    with pytest.raises(ValueError, match="KG_EXTRACT_V31_SUPPORT_THRESHOLD"):
        extract_harness("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)], llm=llm)


# ── 关系阶段重试 ────────────────────────────────────────────────────────────

def test_relation_retry_recovers_a_timed_out_pass_and_retires_the_transient_issue():
    text = "甲协助乙。"
    ontology = "@startuml\nclass A\nclass B\nA --> B : assists\n@enduml"
    model, calls = _relation_model(text, fail_times=1)
    llm = ScriptedLLM(model)
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=llm,
                               decider=SemanticDecider(), use_scheduler=False)
    assert calls["relations"] == 2
    assert [relation["rel_type"] for relation in result["raw"]["relations"]] == ["assists"]
    # 首次超时留下的问题必须随重试成功一起撤回，否则仍会否决整图。
    timeout_issues = [issue for issue in result["run_trace"]["issues"] if issue["code"] == "LLM_TIMEOUT"]
    assert timeout_issues and all(issue["status"] == "resolved" for issue in timeout_issues)
    assert result["verification"]["valid"] is True
    assert result["status"] == "succeeded"
    assert result["error_code"] is None


def test_relation_retry_can_be_disabled():
    text = "甲协助乙。"
    ontology = "@startuml\nclass A\nclass B\nA --> B : assists\n@enduml"
    model, calls = _relation_model(text, fail_times=1)
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False, relation_retries=0)
    assert calls["relations"] == 1
    assert result["raw"]["relations"] == []
    # 执行失败说明这一段原文没有被读完：记入 failed_chunks 并保持 partial。
    assert result["status"] == "partial"
    assert result["error_code"] == "LLM_TIMEOUT"
    assert result["verification"]["failed_chunks"] == ["source-A"]


# ── 候选被拒 ≠ 图谱不可信 ──────────────────────────────────────────────────

def test_rejected_relation_candidate_keeps_the_published_graph_publishable():
    text = "甲协助乙。"
    ontology = "@startuml\nclass A\nclass B\nA --> B : assists\n@enduml"
    model, _ = _relation_model(text, bad_evidence=True)
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False, max_repair_rounds=0)
    assert result["raw"]["relations"] == []
    assert any(issue["code"] == "INVALID_RELATION_REFERENCE_OR_EVIDENCE" for issue in result["coverage_gaps"])
    assert result["verification"]["valid"] is True
    assert result["verification"]["coverage_complete"] is False
    assert result["status"] == "succeeded"


def test_strict_coverage_switch_restores_the_old_all_or_nothing_gate():
    text = "甲协助乙。"
    ontology = "@startuml\nclass A\nclass B\nA --> B : assists\n@enduml"
    model, _ = _relation_model(text, bad_evidence=True)
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False, max_repair_rounds=0,
                               strict_coverage=True)
    assert result["verification"]["valid"] is False
    assert result["status"] == "partial"


def test_issue_superseded_when_the_same_fact_is_later_published_from_another_chunk():
    """同一三元组在别的切片以合法证据入图后，旧的拒绝记录不得继续否决整图。"""
    texts = {"source-A": "甲协助乙。", "source-B": "甲协助乙。"}
    ontology = "@startuml\nclass A\nclass B\nA --> B : assists\n@enduml"

    def model(stage, payload, context):
        chunk = context["chunk_id"]
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", texts[chunk], chunk=chunk),
                                 _entity("b", "B", "乙", texts[chunk], chunk=chunk)]}
        if stage == "relations":
            relation = _relation(payload, "甲", "assists", "乙", texts[chunk], chunk=chunk)
            if chunk == "source-A":
                # 本切片给出的是别处的引文，locate 解析不到 → 候选被拒。
                relation["evidence"] = [{"chunk_id": "source-B", "quote": "不存在的原文"}]
            return {"relations": [relation]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk(key, value) for key, value in texts.items()],
                               llm=ScriptedLLM(model), decider=SemanticDecider(), use_scheduler=False,
                               max_repair_rounds=0)
    assert [relation["rel_type"] for relation in result["raw"]["relations"]] == ["assists"]
    stale = [issue for issue in result["run_trace"]["issues"]
             if issue["code"] == "INVALID_RELATION_REFERENCE_OR_EVIDENCE"]
    assert stale and all(issue["status"] == "superseded" for issue in stale)
    assert result["verification"]["valid"] is True
    assert result["status"] == "succeeded"


def test_integrity_issue_still_blocks_even_though_coverage_gaps_do_not():
    """重数冲突描述的是已发布图谱本身，必须继续阻断。"""
    text = "甲同时分配给乙和丙。"
    ontology = '@startuml\nclass A\nclass B\nA --> "0..1" B : assignedTo\n@enduml'

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text), _entity("b", "B", "乙", text),
                                 _entity("c", "B", "丙", text)]}
        if stage == "relations":
            return {"relations": [_relation(payload, "甲", "assignedTo", name, text) for name in ["乙", "丙"]]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False)
    assert result["verification"]["valid"] is False
    assert result["verification"]["hard_issue_count"] >= 1
    assert any(issue["code"] == "CARDINALITY_CONFLICT" for issue in result["verification"]["hard_issues"])
    assert result["status"] == "partial"


# ── 空结果自检 ─────────────────────────────────────────────────────────────

def test_empty_extraction_is_failed_and_says_so():
    text = "甲存在。"
    result = extract_aggregate("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(lambda stage, _payload, _context: _empty(stage)),
                               decider=SemanticDecider(), use_scheduler=False)
    assert result["status"] == "failed"
    assert result["error_code"] == "EMPTY_EXTRACTION"
    assert result["_error"]
    assert result["graph"]["nodes"] == [] and result["graph"]["relationships"] == []
    assert result["verification"]["valid"] is False


def test_blank_chunks_are_skipped_without_faking_success():
    result = extract_aggregate("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", "   \n  ")],
                               decider=SemanticDecider(), use_scheduler=False)
    assert result["status"] == "failed"
    assert result["error_code"] == "EMPTY_EXTRACTION"
    assert result["profile"]["skipped_empty_chunks"] == 1
