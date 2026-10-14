"""v2_2 Clef 预判：高置信负例跳过生成式关系调用，失败时回退。"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = PACKAGE_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    parent = sys.modules.get(package_name)
    if parent is None:
        parent = types.ModuleType(package_name)
        parent.__path__ = [str(directory)]
        parent.__package__ = package_name
        sys.modules[package_name] = parent
    loaded: dict[str, types.ModuleType] = {}
    for stem in stems:
        module_name = f"{package_name}.{stem}"
        if module_name in sys.modules:
            loaded[stem] = sys.modules[module_name]
            continue
        spec = importlib.util.spec_from_file_location(module_name, directory / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded[stem] = module
    return loaded


MODULES = _load_package_modules(
    "knowledge_extract_v22_v31",
    PACKAGE_DIR,
    [
        "config",
        "clef_gate",
        "ontology_compiler",
        "chunk_context",
        "evidence",
        "prompt_router",
        "relation_resolver",
        "relation_context",
        "candidate_generation",
        "split_extractor",
    ],
)
GATE = MODULES["clef_gate"]
CG = MODULES["candidate_generation"]
RC = MODULES["relation_context"]
CC = MODULES["chunk_context"]
EV = MODULES["evidence"]
OC = MODULES["ontology_compiler"]
PR = MODULES["prompt_router"]
SE = MODULES["split_extractor"]

from algorithm.knowledge_extract.v2_2_v31.tests.stub_llm import StubLLM  # noqa: E402


ONTOLOGY = """@startuml
class Person {
  name: String
}
class Organization {
  name: String
}
Person --> Organization : worksAt
@enduml
"""
TEXT = "张三就职于示例单位。"


class FakeClef:
    def __init__(self, responder):
        self.calls = []
        self.responder = responder

    def systemone(self, request, timeout=None):
        self.calls.append(request)
        answers = {}
        for question_id, question in request["questions"].items():
            answers[question_id] = self.responder(question_id, question, request["state"])
        return {
            "model": "clef-flash",
            "answers": answers,
            "usage": {"input_tokens": 1},
            "code": 0,
        }


class ServiceDown:
    def systemone(self, request, timeout=None):
        raise RuntimeError("clef down")


class TooLongThenOk:
    def __init__(self):
        self.calls = 0

    def systemone(self, request, timeout=None):
        self.calls += 1
        if len(request["questions"]) > 1:
            error = RuntimeError("too long")
            error.error_kind = "input_too_long"
            raise error
        question_id = next(iter(request["questions"]))
        return {
            "model": "clef-flash",
            "answers": {
                question_id: {"type": "choice", "choice": "NONE", "confidence": 0.99},
            },
            "usage": {"input_tokens": 1},
            "code": 0,
        }


def _pool(chunk_id: str, rows: list[tuple[str, str, str]]):
    entities = []
    for candidate_id, type_id, name in rows:
        entity = types.SimpleNamespace(
            candidate_id=candidate_id,
            canonical_type=type_id,
            proposed_type=type_id,
            name=name,
            source="",
            evidence=[
                EV.Evidence(
                    span_id=f"{chunk_id}:0:1",
                    source_block_ids=[chunk_id],
                    quote=name,
                    start_char=0,
                    end_char=len(name),
                )
            ],
        )
        entities.append(entity)

    class _Pool:
        def accepted_entities(self):
            return entities

        def entity_catalog(self):
            return [
                {"id": entity.candidate_id, "type": entity.canonical_type, "name": entity.name}
                for entity in entities
            ]

    return _Pool()


def _relation_pass(text: str, clef, names=(("e1", "Person", "张三"), ("e2", "Organization", "示例单位"))):
    plan = OC.compile_plan(ONTOLOGY)
    chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
    manager = CC.ChunkContextManager([chunk])
    request = CC.ExtractionRequest(chunks=[chunk])
    llm = StubLLM()
    results = CG.relation_pass(
        plan,
        manager,
        request,
        llm,
        _pool("c0", list(names)),
        route=PR.resolve_document_route(plan, [chunk]),
        max_workers=1,
        use_scheduler=False,
        attempts=[],
        warnings=[],
        local_previous=0,
        local_next=0,
        same_section=True,
        clef=clef,
    )
    relations = results[0][1] if results else []
    relation_calls = [call for call in llm.calls if call[0] == "relation"]
    return relations, relation_calls, llm


class ClefGateTest(unittest.TestCase):
    def test_cooccurrence_quote_uses_the_shared_sentence(self) -> None:
        quote = GATE.cooccurrence_quote(TEXT, "张三", "示例单位")
        self.assertEqual(quote, TEXT)

    def test_cooccurrence_quote_uses_line_without_period(self) -> None:
        text = "张三\t示例单位\n下一行无关"
        quote = GATE.cooccurrence_quote(text, "张三", "示例单位")
        self.assertEqual(quote, "张三\t示例单位")

    def test_cooccurrence_quote_prefers_nearest_pair(self) -> None:
        # 同一长句里两端各出现两次；首现对跨度超过 max_len，应落到最近一对。
        filler = "填充" * 120
        text = f"张三{filler}示例单位{filler}张三就职于示例单位"
        quote = GATE.cooccurrence_quote(text, "张三", "示例单位")
        self.assertEqual(quote, "张三就职于示例单位")
        self.assertNotIn("填充", quote)

    def test_anchor_positive_grounds_with_local_endpoint_quote(self) -> None:
        def respond(_qid, question, _state):
            label = next(key for key in question["criteria"] if key != "NONE")
            return {"type": "choice", "choice": label, "confidence": 0.96}

        gate = GATE.ClefGate(client=FakeClef(respond), enabled=True, accept=0.9)
        # 片段里只有目标侧名称（模拟文档锚点句），双端共现引文不可得。
        text = "请总部各部门阅办。"
        pair = {
            "pair_id": "c-anchor",
            "source_id": "doc",
            "target_id": "dept",
            "source_name": "关于开展示例专项工作的通知",
            "target_name": "总部各部门",
            "relation_id": "appliesTo",
            "preserve_on_none": True,
        }
        grounded, pending = gate.apply_relation_gate(text, [pair], chunk_id="c0", attempts=[])
        self.assertEqual(pending, [])
        self.assertEqual(len(grounded), 1)
        self.assertIn("总部各部门", grounded[0][1])

    def test_high_confidence_none_skips_generative_relation_call(self) -> None:
        client = FakeClef(lambda _qid, _question, _state: {
            "type": "choice", "choice": "NONE", "confidence": 0.97,
        })
        gate = GATE.ClefGate(client=client, enabled=True, accept=0.9)
        relations, relation_calls, _llm = _relation_pass(TEXT, gate)
        self.assertEqual(relations, [])
        self.assertEqual(relation_calls, [])
        self.assertGreaterEqual(gate.snapshot()["relation_dropped"], 1)
        self.assertEqual(gate.snapshot()["relation_sent_to_llm"], 0)

    def test_preserve_on_none_falls_back_to_llm(self) -> None:
        client = FakeClef(lambda _qid, _question, _state: {
            "type": "choice", "choice": "NONE", "confidence": 0.99,
        })
        gate = GATE.ClefGate(client=client, enabled=True, accept=0.9)
        plan = OC.compile_plan(ONTOLOGY)
        chunk = CC.ExtractionChunk(chunk_id="c0", text=TEXT)
        entities = _pool("c0", [("e1", "Person", "张三"), ("e2", "Organization", "示例单位")])
        pairs = MODULES["relation_resolver"].enumerate_relation_pairs(plan, entities.accepted_entities())
        pairs = [{**pair, "preserve_on_none": True} for pair in pairs]
        grounded, pending = gate.apply_relation_gate(TEXT, pairs, chunk_id="c0", attempts=[])
        self.assertEqual(grounded, [])
        self.assertEqual(len(pending), len(pairs))
        self.assertEqual(gate.snapshot()["relation_dropped"], 0)

    def test_related_to_positive_not_grounded(self) -> None:
        def respond(_qid, question, _state):
            label = next(key for key in question["criteria"] if key != "NONE")
            return {"type": "choice", "choice": label, "confidence": 0.99}

        gate = GATE.ClefGate(client=FakeClef(respond), enabled=True, accept=0.9)
        pair = {
            "pair_id": "p1",
            "source_id": "e1",
            "target_id": "e2",
            "source_name": "张三",
            "target_name": "示例单位",
            "relation_id": "relatedto",
        }
        grounded, pending = gate.apply_relation_gate(TEXT, [pair], chunk_id="c0", attempts=[])
        self.assertEqual(grounded, [])
        self.assertEqual(pending, [pair])

    def test_high_confidence_positive_grounds_quote_without_llm(self) -> None:
        def respond(_qid, question, _state):
            label = next(key for key in question["criteria"] if key != "NONE")
            return {"type": "choice", "choice": label, "confidence": 0.96}

        gate = GATE.ClefGate(client=FakeClef(respond), enabled=True, accept=0.9)
        relations, relation_calls, _llm = _relation_pass(TEXT, gate)
        self.assertEqual(relation_calls, [])
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["from_id"], "e1")
        self.assertEqual(relations[0]["to_id"], "e2")
        self.assertIn("张三", relations[0]["evidence"]["quote"])
        self.assertEqual(gate.snapshot()["relation_grounded_without_llm"], 1)

    def test_wide_evidence_range_stays_on_the_quote_sentence(self) -> None:
        text = "甲单位执行。" + ("填充。" * 200) + "丙单位汇总。"
        entity = types.SimpleNamespace(
            candidate_id="e-bing",
            canonical_type="Organization",
            proposed_type="Organization",
            name="丙单位",
            mentions=[],
            properties={},
            source="",
            evidence=[
                EV.Evidence(
                    span_id="c0:0:1",
                    source_block_ids=["c0"],
                    quote="丙单位",
                    start_char=0,
                    end_char=len(text),
                )
            ],
        )
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        index = RC.build_relation_index(
            [entity],
            CC.ChunkContextManager([chunk]),
            local_previous=0,
            local_next=0,
            same_section=True,
        )
        placed = [
            unit for unit in index.units
            if "e-bing" in index.unit_entities[unit.unit_id]
        ]
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0].text, "丙单位汇总。")
        self.assertNotIn("甲单位", placed[0].text)

    def test_distant_units_in_one_paragraph_are_not_same_paragraph(self) -> None:
        text = "甲单位执行排查。" + ("填充。" * 200) + "丙单位汇总报送。"
        units = [(0, 8), (40, 80), (len(text) - 9, len(text))]
        self.assertEqual(
            GATE.local_context_reason(text, (0, 3), (len(text) - 9, len(text) - 6), units),
            "",
        )
        self.assertEqual(
            GATE.local_context_reason(text, (0, 3), (41, 44), units),
            "adjacent_unit",
        )
        self.assertEqual(
            GATE.local_context_reason(text, (0, 3), (len(text) - 9, len(text) - 6), []),
            "",
        )

    def test_no_cooccurrence_prunes_before_clef_and_llm(self) -> None:
        client = FakeClef(lambda _qid, _question, _state: {
            "type": "choice", "choice": "NONE", "confidence": 0.99,
        })
        gate = GATE.ClefGate(client=client, enabled=True, accept=0.9)
        # 两端名称隔着一段以上，且没有同句 mention → 直接裁掉，不打 Clef/关系 LLM。
        # 同一段或相邻段不再因为规范名不在同一句就被淘汰。
        _relations, relation_calls, _llm = _relation_pass(
            "张三在北京。\n\n中间还有一段说明。\n\n示例单位在上海。",
            gate,
        )
        self.assertEqual(relation_calls, [])
        self.assertEqual(client.calls, [])
        self.assertEqual(_relations, [])

    def test_names_absent_from_chunk_are_pruned(self) -> None:
        client = FakeClef(lambda _qid, _question, _state: {
            "type": "choice", "choice": "NONE", "confidence": 0.99,
        })
        gate = GATE.ClefGate(client=client, enabled=True, accept=0.9)
        _relations, relation_calls, _llm = _relation_pass(
            "只有北京。",
            gate,
            names=(("e1", "Person", "火星人"), ("e2", "Organization", "殖民地")),
        )
        self.assertEqual(relation_calls, [])
        self.assertEqual(client.calls, [])
        self.assertEqual(_relations, [])

    def test_low_confidence_and_service_down_use_llm(self) -> None:
        low = GATE.ClefGate(
            client=FakeClef(lambda _qid, _question, _state: {
                "type": "choice", "choice": "NONE", "confidence": 0.6,
            }),
            enabled=True,
            accept=0.9,
        )
        _relations, low_calls, _llm = _relation_pass(TEXT, low)
        self.assertGreaterEqual(len(low_calls), 1)

        down = GATE.ClefGate(client=ServiceDown(), enabled=True, accept=0.9)
        _relations, down_calls, _llm = _relation_pass(TEXT, down)
        self.assertGreaterEqual(len(down_calls), 1)
        self.assertGreaterEqual(down.snapshot()["unavailable"], 1)

    def test_input_too_long_splits_questions(self) -> None:
        client = TooLongThenOk()
        gate = GATE.ClefGate(client=client, enabled=True, accept=0.9, max_questions=16)
        plan = OC.compile_plan(ONTOLOGY)
        chunk = CC.ExtractionChunk(chunk_id="c0", text=TEXT)
        entities = _pool("c0", [("e1", "Person", "张三"), ("e2", "Organization", "示例单位")])
        pairs = MODULES["relation_resolver"].enumerate_relation_pairs(plan, entities.accepted_entities())
        self.assertGreaterEqual(len(pairs), 1)
        duplicated = pairs + [{**pairs[0], "pair_id": pairs[0]["pair_id"] + "-b"}]
        _grounded, pending = gate.apply_relation_gate(TEXT, duplicated, chunk_id="c0", attempts=[])
        self.assertEqual(pending, [])
        self.assertGreaterEqual(client.calls, 3)

    def test_noul_verification_skips_llm_until_clef_misses(self) -> None:
        chunk = CC.ExtractionChunk(chunk_id="c0", text=TEXT)
        items = [{
            "item_id": "c0:entity[0]",
            "kind": "entity",
            "mention": "张三",
            "evidence_quote": "火星人",
        }]
        client = FakeClef(lambda _qid, _question, _state: {"type": "noul", "noul": 0.05})
        gate = GATE.ClefGate(client=client, enabled=True, accept=0.9)
        llm = StubLLM()
        verdicts = CG.verify_items_with_llm(llm, chunk=chunk, items=items, clef=gate)
        self.assertEqual(verdicts, {"c0:entity[0]": "no"})
        self.assertEqual(llm.calls, [])

        fallback = StubLLM()
        down = GATE.ClefGate(client=ServiceDown(), enabled=True)
        CG.verify_items_with_llm(fallback, chunk=chunk, items=items, clef=down)
        self.assertGreaterEqual(len(fallback.calls), 1)

    def test_disabled_by_default_and_missing_url_is_explicit(self) -> None:
        previous_enabled = os.environ.get("KG_EXTRACT_V22_CLEF_ENABLED")
        previous_url = os.environ.get("CLEF_SERVICE_URL")
        try:
            os.environ.pop("KG_EXTRACT_V22_CLEF_ENABLED", None)
            gate = GATE.ClefGate.from_env()
            self.assertFalse(gate.enabled)
            os.environ["KG_EXTRACT_V22_CLEF_ENABLED"] = "1"
            os.environ.pop("CLEF_SERVICE_URL", None)
            missing = GATE.ClefGate.from_env()
            self.assertFalse(missing.enabled)
            self.assertEqual(missing.disabled_reason, "missing_service_url")
        finally:
            if previous_enabled is None:
                os.environ.pop("KG_EXTRACT_V22_CLEF_ENABLED", None)
            else:
                os.environ["KG_EXTRACT_V22_CLEF_ENABLED"] = previous_enabled
            if previous_url is None:
                os.environ.pop("CLEF_SERVICE_URL", None)
            else:
                os.environ["CLEF_SERVICE_URL"] = previous_url

    def test_extract_split_profile_records_clef(self) -> None:
        chunk = CC.ExtractionChunk(chunk_id="c0", text=TEXT)
        result = SE.extract_split(
            ONTOLOGY,
            chunks=[chunk],
            llm=StubLLM(),
            validation_mode="permissive",
            best_effort=True,
            clef=False,
        )
        self.assertFalse(result["profile"]["clef"]["enabled"])
        self.assertEqual(result["profile"]["clef"]["calls"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
