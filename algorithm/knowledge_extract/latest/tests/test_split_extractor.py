"""Phase 1 Step 4 单元测试：Entity / Relation 拆分（Two-Pass）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v3/tests/test_split_extractor.py

覆盖（重构后协议）：
- prompt 契约：实体 pass 只给类型目录、禁关系；关系 pass 为候选 pair 分类；
- §20 实体解析/去重（跨 chunk 合并属性与证据）；
- §19 + AC-7 关系校验（CandidatePool/RelationResolver：端点存在、签名匹配、未知类型）；
- §25/§26/§27 O1/O2 组装与 UID 稳定性；
- AC-2 范围纪律、失败归因（JSON/请求）、空输入；
- benchmark gold 冒烟（mention 级实体全覆盖、证据 100%）。

注：重构后实体名 = 原文 mention（span-first），gold 中不在原文的编号类名称
（CR-001 等）不再可产出，相关断言按 mention 重新基线化。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PACKAGE_DIR / "tests" / "fixtures"
ONTOLOGIES_DIR = FIXTURES_DIR / "ontologies"
CASES_DIR = FIXTURES_DIR / "cases"
TESTS_DIR = PACKAGE_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    # pytest 全量运行时多个测试文件共享 sys.modules：已加载的模块直接复用，
    # 避免同名模块被重复 exec 出多个类对象（isinstance/类型一致性污染）。
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
    "knowledge_extract_latest",
    PACKAGE_DIR,
    [
        "config",
        "scheduler",
        "graph_utils",
        "ontology_schema",
        "ontology_compiler",
        "chunk_context",
        "evidence",
        "verification",
        "graph_assembler",
        "prompt_router",
        "relation_resolver",
        "candidate_generation",
        "property_enrichment",
        "split_extractor",
    ],
)
SE = MODULES["split_extractor"]
CC = MODULES["chunk_context"]
GA = MODULES["graph_assembler"]
GU = MODULES["graph_utils"]
PR = MODULES["prompt_router"]
RR = MODULES["relation_resolver"]
CG = MODULES["candidate_generation"]
PE = MODULES["property_enrichment"]
V = MODULES["verification"]

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # 允许独立运行（python tests/test_split_extractor.py）

from algorithm.knowledge_extract.latest.tests.stub_llm import StubLLM, parse_known_entities, span_item  # noqa: E402


RULE_ONTOLOGY = """@startuml rule-ontology
class "规则" as Rule <<KnowledgeUnit>> {
  + 规则编号 (ruleId): String // 规则唯一编号
  + 规则内容 (ruleText): String // 规则内容原文
}
class "简单规则" as SimpleRule <<KnowledgeUnit>> {
  + 规则编号 (ruleId): String // 规则唯一编号
  + 规则内容 (ruleText): String // 规则内容原文
  + 触发条件 (triggerCondition): String // 触发条件
  + 动作 (action): String // 执行动作
}
class "复合规则" as CompositeRule <<KnowledgeUnit>> {
  + 规则编号 (ruleId): String // 规则唯一编号
  + 规则内容 (ruleText): String // 规则内容原文
  + 组合类型 (combinationType): String // AND / OR / SEQUENCE
}
class "对象" as Object <<KnowledgeUnit>> {
  + 对象名称 (objectName): String // 对象名称
}
Rule <|-- SimpleRule
Rule <|-- CompositeRule
CompositeRule "1" --> "1..*" SimpleRule : hasSubRule
SimpleRule "1" --> "0..*" Object : constrainsObject
' @identity Rule: ruleId
' @identity SimpleRule: ruleId
' @identity CompositeRule: ruleId
' @identity Object: objectName
@enduml
"""

COMPOSITE_TEXT = "未经本企业充分论证和安全测试，未经集团批准，不得部署安装各类开源智能体。"
MEMBER_MENTIONS = ["未经本企业充分论证和安全测试", "未经集团批准"]
MEMBER_RULE_TEXTS = [
    "未经本企业充分论证和安全测试，不得部署安装各类开源智能体。",
    "未经集团批准，不得部署安装各类开源智能体。",
]


def composite_entity_handler(prompt: str, chunk: dict) -> list[dict]:
    return [
        span_item("CompositeRule", COMPOSITE_TEXT),
        span_item("SimpleRule", MEMBER_MENTIONS[0]),
        span_item("SimpleRule", MEMBER_MENTIONS[1]),
    ]


def composite_property_handler(prompt: str, chunk: dict) -> dict:
    updates = []
    for row in parse_known_entities(prompt):
        if row["type"] == "CompositeRule":
            properties = {"ruleText": COMPOSITE_TEXT, "combinationType": "AND"}
        else:
            index = MEMBER_MENTIONS.index(row["name"].strip())
            properties = {
                "ruleText": MEMBER_RULE_TEXTS[index],
                "triggerCondition": MEMBER_MENTIONS[index],
                "action": "不得部署安装各类开源智能体",
            }
        updates.append(
            {
                "entity_id": row["id"],
                "properties": properties,
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        )
    return {"updates": updates}


def composite_relation_handler(prompt: str, pairs: list[dict], chunk: dict) -> list[dict]:
    return [
        {"pair_id": pair["pair_id"], "label": pair["relation_id"], "evidence": {"quote": COMPOSITE_TEXT}}
        for pair in pairs
        if pair["relation_id"] == "hassubrule"
    ]


class PromptTest(unittest.TestCase):
    def test_entity_prompt_scope_and_features_no_relations(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY, target_types=["Rule"])
        chunk = CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)
        route = PR.resolve_document_route(plan, [chunk])
        messages = PR.build_chat_messages(task="entity", plan=plan, route=route, chunk=chunk)
        prompt = messages[1]["content"]
        self.assertIn("只提议实体候选，禁止输出关系", prompt)
        self.assertIn("`SimpleRule`", prompt)  # 父类子类展开进类型目录
        self.assertIn("`CompositeRule`", prompt)
        self.assertIn("（实体任务不展示关系目录）", prompt)
        self.assertNotIn("hasSubRule", prompt)  # 实体 pass 不提供关系

    def test_relation_prompt_catalog_and_signatures(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        chunk = CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)
        route = PR.resolve_document_route(plan, [chunk])
        entities = [
            {"id": "entity-0", "type": "CompositeRule", "name": "CR-001"},
            {"id": "entity-1", "type": "SimpleRule", "name": "R-001"},
        ]
        pairs = RR.enumerate_relation_pairs(plan, entities)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["relation_id"], "hassubrule")
        messages = PR.build_chat_messages(
            task="relation",
            plan=plan,
            route=route,
            chunk=chunk,
            known_entities=entities,
            output_schema=CG.relation_pair_decision_schema(pairs),
            relation_pairs=pairs,
        )
        prompt = messages[1]["content"]
        self.assertIn(pairs[0]["pair_id"], prompt)
        self.assertIn("entity-0", prompt)
        self.assertIn("CR-001", prompt)
        self.assertIn("R-001", prompt)
        self.assertIn("hassubrule", prompt)
        self.assertIn("只分类给定 relation pair；不得创建新端点、不得输出未列出的关系", prompt)
        self.assertIn("禁止输出空 decisions", prompt)
        self.assertNotIn('没有结果时输出 {"decisions": {}}', prompt)
        self.assertIn("current chunk.text 或 local context", prompt)
        decisions_schema = CG.relation_pair_decision_schema(pairs)["properties"]["decisions"]
        self.assertEqual(decisions_schema["type"], "object")
        self.assertEqual(decisions_schema["required"], [pairs[0]["pair_id"]])
        self.assertIn(pairs[0]["pair_id"], decisions_schema["properties"])
        self.assertIn(
            "ABSTAIN",
            decisions_schema["properties"][pairs[0]["pair_id"]]["properties"]["label"]["enum"],
        )

    def test_relation_decision_schema_rejects_missing_pair(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        pairs = RR.enumerate_relation_pairs(
            plan,
            [
                {"id": "entity-0", "type": "CompositeRule", "name": "复合规则"},
                {"id": "entity-1", "type": "SimpleRule", "name": "规则一"},
                {"id": "entity-2", "type": "SimpleRule", "name": "规则二"},
            ],
        )
        schema = CG.relation_pair_decision_schema(pairs)
        first = pairs[0]
        incomplete = {
            "decisions": {
                first["pair_id"]: {
                    "label": "NONE",
                    "evidence": {"quote": ""},
                }
            }
        }
        errors = CG.validate_json_schema(incomplete, schema)
        self.assertTrue(errors)
        self.assertTrue(any("missing required property" in error for error in errors))


class TwoPassPipelineTest(unittest.TestCase):
    def test_relation_abstain_is_observable_and_skips_semantic_verifier(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        text = "复合规则可能与规则一有关，但原文关系不明确。"
        for type_id, name in (("CompositeRule", "复合规则"), ("SimpleRule", "规则一")):
            start = text.index(name)
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[SE.Evidence(
                    span_id=f"c1:{start}:{start + len(name)}",
                    source_block_ids=["c1"],
                    quote=name,
                    start_char=start,
                    end_char=start + len(name),
                )],
                source="test",
            ))
        llm = StubLLM(
            relation_handler=lambda prompt, pairs, chunk: [
                {
                    "pair_id": pair["pair_id"],
                    "label": "ABSTAIN",
                    "evidence": {"quote": ""},
                }
                for pair in pairs
            ],
        )
        chunk = CC.ExtractionChunk(chunk_id="c1", text=text)
        warnings: list[dict] = []
        results = CG.relation_pass(
            plan,
            CC.ChunkContextManager([chunk]),
            CC.ExtractionRequest(chunks=[chunk]),
            llm,
            pool,
            route=PR.resolve_document_route(plan, [chunk]),
            max_workers=1,
            use_scheduler=False,
            attempts=[],
            warnings=warnings,
            local_previous=0,
            local_next=0,
            same_section=True,
            max_attempts=1,
            retry_base_seconds=0,
        )
        self.assertEqual(results, [("c1", [])])
        self.assertEqual([task for task, _ in llm.calls], ["relation"])
        self.assertIn("RELATION_PAIR_ABSTAINED", {item["code"] for item in warnings})

    def test_relation_pass_extracts_pair_across_adjacent_chunks(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        chunks = [
            CC.ExtractionChunk(
                chunk_id="c1",
                document_id="doc-1",
                section_id="s1",
                text="复合规则如下。",
            ),
            CC.ExtractionChunk(
                chunk_id="c2",
                document_id="doc-1",
                section_id="s1",
                text="该复合规则包含规则一。",
            ),
        ]
        for type_id, name, chunk_id, text in (
            ("CompositeRule", "复合规则", "c1", chunks[0].text),
            ("SimpleRule", "规则一", "c2", chunks[1].text),
        ):
            start = text.index(name)
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[SE.Evidence(
                    span_id=f"{chunk_id}:{start}:{start + len(name)}",
                    source_block_ids=[chunk_id],
                    quote=name,
                    start_char=start,
                    end_char=start + len(name),
                )],
                source="test",
            ))
        relation_quote = chunks[1].text
        llm = StubLLM(
            relation_handler=lambda prompt, pairs, chunk: [
                {
                    "pair_id": pair["pair_id"],
                    "label": pair["relation_id"],
                    "evidence": {"quote": relation_quote},
                }
                for pair in pairs
            ],
        )
        manager = CC.ChunkContextManager(chunks)
        results = CG.relation_pass(
            plan,
            manager,
            CC.ExtractionRequest(chunks=chunks),
            llm,
            pool,
            route=PR.resolve_document_route(plan, chunks),
            max_workers=1,
            use_scheduler=False,
            attempts=[],
            warnings=[],
            local_previous=1,
            local_next=1,
            same_section=True,
            max_attempts=1,
            retry_base_seconds=0,
        )
        relations = [relation for _, rows in results for relation in rows]
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["evidence"]["source_block_ids"], ["c2"])
        self.assertEqual([task for task, _ in llm.calls], [
            "relation",
            "evidence_verification",
        ])

    def test_relation_window_does_not_mix_known_and_unknown_documents(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        chunks = [
            CC.ExtractionChunk(
                chunk_id="known",
                document_id="doc-a",
                section_id="s1",
                text="复合规则如下。",
            ),
            CC.ExtractionChunk(
                chunk_id="unknown",
                document_id=None,
                section_id="s1",
                text="规则一属于另一份未标注文档。",
            ),
        ]
        for type_id, name, chunk in (
            ("CompositeRule", "复合规则", chunks[0]),
            ("SimpleRule", "规则一", chunks[1]),
        ):
            start = chunk.text.index(name)
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[SE.Evidence(
                    span_id=f"{chunk.chunk_id}:{start}:{start + len(name)}",
                    source_block_ids=[chunk.chunk_id],
                    quote=name,
                    start_char=start,
                    end_char=start + len(name),
                )],
                source="test",
            ))
        llm = StubLLM(
            relation_handler=lambda prompt, pairs, chunk: [
                {
                    "pair_id": pair["pair_id"],
                    "label": pair["relation_id"],
                    "evidence": {"quote": chunk["text"]},
                }
                for pair in pairs
            ],
        )
        results = CG.relation_pass(
            plan,
            CC.ChunkContextManager(chunks),
            CC.ExtractionRequest(chunks=chunks),
            llm,
            pool,
            route=PR.resolve_document_route(plan, chunks),
            max_workers=1,
            use_scheduler=False,
            attempts=[],
            warnings=[],
            local_previous=1,
            local_next=1,
            same_section=True,
            max_attempts=1,
            retry_base_seconds=0,
        )
        self.assertEqual(results, [("known", []), ("unknown", [])])
        self.assertEqual(llm.calls, [])

    def test_relation_semantic_verification_is_batched_and_missing_is_terminal(self) -> None:
        text = "源实体约束目标实体。"
        relations = [
            {
                "from_id": "source",
                "to_id": f"target-{index}",
                "from_name": "源实体",
                "to_name": "目标实体",
                "rel_type": "constrains",
                "evidence": {"quote": text},
            }
            for index in range(CG.RELATION_VERIFICATION_BATCH_SIZE + 1)
        ]
        llm = StubLLM(
            evidence_handler=lambda prompt, chunk, items: {
                "verdicts": [
                    {
                        "item_id": items[0]["item_id"],
                        "supported": "yes",
                        "reason": "仅返回本批第一项，用于模拟漏答",
                    }
                ]
            },
        )
        attempts: list[dict] = []
        warnings: list[dict] = []
        accepted = CG.verify_relation_claims(
            llm,
            chunk=CC.ExtractionChunk(chunk_id="c1", text=text),
            relations=relations,
            attempts=attempts,
            warnings=warnings,
            max_attempts=1,
            retry_base_seconds=0,
        )
        self.assertEqual(len(accepted), 2)
        self.assertEqual([task for task, _ in llm.calls], [
            "evidence_verification",
            "evidence_verification",
        ])
        self.assertEqual(
            sum(attempt.get("terminal") is True for attempt in attempts),
            1,
        )
        self.assertIn(
            "RELATION_EVIDENCE_VERIFIER_INCOMPLETE",
            {item["code"] for item in warnings},
        )

    def test_relation_semantic_verifier_rejects_duplicate_and_unknown_ids(self) -> None:
        relation = {
            "from_id": "source",
            "to_id": "target",
            "rel_type": "constrains",
            "evidence": {"quote": "源实体约束目标实体。"},
        }
        llm = StubLLM(
            evidence_handler=lambda prompt, chunk, items: {
                "verdicts": [
                    {"item_id": items[0]["item_id"], "supported": "yes", "reason": "first"},
                    {"item_id": items[0]["item_id"], "supported": "yes", "reason": "duplicate"},
                    {"item_id": "relation-unknown", "supported": "yes", "reason": "unknown"},
                ]
            },
        )
        attempts: list[dict] = []
        accepted = CG.verify_relation_claims(
            llm,
            chunk=CC.ExtractionChunk(chunk_id="c1", text="源实体约束目标实体。"),
            relations=[relation],
            attempts=attempts,
            warnings=[],
            max_attempts=1,
            retry_base_seconds=0,
        )
        self.assertEqual(accepted, [])
        details = attempts[-1]["incomplete_details"]
        self.assertEqual(details["duplicate_item_ids"], ["relation-0"])
        self.assertEqual(details["unknown_item_ids"], ["relation-unknown"])

    def test_relation_requires_explicit_semantic_evidence_verdict(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        text = "复合规则包含规则一。"
        for type_id, name in (("CompositeRule", "复合规则"), ("SimpleRule", "规则一")):
            start = text.index(name)
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[SE.Evidence(
                    span_id=f"c1:{start}:{start + len(name)}",
                    source_block_ids=["c1"],
                    quote=name,
                    start_char=start,
                    end_char=start + len(name),
                )],
                source="test",
            ))

        llm = StubLLM(
            relation_handler=lambda prompt, pairs, chunk: [
                {
                    "pair_id": pair["pair_id"],
                    "label": pair["relation_id"],
                    "evidence": {"quote": text},
                }
                for pair in pairs
            ],
            evidence_handler=lambda prompt, chunk, items: {
                "verdicts": [
                    {
                        "item_id": item["item_id"],
                        "supported": "no",
                        "reason": "关系方向或语义不受原文支持",
                    }
                    for item in items
                ]
            },
        )
        chunk = CC.ExtractionChunk(chunk_id="c1", text=text)
        warnings: list[dict] = []
        results = CG.relation_pass(
            plan,
            CC.ChunkContextManager([chunk]),
            CC.ExtractionRequest(chunks=[chunk]),
            llm,
            pool,
            route=PR.resolve_document_route(plan, [chunk]),
            max_workers=1,
            use_scheduler=False,
            attempts=[],
            warnings=warnings,
            local_previous=0,
            local_next=0,
            same_section=True,
            max_attempts=1,
            retry_base_seconds=0,
        )
        self.assertEqual(results, [("c1", [])])
        self.assertEqual([task for task, _ in llm.calls], ["relation", "evidence_verification"])
        self.assertIn(
            "RELATION_SEMANTIC_EVIDENCE_REJECTED",
            {item["code"] for item in warnings},
        )

    def test_relation_batch_failure_is_bisected_instead_of_dropped(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        text = "复合规则包含规则一和规则二。"
        for type_id, name in (
            ("CompositeRule", "复合规则"),
            ("SimpleRule", "规则一"),
            ("SimpleRule", "规则二"),
        ):
            start = text.index(name)
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[SE.Evidence(
                    span_id=f"c1:{start}:{start + len(name)}",
                    source_block_ids=["c1"],
                    quote=name,
                    start_char=start,
                    end_char=start + len(name),
                )],
                source="test",
            ))

        class BatchSensitiveLLM:
            def __init__(self) -> None:
                self.batch_sizes: list[int] = []

            def generate_structured(self, output_schema, messages, context):
                pair_ids = output_schema["properties"]["decisions"]["required"]
                self.batch_sizes.append(len(pair_ids))
                if len(pair_ids) > 1:
                    return json.dumps({"decisions": {}})
                return json.dumps({
                    "decisions": {
                        pair_ids[0]: {"label": "NONE", "evidence": {"quote": ""}}
                    }
                })

        chunk = CC.ExtractionChunk(chunk_id="c1", text=text)
        manager = CC.ChunkContextManager([chunk])
        request = CC.ExtractionRequest(chunks=[chunk])
        llm = BatchSensitiveLLM()
        warnings: list[dict] = []
        attempts: list[dict] = []
        results = CG.relation_pass(
            plan,
            manager,
            request,
            llm,
            pool,
            route=PR.resolve_document_route(plan, [chunk]),
            max_workers=1,
            use_scheduler=False,
            attempts=attempts,
            warnings=warnings,
            local_previous=0,
            local_next=0,
            same_section=True,
            max_attempts=1,
            retry_base_seconds=0,
        )
        self.assertEqual(results, [("c1", [])])
        self.assertEqual(llm.batch_sizes, [2, 1, 1])
        self.assertTrue(attempts)
        self.assertTrue(all(attempt.get("terminal") is False for attempt in attempts))
        self.assertTrue(all(attempt.get("recovered_by") == "relation_batch_split" for attempt in attempts))
        self.assertIn("RELATION_BATCH_SPLIT_RETRY", {item["code"] for item in warnings})
        self.assertNotIn("RELATION_PAIR_UNDECIDED", {item["code"] for item in warnings})

    def test_permanent_relation_provider_failure_is_not_bisected(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        text = "复合规则包含规则一和规则二。"
        for type_id, name in (
            ("CompositeRule", "复合规则"),
            ("SimpleRule", "规则一"),
            ("SimpleRule", "规则二"),
        ):
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[],
                source="test",
            ))

        class PermanentProviderError(RuntimeError):
            status_code = 400

        class FailingLLM:
            def __init__(self) -> None:
                self.calls = 0

            def generate_structured(self, output_schema, messages, context):
                self.calls += 1
                raise PermanentProviderError("unsupported relation schema")

        chunk = CC.ExtractionChunk(chunk_id="c1", text=text)
        manager = CC.ChunkContextManager([chunk])
        llm = FailingLLM()
        warnings: list[dict] = []
        CG.relation_pass(
            plan,
            manager,
            CC.ExtractionRequest(chunks=[chunk]),
            llm,
            pool,
            route=PR.resolve_document_route(plan, [chunk]),
            max_workers=1,
            use_scheduler=False,
            attempts=[],
            warnings=warnings,
            local_previous=0,
            local_next=0,
            same_section=True,
            max_attempts=3,
            retry_base_seconds=0,
        )
        self.assertEqual(llm.calls, 1)
        self.assertIn("RELATION_BATCH_UNDECIDED", {item["code"] for item in warnings})
        self.assertNotIn("RELATION_BATCH_SPLIT_RETRY", {item["code"] for item in warnings})

    def test_end_to_end_composite_rule(self) -> None:
        llm = StubLLM(
            entity_handler=composite_entity_handler,
            property_handler=composite_property_handler,
            relation_handler=composite_relation_handler,
        )
        result = SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)],
            llm=llm,
            identity_admission="observe",
        )
        raw = result["raw"]
        self.assertEqual(
            [entity["type"] for entity in raw["entities"]],
            ["CompositeRule", "SimpleRule", "SimpleRule"],
        )
        self.assertEqual(
            [entity["name"] for entity in raw["entities"]],
            [COMPOSITE_TEXT, *MEMBER_MENTIONS],
        )
        self.assertEqual(len(raw["relations"]), 2)
        for relation in raw["relations"]:
            self.assertEqual(relation["rel_type"], "hasSubRule")
            self.assertEqual(relation["from_type"], "CompositeRule")
            self.assertEqual(relation["to_type"], "SimpleRule")
            self.assertIn("span_id", relation["evidence"])
            self.assertEqual(relation["evidence"]["source_block_ids"], ["chunk-0"])
        # AC-7：无悬空关系
        self.assertEqual(len(result["graph"]["nodes"]), 3)
        self.assertEqual(len(result["graph"]["relationships"]), 2)
        self.assertEqual(len(result["triples"]), 2)
        # O1
        model = result["intermediate_model"]
        self.assertEqual(len(model["objects"]), 3)
        self.assertEqual(len(model["relations"]), 2)
        self.assertEqual(model["objects"][0]["mapping"]["status"], "accepted")
        self.assertEqual(model["objects"][0]["identity"], COMPOSITE_TEXT)
        # 证据覆盖
        self.assertEqual(result["evidence_coverage"]["entities"]["rate"], 1.0)
        self.assertEqual(result["evidence_coverage"]["relations"]["rate"], 1.0)
        quality = result["profile"]["resolution_quality"]
        self.assertEqual(quality["candidate_rejection_rate"], 0.0)
        self.assertEqual(quality["evidence_rejection_count"], 0)
        self.assertEqual(quality["identity_admission"], "observe")
        # 属性按兼容的 type + missing-property 目录分批，必要时做语义核验。
        tasks = [task for task, _ in llm.calls]
        self.assertEqual(tasks[0], "entity")
        self.assertEqual(tasks[-2:], ["relation", "evidence_verification"])
        # 覆盖率兜底会对本轮完全没补到属性的实体再跑一轮，因此次数上不封顶为 2。
        self.assertGreaterEqual(tasks.count("identity"), 2)
        self.assertGreaterEqual(tasks.count("property"), 2)
        self.assertIn("evidence_verification", tasks)
        self.assertNotIn("PROPERTY_EVIDENCE_UNVERIFIED", {w["code"] for w in result["warnings"]})
        self.assertIsNone(result["error_code"])

    def test_default_rule_id_gap_keeps_unresolved_entities_and_relations(self) -> None:
        llm = StubLLM(
            entity_handler=composite_entity_handler,
            property_handler=composite_property_handler,
            relation_handler=composite_relation_handler,
        )
        result = SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)],
            llm=llm,
        )
        self.assertEqual(len(result["raw"]["entities"]), 3)
        self.assertEqual(len(result["raw"]["relations"]), 2)
        self.assertEqual(len(result["graph"]["nodes"]), 3)
        self.assertEqual(len(result["graph"]["relationships"]), 2)
        self.assertEqual(len(result["intermediate_model"]["objects"]), 3)
        self.assertEqual(len(result["intermediate_model"]["relations"]), 2)
        # 新契约：本体声明的字段全部保留，缺失值用空字符串占位（不得填默认编号）。
        self.assertTrue(
            all(not entity["properties"].get("规则编号") for entity in result["raw"]["entities"])
        )
        self.assertTrue(all("ruleId" not in obj["properties"] for obj in result["intermediate_model"]["objects"]))
        self.assertEqual(result["profile"]["resolution_quality"]["unresolved_count"], 3)
        self.assertEqual(result["profile"]["resolution_quality"]["quarantined_count"], 0)
        self.assertEqual(result["profile"]["resolution_quality"]["identity_admission"], "quarantine")
        self.assertIn(
            "RULE_ID_MISSING",
            {warning["code"] for warning in result["warnings"]},
        )

    def test_actual_ruleid_display_identifier_alias_keeps_rule_output(self) -> None:
        # This is the production dialect: identifier=规则ID, display name=RuleID.
        ontology = """@startuml rule_ontology
class "规则" <<RuleType>> {
  - RuleID (规则ID) //规则的唯一标识符
  - RuleName (RuleName) //规则名称
}
' @identity RuleType: RuleID
@enduml
"""
        text = "该规则要求设备停机。"
        llm = StubLLM(entity_handler=lambda _prompt, _chunk: [span_item("RuleType", "该规则")])

        result = SE.extract_split(
            ontology,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=text)],
            llm=llm,
        )

        self.assertEqual(len(result["raw"]["entities"]), 1)
        self.assertEqual(len(result["graph"]["nodes"]), 1)
        self.assertEqual(len(result["intermediate_model"]["objects"]), 1)
        self.assertEqual(result["profile"]["resolution_quality"]["quarantined_count"], 0)
        self.assertEqual(result["profile"]["resolution_quality"]["unresolved_count"], 1)
        self.assertIn("RULE_ID_MISSING", {warning["code"] for warning in result["warnings"]})
        self.assertNotIn("ENTITY_IDENTITY_QUARANTINED", {warning["code"] for warning in result["warnings"]})
        # 新契约：字段存在但值为空；identifier 统一映射为 display name。
        self.assertFalse(result["raw"]["entities"][0]["properties"].get("RuleID"))
        self.assertNotIn("规则ID", result["raw"]["entities"][0]["properties"])

    def test_entity_dedup_merge_across_chunks(self) -> None:
        terminology_ontology = """@startuml terminology
class "术语" as Terminology <<KnowledgeUnit>> {
  + 术语标准名称 (TermName): String // 术语的标准名称
  + 术语标准定义 (TermDefinition): String // 术语的标准业务定义
  + 同义词 (Synonyms): String[] // 简称、别称或口语化表达
}
' @identity Terminology: TermName
@enduml
"""
        chunks = [
            CC.ExtractionChunk(chunk_id="c1", text="开展关键主数据治理（也称主数据管理），建立健全主数据识别、采集、校验机制。"),
            CC.ExtractionChunk(chunk_id="c2", text="主数据治理是数据治理的核心。"),
        ]

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            if chunk.get("chunk_id") == "c2":
                return [span_item("Terminology", "主数据治理", quote="主数据治理是数据治理的核心")]
            return [span_item("Terminology", "主数据治理", quote="开展关键主数据治理（也称主数据管理）")]

        def property_handler(prompt: str, chunk: dict) -> dict:
            if chunk.get("chunk_id") != "c1":
                return {"updates": []}
            return {
                "updates": [
                    {
                        "entity_id": row["id"],
                        "properties": {
                            "TermDefinition": "对主数据的识别、采集、校验等治理活动",
                            "Synonyms": ["主数据管理"],
                        },
                        "evidence": {"quote": "开展关键主数据治理（也称主数据管理）"},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        def identity_handler(prompt: str, chunk: dict) -> dict:
            return {
                "updates": [
                    {
                        "entity_id": row["id"],
                        "properties": {"TermName": "主数据治理"},
                        "evidence": {"quote": "主数据治理"},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        llm = StubLLM(
            entity_handler=entity_handler,
            identity_handler=identity_handler,
            property_handler=property_handler,
        )
        result = SE.extract_split(
            terminology_ontology,
            chunks=chunks,
            llm=llm,
        )
        entities = result["raw"]["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["candidate_id"], "entity-0")
        properties = result["intermediate_model"]["objects"][0]["properties"]
        self.assertEqual(properties["TermDefinition"], "对主数据的识别、采集、校验等治理活动")
        self.assertEqual(properties["Synonyms"], ["主数据管理"])
        # 两条 evidence（不同 chunk span）都保留
        self.assertEqual(len(result["intermediate_model"]["objects"][0]["evidence"]), 2)

    def test_out_of_scope_entity_rejected(self) -> None:
        # 重构后 entity pass 的 output schema enum 已限定在目标范围内，范围外类型
        # 在结构化校验阶段就会被拒；ENTITY_OUT_OF_SCOPE 的兜底防线在 CandidatePool。
        official = (ONTOLOGIES_DIR / "official_document_ontology.puml").read_text(encoding="utf-8")
        schema = MODULES["ontology_schema"].OntologySchema.parse(official)
        plan = SE.compile_plan(official, target_types=["OperationalActivity"])
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        accepted = pool.add_entity(
            SE.EntityCandidate(
                candidate_id="",
                proposed_type="OperationalActivity",
                canonical_type="OperationalActivity",
                name="加强网络和数据安全监测",
                properties={},
                evidence=[],
                source="test",
            )
        )
        self.assertIsNotNone(accepted)
        warnings: list[dict] = []
        rejected = pool.add_entity(
            SE.EntityCandidate(
                candidate_id="",
                proposed_type="OperationalAgent",
                canonical_type="OperationalAgent",
                name="集团",
                properties={},
                evidence=[],
                source="test",
            ),
            warnings=warnings,
        )
        self.assertIsNone(rejected)
        codes = [warning["code"] for warning in warnings]
        self.assertIn("ENTITY_OUT_OF_SCOPE", codes)
        self.assertEqual(
            [entity.canonical_type for entity in pool.accepted_entities()],
            ["OperationalActivity"],
        )


class ClosedWorldPropertyEnrichmentTest(unittest.TestCase):
    ONTOLOGY = """@startuml property
class "设备" as Device {
  + 设备编号 (deviceId): String
  + 用途说明 (description): String
}
' @identity Device: deviceId
@enduml
"""

    def setUp(self) -> None:
        self.plan = SE.compile_plan(self.ONTOLOGY)
        self.schema = MODULES["ontology_schema"].OntologySchema.parse(self.ONTOLOGY)
        self.chunk = CC.ExtractionChunk(
            chunk_id="c1",
            text="设备A用于每日检查轨道状态，并记异常。",
        )
        self.manager = CC.ChunkContextManager([self.chunk])
        self.route = PR.DocumentRoute(kind="generic", source="fallback")

    def _pool(self, *, anchored: bool = True):
        pool = SE.CandidatePool(self.schema, identity_admission="observe")
        evidence = []
        if anchored:
            evidence = [
                SE.Evidence(
                    span_id="c1:0:3",
                    source_block_ids=["c1"],
                    quote="设备A",
                    start_char=0,
                    end_char=3,
                )
            ]
        entity = SE.EntityCandidate(
            candidate_id="",
            proposed_type="Device",
            canonical_type="Device",
            name="设备A",
            properties={"deviceId": "D-001"},
            evidence=evidence,
            source="llm",
        )
        accepted = pool.add_entity(entity)
        self.assertIsNotNone(accepted)
        return pool, accepted

    def _run(self, pool, llm):
        warnings: list[dict] = []
        summary = PE.enrich_entity_properties(
            pool,
            self.manager,
            self.plan,
            llm,
            route=self.route,
            attempts=[],
            warnings=warnings,
            max_attempts=1,
            retry_base_seconds=0,
        )
        return summary, warnings

    def test_identity_property_is_not_completable(self) -> None:
        _, entity = self._pool()
        entity.properties.clear()
        self.assertEqual(set(PE._declared_properties(self.plan, entity)), {"description"})

    def test_semantic_paraphrase_requires_explicit_yes(self) -> None:
        pool, entity = self._pool()
        frozen_before = SE._frozen_entity_fingerprint(pool, self.plan)

        def property_handler(prompt: str, chunk: dict) -> dict:
            return {
                "updates": [
                    {
                        "entity_id": row["id"],
                        "properties": {"description": "承担线路设施例行巡检与故障登记"},
                        "evidence": {"quote": "设备A用于每日检查轨道状态，并记异常"},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        summary, _ = self._run(pool, StubLLM(property_handler=property_handler))
        self.assertEqual(entity.properties["description"], "承担线路设施例行巡检与故障登记")
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(summary["verifications"], 1)
        self.assertEqual(len(entity.property_evidence["description"]), 1)
        self.assertEqual(SE._frozen_entity_fingerprint(pool, self.plan), frozen_before)

    def test_uncertain_semantic_verdict_is_rejected(self) -> None:
        pool, entity = self._pool()

        def property_handler(prompt: str, chunk: dict) -> dict:
            return {
                "updates": [
                    {
                        "entity_id": row["id"],
                        "properties": {"description": "主要用于生产排程"},
                        "evidence": {"quote": "设备A用于每日检查轨道状态"},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        def uncertain(prompt: str, chunk: dict, items: list[dict]) -> dict:
            return {
                "verdicts": [
                    {"item_id": item["item_id"], "supported": "uncertain", "reason": "not enough context"}
                    for item in items
                ]
            }

        summary, warnings = self._run(
            pool,
            StubLLM(property_handler=property_handler, evidence_handler=uncertain),
        )
        self.assertNotIn("description", entity.properties)
        self.assertEqual(summary["applied"], 0)
        self.assertEqual(summary["rejected"], 1)
        self.assertIn("PROPERTY_EVIDENCE_UNVERIFIED", {row["code"] for row in warnings})

    def test_conflicting_duplicate_verdicts_fail_closed(self) -> None:
        pool, entity = self._pool()

        def property_handler(prompt: str, chunk: dict) -> dict:
            return {
                "updates": [
                    {
                        "entity_id": row["id"],
                        "properties": {"description": "每日检查轨道状态"},
                        "evidence": {"quote": "设备A用于每日检查轨道状态"},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        def contradictory(prompt: str, chunk: dict, items: list[dict]) -> dict:
            item_id = items[0]["item_id"]
            return {
                "verdicts": [
                    {"item_id": item_id, "supported": "yes", "reason": "first"},
                    {"item_id": item_id, "supported": "no", "reason": "conflict"},
                ]
            }

        summary, warnings = self._run(
            pool,
            StubLLM(property_handler=property_handler, evidence_handler=contradictory),
        )
        self.assertNotIn("description", entity.properties)
        self.assertEqual(summary["applied"], 0)
        self.assertIn("PROPERTY_EVIDENCE_UNVERIFIED", {row["code"] for row in warnings})

    def test_bundle_semantics_are_scoped_to_entity_anchor(self) -> None:
        self.chunk.text = "设备A用于每日检查轨道状态，设备B用于运输物料。"
        pool, entity = self._pool()

        def property_handler(prompt: str, chunk: dict) -> dict:
            return {
                "updates": [
                    {
                        "entity_id": row["id"],
                        "properties": {"description": "运输物料"},
                        "evidence": {"quote": "设备B用于运输物料"},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        def reject_cross_entity(prompt: str, chunk: dict, items: list[dict]) -> dict:
            self.assertEqual(len(items), 1)
            self.assertIn("设备A", items[0]["entity_context"])
            self.assertIn("设备B", items[0]["entity_context"])
            return {
                "verdicts": [
                    {
                        "item_id": items[0]["item_id"],
                        "supported": "no",
                        "reason": "运输事实属于设备B",
                    }
                ]
            }

        summary, warnings = self._run(
            pool,
            StubLLM(
                property_handler=property_handler,
                evidence_handler=reject_cross_entity,
            ),
        )
        self.assertNotIn("description", entity.properties)
        self.assertEqual(summary["verifications"], 1)
        self.assertIn("PROPERTY_EVIDENCE_REJECTED", {row["code"] for row in warnings})

    def test_authoritative_entity_can_complete_identity_before_freeze(self) -> None:
        pool, entity = self._pool()
        entity.source = "standard_semantics"
        entity.properties.clear()

        def identity_handler(prompt: str, chunk: dict) -> dict:
            return {
                "updates": [
                    {
                        "entity_id": row["id"],
                        "properties": {"deviceId": "D-001"},
                        "evidence": {"quote": "设备A用于每日检查轨道状态"},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        summary = PE.enrich_entity_identities(
            pool,
            self.manager,
            self.plan,
            StubLLM(identity_handler=identity_handler),
            route=self.route,
            attempts=[],
            warnings=[],
            max_attempts=1,
            retry_base_seconds=0,
        )
        self.assertEqual(entity.properties["deviceId"], "D-001")
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(summary["verifications"], 1)

    def test_contextual_bundle_evidence_uses_valid_span_id(self) -> None:
        evidence = PE._contextual_evidence(self.chunk, 0, len(self.chunk.text))
        self.assertEqual(evidence.span_id, f"c1:0:{len(self.chunk.text)}")
        self.assertEqual(evidence.quote, self.chunk.text)

    def test_entity_without_anchor_is_not_sent_to_llm(self) -> None:
        pool, entity = self._pool(anchored=False)
        llm = StubLLM()
        summary, warnings = self._run(pool, llm)
        self.assertEqual(llm.calls, [])
        self.assertNotIn("description", entity.properties)
        self.assertEqual(summary["rejected"], 1)
        self.assertIn("PROPERTY_ENTITY_WITHOUT_ANCHOR", {row["code"] for row in warnings})

    def test_repair_cannot_add_entity_after_freeze(self) -> None:
        pool, _ = self._pool()
        warnings: list[dict] = []
        applied = SE._apply_repair_proposal(
            pool,
            {
                "entities": [
                    {
                        "type": "Device",
                        "name": "设备B",
                        "properties": {"deviceId": "D-002"},
                        "evidence": {"quote": "设备A"},
                    }
                ],
                "relations": [],
            },
            self.manager,
            warnings,
        )
        self.assertEqual(applied, 0)
        self.assertEqual(pool.entity_candidate_count, 1)
        self.assertIn("REPAIR_ENTITY_REJECTED_FROZEN", {row["code"] for row in warnings})


class RelationValidationTest(unittest.TestCase):
    """AC-7 关系校验：重构后 relation pass 只枚举合法 pair，非法关系在

    CandidatePool/RelationResolver 边界被拒绝，这里直接对池做单元测试。"""

    def _pool(self) -> "SE.CandidatePool":
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        plan = SE.compile_plan(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        pool.add_entity(
            SE.EntityCandidate(
                candidate_id="",
                proposed_type="SimpleRule",
                canonical_type="SimpleRule",
                name="R-001",
                properties={"ruleId": "R-001"},
                evidence=[],
                source="test",
            )
        )
        pool.add_entity(
            SE.EntityCandidate(
                candidate_id="",
                proposed_type="Object",
                canonical_type="Object",
                name="办公设备",
                properties={"objectName": "办公设备"},
                evidence=[],
                source="test",
            )
        )
        return pool

    def _relation(self, from_id: str, rel_type: str, to_id: str) -> "SE.RelationCandidate":
        return SE.RelationCandidate(
            candidate_id="",
            from_candidate_id=from_id,
            to_candidate_id=to_id,
            proposed_relation=rel_type,
            canonical_relation=None,
            evidence=[],
        )

    def test_unknown_endpoint_rejected(self) -> None:
        pool = self._pool()
        warnings: list[dict] = []
        resolved = pool.add_relation(
            self._relation("entity-99", "constrainsObject", "entity-1"),
            warnings=warnings,
        )
        self.assertIsNone(resolved)
        self.assertEqual(pool.accepted_relations(), [])
        self.assertIn("UNKNOWN_RELATION_ENDPOINT_ID", {w["code"] for w in warnings})

    def test_relation_type_mismatch_rejected(self) -> None:
        pool = self._pool()
        warnings: list[dict] = []
        resolved = pool.add_relation(
            self._relation("entity-0", "hasSubRule", "entity-1"),
            warnings=warnings,
        )
        self.assertIsNone(resolved)
        self.assertEqual(pool.accepted_relations(), [])
        self.assertIn("RELATION_ENDPOINT_TYPE_MISMATCH", {w["code"] for w in warnings})

    def test_unknown_relation_type_rejected(self) -> None:
        pool = self._pool()
        warnings: list[dict] = []
        resolved = pool.add_relation(
            self._relation("entity-0", "NOT_IN_ONTOLOGY", "entity-1"),
            warnings=warnings,
        )
        self.assertIsNone(resolved)
        self.assertEqual(pool.accepted_relations(), [])
        self.assertIn("UNKNOWN_RELATION_TYPE", {w["code"] for w in warnings})

    def test_valid_relation_accepted(self) -> None:
        pool = self._pool()
        warnings: list[dict] = []
        resolved = pool.add_relation(
            self._relation("entity-0", "constrainsObject", "entity-1"),
            warnings=warnings,
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.canonical_relation, "constrainsObject")
        self.assertEqual(resolved.from_name, "R-001")
        self.assertEqual(resolved.to_name, "办公设备")

    def test_duplicate_relation_unions_evidence(self) -> None:
        pool = self._pool()
        manager = CC.ChunkContextManager(
            [
                CC.ExtractionChunk(chunk_id="c1", text="R-001约束办公设备。"),
                CC.ExtractionChunk(chunk_id="c2", text="办公设备受R-001约束。"),
            ]
        )
        warnings: list[dict] = []
        for quote in ("R-001约束办公设备", "办公设备受R-001约束"):
            applied = SE._apply_repair_proposal(
                pool,
                {
                    "relations": [
                        {
                            "from_id": "entity-0",
                            "to_id": "entity-1",
                            "rel_type": "constrainsObject",
                            "evidence": {"quote": quote},
                        }
                    ]
                },
                manager,
                warnings,
            )
            self.assertIn(applied, {0, 1})
        relation = pool.accepted_relations()[0]
        self.assertEqual(
            {item.span_id.split(":", 1)[0] for item in relation.evidence},
            {"c1", "c2"},
        )

    def test_repair_evidence_is_bound_only_within_selected_chunks(self) -> None:
        pool = self._pool()
        quote = "R-001约束办公设备"
        manager = CC.ChunkContextManager(
            [
                CC.ExtractionChunk(chunk_id="c1", text=f"历史摘录：{quote}。"),
                CC.ExtractionChunk(chunk_id="c2", text=f"待修复条款：{quote}。"),
            ]
        )
        applied = SE._apply_repair_proposal(
            pool,
            {
                "relations": [
                    {
                        "from_id": "entity-0",
                        "to_id": "entity-1",
                        "rel_type": "constrainsObject",
                        "evidence": {"quote": quote},
                    }
                ]
            },
            manager,
            [],
            allowed_chunk_ids=("c2",),
        )
        self.assertEqual(applied, 1)
        self.assertEqual(pool.accepted_relations()[0].evidence[0].source_block_ids, ["c2"])

    def test_single_cardinality_conflict_is_rejected(self) -> None:
        ontology = """@startuml cardinality
class Source
class Target
Source "0..*" --> "0..1" Target : owns
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema)
        for type_id, name in (("Source", "S"), ("Target", "T1"), ("Target", "T2")):
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[],
                source="test",
            ))
        warnings: list[dict] = []
        first = pool.add_relation(self._relation("entity-0", "owns", "entity-1"), warnings=warnings)
        second = pool.add_relation(self._relation("entity-0", "owns", "entity-2"), warnings=warnings)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(pool.accepted_relations()), 1)
        self.assertIn("RELATION_CARDINALITY_EXCEEDED", {item["code"] for item in warnings})

    def test_cardinality_prefers_stronger_evidence_independent_of_arrival_order(self) -> None:
        ontology = """@startuml cardinality
class Source
class Target
Source "0..*" --> "0..1" Target : owns
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)

        def run(order: tuple[str, str]) -> tuple[str, set[str]]:
            pool = SE.CandidatePool(schema)
            for type_id, name in (("Source", "S"), ("Target", "T1"), ("Target", "T2")):
                pool.add_entity(SE.EntityCandidate(
                    candidate_id="",
                    proposed_type=type_id,
                    canonical_type=type_id,
                    name=name,
                    properties={},
                    evidence=[],
                    source="test",
                ))
            candidates = {
                "weak": SE.RelationCandidate(
                    candidate_id="",
                    from_candidate_id="entity-0",
                    to_candidate_id="entity-1",
                    proposed_relation="owns",
                    canonical_relation=None,
                    evidence=[MODULES["evidence"].Evidence("c1:0:4", ["c1"], "owns")],
                ),
                "strong": SE.RelationCandidate(
                    candidate_id="",
                    from_candidate_id="entity-0",
                    to_candidate_id="entity-2",
                    proposed_relation="owns",
                    canonical_relation=None,
                    evidence=[MODULES["evidence"].Evidence("c1:5:14", ["c1"], "S owns T2")],
                ),
            }
            warnings: list[dict] = []
            for name in order:
                pool.add_relation(candidates[name], warnings=warnings)
            self.assertEqual(pool.relation_candidate_count, 2)
            self.assertEqual(
                sum(item.mapping_status == "rejected" for item in pool.relation_candidates()),
                1,
            )
            return pool.accepted_relations()[0].to_candidate_id, {
                item["code"] for item in warnings
            }

        forward_target, forward_codes = run(("weak", "strong"))
        reverse_target, reverse_codes = run(("strong", "weak"))
        self.assertEqual(forward_target, "entity-2")
        self.assertEqual(reverse_target, "entity-2")
        self.assertIn("RELATION_CARDINALITY_SUPERSEDED", forward_codes)
        self.assertIn("RELATION_CARDINALITY_EXCEEDED", reverse_codes)

    def test_cardinality_reconsiders_previously_blocked_candidate(self) -> None:
        ontology = """@startuml cardinality
class Source
class Target
Source "0..1" --> "0..1" Target : owns
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema)
        for type_id, name in (
            ("Source", "S1"),
            ("Source", "S2"),
            ("Target", "T1"),
            ("Target", "T2"),
        ):
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[],
                source="test",
            ))

        def relation(from_id: str, to_id: str, score: float) -> "SE.RelationCandidate":
            candidate = self._relation(from_id, "owns", to_id)
            candidate.mapping_score = score
            return candidate

        # A is first displaced by B.  C then displaces B, so a global rebuild
        # must allow A to become accepted again because A and C do not conflict.
        warnings: list[dict] = []
        pool.add_relation(relation("entity-0", "entity-2", 1.0), warnings=warnings)  # A
        pool.add_relation(relation("entity-0", "entity-3", 2.0), warnings=warnings)  # B
        pool.add_relation(relation("entity-1", "entity-3", 3.0), warnings=warnings)  # C
        self.assertEqual(
            {
                (item.from_candidate_id, item.to_candidate_id)
                for item in pool.accepted_relations()
            },
            {("entity-0", "entity-2"), ("entity-1", "entity-3")},
        )
        self.assertNotIn(
            "relation-0",
            {
                str((item.get("value") or {}).get("candidate_id") or "")
                for item in warnings
                if item.get("code", "").startswith("RELATION_CARDINALITY_")
            },
        )
        self.assertNotIn(
            "relation-0",
            {
                str((item.get("value") or {}).get("candidate_id") or "")
                for item in pool.unresolved_relation_rejections()
            },
        )

    def test_endpoint_remap_prunes_merged_cardinality_rejection(self) -> None:
        ontology = """@startuml cardinality
class Source
class Target
Source "0..*" --> "0..1" Target : owns
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema)
        for type_id, name in (("Source", "S"), ("Target", "T1"), ("Target", "T2")):
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[],
                source="test",
            ))
        warnings: list[dict] = []
        pool.add_relation(self._relation("entity-0", "owns", "entity-1"), warnings=warnings)
        pool.add_relation(self._relation("entity-0", "owns", "entity-2"), warnings=warnings)
        pool._relation_resolver.remap_endpoints(  # noqa: SLF001 - resolver invariant test
            {"entity-2": "entity-1"},
            warnings=warnings,
        )
        self.assertEqual(pool.relation_candidate_count, 1)
        self.assertEqual(pool.unresolved_relation_rejections(), [])

    def test_cardinality_rebuild_does_not_revive_other_rejection(self) -> None:
        ontology = """@startuml cardinality
class Source
class Target
Source "0..*" --> "0..1" Target : owns
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema)
        for type_id, name in (
            ("Source", "S1"),
            ("Source", "S2"),
            ("Target", "T1"),
            ("Target", "T2"),
        ):
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties={},
                evidence=[],
                source="test",
            ))
        externally_rejected = self._relation("entity-0", "owns", "entity-2")
        pool.add_relation(externally_rejected)
        externally_rejected.mapping_status = "rejected"
        externally_rejected.warnings.append("EXTERNAL_REJECTION")
        pool.add_relation(self._relation("entity-1", "owns", "entity-3"))
        self.assertEqual(externally_rejected.mapping_status, "rejected")
        self.assertEqual(
            [(item.from_candidate_id, item.to_candidate_id) for item in pool.accepted_relations()],
            [("entity-1", "entity-3")],
        )


class GlobalIdentityResolutionTest(unittest.TestCase):
    def test_different_mentions_merge_after_identity_finalization(self) -> None:
        ontology = """@startuml identity
class "机构" as Organization {
  + 机构编号 (orgId): String
}
' @identity Organization: orgId
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema, ontology_namespace="identity")
        for index, (name, chunk_id) in enumerate((("中国中车", "c1"), ("中车", "c2"))):
            pool.add_entity(
                SE.EntityCandidate(
                    candidate_id="",
                    proposed_type="Organization",
                    canonical_type="Organization",
                    name=name,
                    properties={},
                    evidence=[SE.Evidence(
                        span_id=f"{chunk_id}:0:{len(name)}",
                        source_block_ids=[chunk_id],
                        quote=name,
                        start_char=0,
                        end_char=len(name),
                    )],
                    source="test",
                )
            )

        self.assertEqual(pool.entity_candidate_count, 2)
        for entity in pool.entity_candidates():
            entity.properties["orgId"] = "ORG-001"
        summary = pool.finalize_identities()
        remap = pool.resolve_global()
        self.assertEqual(summary, {"identified": 2, "unresolved": 0, "quarantined": 0})
        self.assertEqual(pool.entity_candidate_count, 1)
        entity = pool.accepted_entities()[0]
        self.assertEqual(entity.lifecycle, "RESOLVED")
        self.assertEqual(entity.identity_property, "orgId")
        self.assertEqual(entity.identity_value, "ORG-001")
        self.assertEqual(len(entity.evidence), 2)
        self.assertEqual(remap["entity-1"], "entity-0")
        self.assertEqual(set(entity.mention_aliases), {"中国中车", "中车"})

    def test_same_name_different_stable_identity_does_not_merge(self) -> None:
        ontology = """@startuml identity
class "机构" as Organization {
  + 机构编号 (orgId): String
}
' @identity Organization: orgId
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema, ontology_namespace="identity")
        for index, chunk_id in enumerate(("c1", "c2"), start=1):
            pool.add_entity(SE.EntityCandidate(
                candidate_id="",
                proposed_type="Organization",
                canonical_type="Organization",
                name="同名机构",
                properties={"orgId": f"ORG-00{index}"},
                evidence=[SE.Evidence(
                    span_id=f"{chunk_id}:0:4",
                    source_block_ids=[chunk_id],
                    quote="同名机构",
                    start_char=0,
                    end_char=4,
                )],
                source="test",
            ))
        self.assertEqual(pool.entity_candidate_count, 2)
        pool.finalize_identities()
        remap = pool.resolve_global()
        self.assertEqual(pool.entity_candidate_count, 2)
        self.assertEqual(remap, {"entity-0": "entity-0", "entity-1": "entity-1"})

    def test_same_name_late_divergent_identity_stays_split(self) -> None:
        ontology = """@startuml identity
class "机构" as Organization {
  + 机构编号 (orgId): String
}
' @identity Organization: orgId
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema, ontology_namespace="identity")
        for chunk_id in ("c1", "c2"):
            pool.add_entity(SE.EntityCandidate(
                candidate_id="", proposed_type="Organization", canonical_type="Organization",
                name="同名机构", properties={}, evidence=[SE.Evidence(
                    span_id=f"{chunk_id}:0:4", source_block_ids=[chunk_id], quote="同名机构",
                    start_char=0, end_char=4,
                )], source="test",
            ))
        self.assertEqual(pool.entity_candidate_count, 2)
        pool.entity_candidates()[0].properties["orgId"] = "ORG-001"
        pool.entity_candidates()[1].properties["orgId"] = "ORG-002"
        pool.finalize_identities()
        remap = pool.resolve_global()
        self.assertEqual(pool.entity_candidate_count, 2)
        self.assertEqual(remap, {"entity-0": "entity-0", "entity-1": "entity-1"})

    def test_missing_declared_identity_can_be_quarantined(self) -> None:
        ontology = """@startuml identity
class "机构" as Organization {
  + 机构编号 (orgId): String
}
class "对象" as Object {
  + 对象编号 (objectId): String
}
Organization --> Object : owns
' @identity Organization: orgId
' @identity Object: objectId
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        warnings = []
        pool = SE.CandidatePool(
            schema, ontology_namespace="identity", identity_admission="quarantine"
        )
        entity = pool.add_entity(SE.EntityCandidate(
            candidate_id="", proposed_type="Organization", canonical_type="Organization",
            name="未识别机构", properties={}, evidence=[], source="test",
        ))
        summary = pool.finalize_identities(warnings=warnings)
        self.assertEqual(summary["quarantined"], 1)
        self.assertEqual(entity.lifecycle, "QUARANTINED")
        self.assertEqual(entity.mapping_status, "rejected")
        self.assertEqual(pool.accepted_entities(), [])
        self.assertIn("ENTITY_IDENTITY_QUARANTINED", {item["code"] for item in warnings})
        target = pool.add_entity(SE.EntityCandidate(
            candidate_id="", proposed_type="Object", canonical_type="Object",
            name="目标", properties={"objectId": "OBJ-1"}, evidence=[], source="test",
        ))
        pool.finalize_identities(warnings=warnings)
        relation = pool.add_relation(SE.RelationCandidate(
            candidate_id="", from_candidate_id=entity.candidate_id,
            to_candidate_id=target.candidate_id, proposed_relation="owns",
            canonical_relation=None, evidence=[],
        ), warnings=warnings)
        self.assertIsNone(relation)
        self.assertIn("RELATION_ENDPOINT_QUARANTINED", {item["code"] for item in warnings})
        entity.properties["orgId"] = "ORG-RECOVERED"
        recovered = pool.finalize_identities(warnings=warnings)
        self.assertEqual(recovered["identified"], 2)
        self.assertEqual(entity.lifecycle, "IDENTIFIED")
        self.assertEqual(entity.mapping_status, "accepted")

    def test_rule_id_missing_is_unresolved_but_still_accepted(self) -> None:
        ontology = """@startuml identity
class "规则" as Rule {
  + 规则编号 (ruleId): String
}
' @identity Rule: ruleId
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        warnings = []
        pool = SE.CandidatePool(schema, ontology_namespace="identity")
        entity = pool.add_entity(SE.EntityCandidate(
            candidate_id="", proposed_type="Rule", canonical_type="Rule",
            name="必须检查", properties={}, evidence=[], source="test",
        ))

        summary = pool.finalize_identities(warnings=warnings)

        self.assertEqual(summary, {"identified": 0, "unresolved": 1, "quarantined": 0})
        self.assertEqual(entity.lifecycle, "UNRESOLVED")
        self.assertEqual(entity.mapping_status, "accepted")
        self.assertEqual(entity.identity_property, "name")
        self.assertNotIn("ruleId", entity.properties)
        self.assertEqual(pool.accepted_entities(), [entity])
        self.assertIn("RULE_ID_MISSING", {item["code"] for item in warnings})

    def test_rule_id_missing_does_not_restore_other_rejection(self) -> None:
        ontology = """@startuml identity
class "规则" as Rule {
  + 规则编号 (ruleId): String
}
' @identity Rule: ruleId
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        warnings = []
        pool = SE.CandidatePool(schema, ontology_namespace="identity")
        entity = pool.add_entity(SE.EntityCandidate(
            candidate_id="", proposed_type="Rule", canonical_type="Rule",
            name="证据不足规则", properties={}, evidence=[], source="test",
        ))
        entity.mapping_status = "rejected"
        entity.warnings.append("PROPERTY_EVIDENCE_REJECTED")

        pool.finalize_identities(warnings=warnings)

        self.assertEqual(entity.lifecycle, "UNRESOLVED")
        self.assertEqual(entity.mapping_status, "rejected")
        self.assertEqual(pool.accepted_entities(), [])
        self.assertIn("RULE_ID_MISSING", {item["code"] for item in warnings})

    def test_rule_id_present_remains_stable_identity(self) -> None:
        ontology = """@startuml identity
class "规则" as Rule {
  + 规则编号 (ruleId): String
}
' @identity Rule: ruleId
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema, ontology_namespace="identity")
        entity = pool.add_entity(SE.EntityCandidate(
            candidate_id="", proposed_type="Rule", canonical_type="Rule",
            name="规则原文", properties={}, evidence=[], source="test",
        ))
        duplicate = pool.add_entity(SE.EntityCandidate(
            candidate_id="", proposed_type="Rule", canonical_type="Rule",
            name="规则别名", properties={}, evidence=[], source="test",
        ))
        entity.properties["ruleId"] = "R-001"
        duplicate.properties["ruleId"] = "R-001"

        summary = pool.finalize_identities()
        remap = pool.resolve_global()

        self.assertEqual(summary, {"identified": 2, "unresolved": 0, "quarantined": 0})
        self.assertEqual(entity.lifecycle, "RESOLVED")
        self.assertEqual(entity.identity_property, "ruleId")
        self.assertEqual(entity.identity_value, "R-001")
        self.assertEqual(pool.entity_candidate_count, 1)
        self.assertEqual(remap, {"entity-0": "entity-0", "entity-1": "entity-0"})

    def test_relation_endpoint_rewritten_after_merge(self) -> None:
        ontology = """@startuml identity
class "机构" as Organization {
  + 机构编号 (orgId): String
}
class "对象" as Object {
  + 对象编号 (objectId): String
}
Organization --> Object : employs
' @identity Organization: orgId
' @identity Object: objectId
@enduml
"""
        schema = MODULES["ontology_schema"].OntologySchema.parse(ontology)
        pool = SE.CandidatePool(schema, ontology_namespace="identity")
        for name in ("中国中车", "中车"):
            pool.add_entity(SE.EntityCandidate(
                candidate_id="", proposed_type="Organization", canonical_type="Organization",
                name=name, properties={}, evidence=[], source="test",
            ))
        pool.add_entity(SE.EntityCandidate(
            candidate_id="", proposed_type="Object", canonical_type="Object",
            name="项目", properties={"objectId": "OBJ-1"}, evidence=[], source="test",
        ))
        pool.add_relation(SE.RelationCandidate(
            candidate_id="", from_candidate_id="entity-1", to_candidate_id="entity-2",
            proposed_relation="employs", canonical_relation=None, evidence=[],
        ))
        pool.entity_candidates()[0].properties["orgId"] = "ORG-1"
        pool.entity_candidates()[1].properties["orgId"] = "ORG-1"
        pool.finalize_identities()
        pool.resolve_global()
        relation = pool.accepted_relations()[0]
        self.assertEqual(relation.from_candidate_id, "entity-0")
        self.assertEqual(relation.to_candidate_id, "entity-2")


class FailureAndEdgeTest(unittest.TestCase):
    def test_json_parse_failure_recorded(self) -> None:
        def llm(messages) -> str:
            return "not json"

        result = SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)],
            llm=llm,
        )
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        # v3 实体 pass：1 次 json 尝试 + 1 次 json 恢复尝试后终止；无实体 -> 不触发关系/属性 pass
        self.assertEqual(len(result["attempts"]), 2)
        self.assertTrue(all(attempt["type"] == "json" for attempt in result["attempts"]))
        self.assertEqual(result["error_code"], "LLM_JSON_PARSE_FAILED")

    def test_request_failure_recorded(self) -> None:
        def llm(messages) -> str:
            raise RuntimeError("network down")

        result = SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)],
            llm=llm,
        )
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        self.assertEqual(result["attempts"][0]["type"], "request")
        self.assertEqual(result["error_code"], "LLM_REQUEST_FAILED")

    def test_empty_input(self) -> None:
        llm = StubLLM()
        result = SE.extract_split(RULE_ONTOLOGY, chunks=[], llm=llm)
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("EMPTY_INPUT", codes)
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        self.assertEqual(result["profile"]["chunks"], 0)

    def test_uid_stable_regardless_of_properties(self) -> None:
        objects = [
            {"id": "entity-0", "type": "SimpleRule", "identity": "R-001", "properties": {"action": "A"}},
            {"id": "entity-1", "type": "SimpleRule", "identity": "R-001", "properties": {"action": "B"}},
        ]
        graph = GA.GraphAssembler().build_graph(objects, [])
        self.assertEqual(len(graph["nodes"]), 1)  # 同一 identity 合并为同一 UID
        self.assertEqual(
            graph["nodes"][0]["uid"],
            GU.entity_uid("SimpleRule", "R-001", None),
        )


class BenchmarkSmokeTest(unittest.TestCase):
    def test_ontology_class_extraction_gold(self) -> None:
        case = json.loads((CASES_DIR / "04_ontology_class_extraction.json").read_text(encoding="utf-8"))
        ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")
        text = case["text"]
        # gold 实体名必须逐字落在原文中（span-first）；取 gold 语义对应的原文 mention
        mentions = [
            "开展排查",
            "将开源智能体安全风险排查情况（附件2）通过内网办公邮箱报集团科信部",
            "加强网络和数据安全监测",
            "第一时间处置，并向集团报告",
        ]
        for mention in mentions:
            self.assertIn(mention, text)

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            return [span_item("OperationalActivity", mention) for mention in mentions]

        llm = StubLLM(entity_handler=entity_handler)
        result = SE.extract_split(
            ontology,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=text)],
            llm=llm,
            target_types=case["target_types"],
            include_subclasses=case["include_subclasses"],
            validation_mode=case["validation_mode"],
            identity_admission="observe",
        )
        pred_keys = {(e["type"], e["name"]) for e in result["raw"]["entities"]}
        self.assertEqual(
            pred_keys,
            {("OperationalActivity", mention) for mention in mentions},
        )
        self.assertEqual(result["evidence_coverage"]["entities"]["rate"], 1.0)
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertNotIn("ENTITY_OUT_OF_SCOPE", codes)


class FinalVerificationStatusTest(unittest.TestCase):
    def test_repair_no_new_facts_keeps_unresolved_coverage_visible(self) -> None:
        issue = V.VerificationIssue(
            code="REQUIRED_RELATION_MISSING",
            message="missing required relation",
            severity="warning",
            repair_type="required_relation",
        )
        reports = iter(
            [
                V.VerificationReport((issue,)),
                V.VerificationReport((issue,)),
            ]
        )
        result = SE.TargetedRepairOrchestrator(max_rounds=2).run(
            object(),
            verify=lambda _state: next(reports),
            repair=lambda _state, _issues, _round: {"relations": []},
            apply=lambda state, _proposal: (state, 0),
        )

        self.assertFalse(result.succeeded)
        self.assertFalse(result.coverage_complete)
        self.assertEqual(result.unresolved_repairable_count, 1)
        self.assertEqual(result.stop_reason, "repair_no_new_facts")
        self.assertEqual(result.to_dict()["unresolved_repairable"][0]["code"], "REQUIRED_RELATION_MISSING")

    def test_permissive_hard_verification_is_partial_not_success(self) -> None:
        report = V.VerificationReport(
            (V.VerificationIssue(code="FORCED_FINAL_INVALID", message="invalid"),)
        )
        llm = StubLLM(
            entity_handler=lambda _prompt, _chunk: [span_item("Term", "制动系统")],
        )
        with patch.object(SE.ExtractionVerifier, "verify", return_value=report):
            result = SE.extract_split(
                "@startuml\nclass Term\n@enduml",
                chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text="制动系统")],
                llm=llm,
                validation_mode="permissive",
                best_effort=True,
                identity_admission="observe",
                max_repair_rounds=0,
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error_code"], "LLM_SCHEMA_INVALID")
        self.assertFalse(result["verification"]["valid"])
        entity = result["raw"]["entities"][0]
        self.assertNotEqual(entity["confidence_status"], "accepted")
        self.assertEqual(entity["gate_status"], "blocked")
        self.assertIn(
            "final_verification_failed",
            entity["confidence_details"]["gates"],
        )


class OntologyPropertyFormatTest(unittest.TestCase):
    """输出实体 properties 必须覆盖本体声明的全部字段（缺失用空值占位）。"""

    ONTOLOGY = """@startuml ont-props
class "规则" <<RuleType>> {
  - RuleID (规则ID) //规则的唯一标识符
  - RuleName (RuleName) //对规则语义的简要描述
  - antecedent (antecedent) //规则成立所依赖的前提条件
  - tags (tags): String[] //标签
}
@enduml"""

    def _plan(self):
        OC = MODULES["ontology_compiler"]
        return OC.compile_plan(
            self.ONTOLOGY, include_subclasses=True, mode="strict", unknown_content_policy="ignore"
        )

    def test_identifier_keys_are_mapped_and_missing_values_filled(self) -> None:
        plan = self._plan()
        entities = [{"type": "RuleType", "name": "电源板", "properties": {"规则ID": "电源板", "RuleName": "电源板"}}]
        out = SE._complete_ontology_properties(entities, plan)
        props = out[0]["properties"]
        self.assertEqual(props["RuleID"], "电源板")
        self.assertEqual(props["RuleName"], "电源板")
        self.assertEqual(props["antecedent"], "")
        self.assertEqual(props["tags"], [])

    def test_all_declared_keys_present(self) -> None:
        plan = self._plan()
        entities = [{"type": "RuleType", "name": "x", "properties": {}}]
        out = SE._complete_ontology_properties(entities, plan)
        declared = {str(p.display_name or p.identifier) for p in plan.class_specs["RuleType"].properties}
        self.assertEqual(set(out[0]["properties"].keys()), declared)


if __name__ == "__main__":
    unittest.main(verbosity=2)
