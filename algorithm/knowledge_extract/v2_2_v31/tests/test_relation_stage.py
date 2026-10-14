"""关系阶段：本体签名、局部证据和小批分类。"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = PACKAGE_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from algorithm.knowledge_extract.v2_2_v31 import candidate_generation as CG
from algorithm.knowledge_extract.v2_2_v31 import chunk_context as CC
from algorithm.knowledge_extract.v2_2_v31 import evidence as EV
from algorithm.knowledge_extract.v2_2_v31 import ontology_compiler as OC
from algorithm.knowledge_extract.v2_2_v31 import prompt_router as PR
from algorithm.knowledge_extract.v2_2_v31.tests.stub_llm import StubLLM


ONTOLOGY = """@startuml
class Person { name: String }
class Organization { name: String }
Person --> Organization : worksAt
Person --> Organization : manages
@enduml
"""

DUTY = """@startuml
class Department { name: String }
class Work { name: String }
Department --> Work : responsibleFor
@enduml
"""


def _entity(chunk_id: str, entity_id: str, type_name: str, name: str, *, source: str = ""):
    return types.SimpleNamespace(
        candidate_id=entity_id,
        canonical_type=type_name,
        proposed_type=type_name,
        name=name,
        mentions=[],
        properties={},
        source=source,
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


class _Pool:
    def __init__(self, entities):
        self._entities = entities

    def accepted_entities(self):
        return self._entities

    def entity_catalog(self):
        return []


def _run(ontology: str, chunks, entities, handler, **kwargs):
    plan = OC.compile_plan(ontology)
    manager = CC.ChunkContextManager(chunks)
    request = CC.ExtractionRequest(chunks=list(chunks))
    llm = StubLLM(relation_handler=handler)
    warnings: list[dict] = []
    results = CG.relation_pass(
        plan,
        manager,
        request,
        llm,
        _Pool(entities),
        route=PR.resolve_document_route(plan, list(chunks)),
        max_workers=1,
        use_scheduler=False,
        attempts=[],
        warnings=warnings,
        local_previous=kwargs.get("local_previous", 1),
        local_next=kwargs.get("local_next", 1),
        same_section=True,
        clef=False,
    )
    relations = [item for _chunk_id, items in results for item in items]
    return relations, warnings, llm


def _support_all(quote: str):
    def handler(_prompt, pairs, _chunk):
        return [
            {"pair_id": pair["pair_id"], "label": pair["relation_id"], "evidence": {"quote": quote}}
            for pair in pairs
        ]
    return handler


def _schema_enums(prompt: str) -> list[str]:
    for index, line in enumerate(prompt.splitlines()):
        if line.strip() == "## output schema":
            schema = json.loads(prompt.splitlines()[index + 1])
            return schema["properties"]["decisions"]["items"]["properties"]["candidate_id"]["enum"]
    return []


class RelationStageTest(unittest.TestCase):
    def test_same_sentence_keeps_each_signature_and_skips_distant_names(self) -> None:
        text = "张三就职于示例单位。\n\n中间还有一段说明。\n\n李四在上海。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [
            _entity("c0", "e1", "Person", "张三"),
            _entity("c0", "e2", "Organization", "示例单位"),
            _entity("c0", "e3", "Person", "李四"),
        ]
        relations, warnings, llm = _run(ONTOLOGY, [chunk], entities, _support_all("张三就职于示例单位。"))
        facts = {(item["from_id"], item["rel_type"], item["to_id"]) for item in relations}
        self.assertEqual(
            facts,
            {("e1", "worksAt", "e2"), ("e1", "manages", "e2")},
        )
        self.assertFalse(any(item["from_id"] == "e3" or item["to_id"] == "e3" for item in relations))
        self.assertEqual([code for code in (warning["code"] for warning in warnings)], [])
        self.assertEqual(len([call for call in llm.calls if call[0] == "relation"]), 1)

    def test_table_row_does_not_link_across_a_gap_row(self) -> None:
        row_a = '{"人":"张三","事":"制动巡检"}'
        row_b = '{"人":"王五","事":"中间事项"}'
        row_c = '{"人":"李四","事":"空压检修"}'
        text = "```jsonl\n" + "\n".join((row_a, row_b, row_c)) + "\n```"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [
            _entity("c0", "p", "Person", "张三"),
            _entity("c0", "org", "Organization", "制动巡检"),
            _entity("c0", "far", "Organization", "空压检修"),
        ]
        # 复用 worksAt：人 -> 组织。空压检修离张三隔了一行，不能成为候选。
        relations, _warnings, _llm = _run(
            ONTOLOGY.split("Person --> Organization : manages")[0] + "@enduml\n",
            [chunk],
            entities,
            _support_all(row_a),
        )
        self.assertEqual(
            [(item["from_id"], item["to_id"]) for item in relations],
            [("p", "org")],
        )

    def test_batch_ceiling_is_per_call(self) -> None:
        names = [f"对象{index:02d}" for index in range(40)]
        text = "总负责安排" + "、".join(names) + "。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [_entity("c0", "src", "Person", "总负责")]
        entities.extend(_entity("c0", f"t{index:02d}", "Organization", name) for index, name in enumerate(names))
        ontology = """@startuml
class Person { name: String }
class Organization { name: String }
Person --> Organization : worksAt
@enduml
"""
        relations, warnings, llm = _run(ontology, [chunk], entities, _support_all(text))
        sizes = [
            len(_schema_enums(prompt))
            for task, prompt in llm.calls
            if task == "relation"
        ]
        self.assertEqual(sorted(sizes), [8, 32])
        self.assertEqual(len(relations), 40)
        self.assertFalse(any(warning["code"] == "RELATION_DECISION_MISSING" for warning in warnings))

    def test_missing_decision_is_not_treated_as_none(self) -> None:
        text = "总负责安排对象00、对象01。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [
            _entity("c0", "src", "Person", "总负责"),
            _entity("c0", "a", "Organization", "对象00"),
            _entity("c0", "b", "Organization", "对象01"),
        ]
        ontology = """@startuml
class Person { name: String }
class Organization { name: String }
Person --> Organization : worksAt
@enduml
"""

        def handler(prompt, pairs, _chunk):
            kept = []
            for pair in pairs:
                line = next(item for item in prompt.splitlines() if f"`{pair['pair_id']}`" in item)
                if "对象00" in line:
                    kept.append({
                        "pair_id": pair["pair_id"],
                        "label": pair["relation_id"],
                        "evidence": {"quote": text},
                    })
            return kept

        relations, warnings, _llm = _run(ontology, [chunk], entities, handler)
        self.assertEqual([(item["from_id"], item["to_id"]) for item in relations], [("src", "a")])
        self.assertIn("RELATION_DECISION_MISSING", {warning["code"] for warning in warnings})

    def test_quote_outside_the_fragment_is_rejected(self) -> None:
        text = "张三就职于示例单位。另有一句无关。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [
            _entity("c0", "e1", "Person", "张三"),
            _entity("c0", "e2", "Organization", "示例单位"),
        ]
        ontology = """@startuml
class Person { name: String }
class Organization { name: String }
Person --> Organization : worksAt
@enduml
"""
        relations, warnings, _llm = _run(
            ontology,
            [chunk],
            entities,
            _support_all("另有一句无关。"),
        )
        self.assertEqual(relations, [])
        self.assertIn("RELATION_EVIDENCE_INVALID", {warning["code"] for warning in warnings})

    def test_adjacent_chunk_and_clause_reference(self) -> None:
        chunks = [
            CC.ExtractionChunk(chunk_id="c0", text="制动巡检由设备部负责。"),
            CC.ExtractionChunk(chunk_id="c1", text="该部门还负责空压检修。"),
        ]
        entities = [
            _entity("c0", "dept", "Department", "设备部"),
            _entity("c0", "work1", "Work", "制动巡检"),
            _entity("c1", "work2", "Work", "空压检修"),
        ]

        def precise(prompt, pairs, _chunk):
            decisions = []
            for pair in pairs:
                line = next(item for item in prompt.splitlines() if f"`{pair['pair_id']}`" in item)
                if "空压检修" in line:
                    quote = "该部门还负责空压检修。"
                else:
                    quote = "制动巡检由设备部负责。"
                decisions.append({
                    "pair_id": pair["pair_id"],
                    "label": pair["relation_id"],
                    "evidence": {"quote": quote},
                })
            return decisions

        relations, _warnings, _llm = _run(DUTY, chunks, entities, precise)
        facts = {(item["from_id"], item["to_id"]) for item in relations}
        self.assertEqual(facts, {("dept", "work1"), ("dept", "work2")})

        cited = "制动巡检每周执行。"
        far_chunks = [
            CC.ExtractionChunk(chunk_id="a", text="设备部的职责见第5.2条。"),
            CC.ExtractionChunk(chunk_id="b", text="中间说明没有条款。"),
            CC.ExtractionChunk(chunk_id="c", text=cited, clause_number="5.2"),
        ]
        far_entities = [
            _entity("a", "dept", "Department", "设备部"),
            _entity("c", "work", "Work", "制动巡检"),
        ]
        far_relations, _far_warnings, _far_llm = _run(
            DUTY,
            far_chunks,
            far_entities,
            _support_all(cited),
        )
        self.assertEqual(
            [(item["from_id"], item["to_id"]) for item in far_relations],
            [("dept", "work")],
        )

    def test_documents_are_not_merged_when_section_is_missing(self) -> None:
        chunks = [
            CC.ExtractionChunk(chunk_id="c1", document_id="d1", text="张三就职于示例单位。"),
            CC.ExtractionChunk(chunk_id="c2", document_id="d2", text="李四就职于另一单位。"),
        ]
        entities = [
            _entity("c1", "p1", "Person", "张三"),
            _entity("c1", "o1", "Organization", "示例单位"),
            _entity("c2", "p2", "Person", "李四"),
            _entity("c2", "o2", "Organization", "另一单位"),
        ]
        relations, _warnings, _llm = _run(
            """@startuml
class Person { name: String }
class Organization { name: String }
Person --> Organization : worksAt
@enduml
""",
            chunks,
            entities,
            lambda _prompt, pairs, _chunk: [
                {
                    "pair_id": pair["pair_id"],
                    "label": pair["relation_id"],
                    "evidence": {
                        "quote": "张三就职于示例单位。" if pair["source_id"] == "p1" else "李四就职于另一单位。",
                    },
                }
                for pair in pairs
            ],
        )
        self.assertEqual(
            {(item["from_id"], item["to_id"]) for item in relations},
            {("p1", "o1"), ("p2", "o2")},
        )

    def test_missing_relation_schema_skips_the_model(self) -> None:
        ontology = """@startuml
class Person { name: String }
@enduml
"""
        chunk = CC.ExtractionChunk(chunk_id="c0", text="张三在北京。")
        relations, warnings, llm = _run(
            ontology,
            [chunk],
            [_entity("c0", "e1", "Person", "张三")],
            _support_all("张三在北京。"),
        )
        self.assertEqual(relations, [])
        self.assertEqual(llm.calls, [])
        self.assertIn("RELATION_NO_SCHEMA", {warning["code"] for warning in warnings})

    def test_related_to_is_not_recalled(self) -> None:
        ontology = """@startuml
class Info { name: String }
Info --> Info : RELATED_TO
@enduml
"""
        text = "反馈表与开源智能体说明写在同一段。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [
            _entity("c0", "a", "Info", "反馈表"),
            _entity("c0", "b", "Info", "开源智能体"),
        ]
        relations, warnings, llm = _run(ontology, [chunk], entities, _support_all(text))
        self.assertEqual(relations, [])
        self.assertEqual(llm.calls, [])
        self.assertIn("RELATION_NO_SCHEMA", {warning["code"] for warning in warnings})

    def test_issued_by_keeps_office_and_skips_recipients(self) -> None:
        ontology = """@startuml
class OfficialDocument { name: String }
class OperationalAgent { name: String }
OfficialDocument --> OperationalAgent : ISSUED_BY
OfficialDocument --> OperationalAgent : APPLIES_TO
@enduml
"""
        text = (
            "中国中车集团有限公司办公室印发通知。"
            "主送中国中车集团有限公司各一级子公司。"
            "联系人：信息公司张振威，电话18600540011。"
        )
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        doc = _entity(
            "c0",
            "doc",
            "OfficialDocument",
            "关于开展排查工作的通知",
            source="document_anchor",
        )
        entities = [
            doc,
            _entity("c0", "office", "OperationalAgent", "中国中车集团有限公司办公室"),
            _entity("c0", "sub", "OperationalAgent", "中国中车集团有限公司各一级子公司"),
            _entity("c0", "contact", "OperationalAgent", "信息公司张振威"),
        ]

        def handler(prompt, pairs, _chunk):
            decisions = []
            for pair in pairs:
                line = next(item for item in prompt.splitlines() if f"`{pair['pair_id']}`" in item)
                if "办公室" in line:
                    quote = "中国中车集团有限公司办公室印发通知。"
                elif "各一级子公司" in line:
                    quote = "主送中国中车集团有限公司各一级子公司。"
                else:
                    quote = "联系人：信息公司张振威，电话18600540011。"
                decisions.append({
                    "pair_id": pair["pair_id"],
                    "label": pair["relation_id"],
                    "evidence": {"quote": quote},
                })
            return decisions

        relations, _warnings, llm = _run(ontology, [chunk], entities, handler)
        facts = {(item["from_id"], item["rel_type"], item["to_id"]) for item in relations}
        self.assertIn(("doc", "ISSUED_BY", "office"), facts)
        self.assertIn(("doc", "APPLIES_TO", "sub"), facts)
        self.assertNotIn(("doc", "ISSUED_BY", "sub"), facts)
        self.assertNotIn(("doc", "ISSUED_BY", "contact"), facts)
        self.assertNotIn(("doc", "APPLIES_TO", "contact"), facts)
        # 联系人句若仍被召回，分类也不应落地；召回阶段通常已跳过。
        self.assertFalse(any(item["to_id"] == "contact" for item in relations))
        self.assertGreaterEqual(len([call for call in llm.calls if call[0] == "relation"]), 1)

    def test_quote_must_cover_both_endpoint_names(self) -> None:
        text = "张三就职于示例单位。另写示例单位要整改。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [
            _entity("c0", "e1", "Person", "张三"),
            _entity("c0", "e2", "Organization", "示例单位"),
        ]
        ontology = """@startuml
class Person { name: String }
class Organization { name: String }
Person --> Organization : worksAt
@enduml
"""
        relations, warnings, _llm = _run(
            ontology,
            [chunk],
            entities,
            _support_all("另写示例单位要整改。"),
        )
        self.assertEqual(relations, [])
        codes = {warning["code"] for warning in warnings}
        self.assertTrue(codes & {"RELATION_EVIDENCE_ENDPOINT_MISS", "RELATION_EVIDENCE_INVALID"})

    def test_short_name_only_quote_is_rejected(self) -> None:
        text = "张三 示例单位。张三就职于示例单位。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [
            _entity("c0", "e1", "Person", "张三"),
            _entity("c0", "e2", "Organization", "示例单位"),
        ]
        ontology = """@startuml
class Person { name: String }
class Organization { name: String }
Person --> Organization : worksAt
@enduml
"""
        # 纯共现：引文只有两端名，无关系表述 → 拒绝。
        rejected, warnings, _llm = _run(
            ontology,
            [chunk],
            entities,
            _support_all("张三 示例单位"),
        )
        self.assertEqual(rejected, [])
        self.assertIn("RELATION_EVIDENCE_NAME_ONLY", {item["code"] for item in warnings})

        # 有关系表述：保留。
        kept, _warnings, _llm = _run(
            ontology,
            [chunk],
            entities,
            _support_all("张三就职于示例单位。"),
        )
        self.assertEqual(
            {(item["from_id"], item["rel_type"], item["to_id"]) for item in kept},
            {("e1", "worksAt", "e2")},
        )

    def test_long_endpoint_name_only_quote_is_kept(self) -> None:
        """长端点（如标题）允许「标题+对象」式引文，避免误伤名单/印发类召回。"""
        title = "关于开展示例专项排查工作的通知文书标题文"  # len>=20，走长端点放行
        target = "总部各部门"
        assert len(title) >= 20
        text = f"{title}\n\n{target}：请落实。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        entities = [
            _entity("c0", "doc", "Document", title),
            _entity("c0", "dept", "Organization", target),
        ]
        ontology = """@startuml
class Document { name: String }
class Organization { name: String }
Document --> Organization : appliesTo
@enduml
"""
        quote = f"{title}\n\n{target}"
        relations, warnings, _llm = _run(
            ontology,
            [chunk],
            entities,
            _support_all(quote),
        )
        self.assertEqual(
            {(item["from_id"], item["rel_type"], item["to_id"]) for item in relations},
            {("doc", "appliesTo", "dept")},
        )
        self.assertNotIn("RELATION_EVIDENCE_NAME_ONLY", {item["code"] for item in warnings})


if __name__ == "__main__":
    unittest.main()
