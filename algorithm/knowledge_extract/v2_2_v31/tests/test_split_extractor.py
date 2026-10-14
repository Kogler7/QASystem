"""Phase 1 Step 4 单元测试：Entity / Relation 拆分（Two-Pass）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v2_2_v31/tests/test_split_extractor.py

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
    "knowledge_extract_v22_v31",
    PACKAGE_DIR,
    [
        "config",
        "scheduler",
        "graph_utils",
        "ontology_schema",
        "ontology_compiler",
        "chunk_context",
        "evidence",
        "graph_assembler",
        "prompt_router",
        "relation_resolver",
        "candidate_generation",
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

from algorithm.knowledge_extract.v2_2_v31.tests.stub_llm import StubLLM, parse_known_entities, span_item  # noqa: E402


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
                "type": row["type"],
                "name": row["name"].strip(),
                "properties": properties,
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        )
    return {"entities": updates}


def _yes_if_known(item: dict, chunk: dict) -> str:
    value = str(item.get("value") or "")
    known = {
        COMPOSITE_TEXT,
        "AND",
        *MEMBER_RULE_TEXTS,
        *MEMBER_MENTIONS,
        "不得部署安装各类开源智能体",
    }
    return "yes" if value in known else "no"


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
        self.assertIn("hassubrule", prompt)
        self.assertIn("CR-001", prompt)
        self.assertIn("R-001", prompt)
        self.assertIn("只判断本体已经允许的实体对；不得创建新端点，不得输出该对未列出的关系", prompt)

    def test_relation_pairs_are_screened_by_ontology_endpoints(self) -> None:
        ontology = (ONTOLOGIES_DIR / "official_document_ontology.puml").read_text(encoding="utf-8")
        plan = SE.compile_plan(ontology)
        entities = [
            {"id": "doc", "type": "OfficialDocument", "name": "关于开展排查的通知"},
            {"id": "agent", "type": "OperationalAgent", "name": "各一级子公司"},
            {"id": "unit", "type": "DocumentUnit", "name": "排查要求一"},
            {"id": "activity", "type": "OperationalActivity", "name": "组织开展排查"},
            {"id": "info", "type": "OperationalInformation", "name": "排查反馈表"},
        ]
        pairs = RR.enumerate_relation_pairs(
            plan,
            entities,
            skip_tokens=RR.withheld_relation_tokens("official_document"),
        )
        by_ends = {(pair["source_id"], pair["target_id"]): RR.pair_relation_ids(pair) for pair in pairs}
        # 同一对端点只保留本体允许的关系，多条签名合成一次判断。
        self.assertEqual(by_ends[("doc", "agent")], ["applies_to", "issued_by"])
        self.assertEqual(by_ends[("activity", "info")], ["produces", "uses_information"])
        # HAS_PART / EVIDENCED_BY 由结构组装，不进入关系分类。
        self.assertNotIn(("doc", "unit"), by_ends)
        self.assertNotIn(("agent", "unit"), by_ends)
        # 没有从主体指向公文的本体关系。
        self.assertNotIn(("agent", "doc"), by_ends)
        self.assertTrue(all(pair["source_id"] != pair["target_id"] for pair in pairs))
        chunk = CC.ExtractionChunk(chunk_id="chunk-0", text="各一级子公司组织开展排查，并填写排查反馈表。")
        route = PR.resolve_document_route(plan, [chunk])
        prompt = PR.build_chat_messages(
            task="relation",
            plan=plan,
            route=route,
            chunk=chunk,
            known_entities=entities,
            output_schema=CG.relation_pair_decision_schema(pairs),
            relation_pairs=pairs,
        )[1]["content"]
        self.assertIn("各一级子公司", prompt)
        self.assertIn("applies_to|issued_by", prompt)
        self.assertIn("发布主体", prompt)
        self.assertIn("适用主体", prompt)


class TwoPassPipelineTest(unittest.TestCase):
    def test_end_to_end_composite_rule(self) -> None:
        llm = StubLLM(
            entity_handler=composite_entity_handler,
            property_handler=composite_property_handler,
            relation_handler=composite_relation_handler,
            verification_handler=_yes_if_known,
        )
        result = SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)],
            llm=llm,
            clef=False,
            skill_refs=[],
            max_repair_rounds=0,
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
        stages = [task for task, _ in llm.calls]
        # skill_refs=[] 为显式 no_match，跳过 skill_selection LLM。
        self.assertIn("entity", stages)
        self.assertIn("relation", stages)
        self.assertEqual(result["profile"]["entity_extract_version"], "v3.1-aligned")
        self.assertEqual(result["profile"]["deterministic_enrichment"], "enabled")
        self.assertTrue(result["profile"]["entity_frozen"])
        self.assertIsNone(result["error_code"])

    def test_exact_type_name_merge_across_chunks(self) -> None:
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
            CC.ExtractionChunk(chunk_id="c1", text="开展企业主数据治理（也称主数据管理），建立健全主数据识别、采集、校验机制。"),
            CC.ExtractionChunk(chunk_id="c2", text="主数据治理是数据治理的核心。"),
        ]

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            text = str(chunk.get("text") or "")
            if "数据治理的核心" in text:
                return [span_item("Terminology", "主数据治理", quote="主数据治理是数据治理的核心")]
            return [span_item("Terminology", "主数据治理", quote="开展企业主数据治理（也称主数据管理）")]

        def property_handler(prompt: str, chunk: dict) -> dict:
            return {
                "entities": [
                    {
                        "type": row["type"],
                        "name": row["name"].strip(),
                        "properties": {
                            "TermName": "主数据治理",
                            "TermDefinition": "对主数据的识别、采集、校验等治理活动",
                            "Synonyms": ["主数据管理"],
                        },
                        "evidence": {"quote": "开展企业主数据治理（也称主数据管理）"},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        llm = StubLLM(
            entity_handler=entity_handler,
            property_handler=property_handler,
            verification_handler=lambda item, chunk: "yes",
        )
        result = SE.extract_split(
            terminology_ontology,
            chunks=chunks,
            llm=llm,
            skill_refs=[],
            max_repair_rounds=0,
            clef=False,
        )
        entities = result["raw"]["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["candidate_id"], "entity-0")
        properties = result["intermediate_model"]["objects"][0]["properties"]
        self.assertEqual(properties["TermDefinition"], "对主数据的识别、采集、校验等治理活动")
        self.assertEqual(properties["Synonyms"], ["主数据管理"])

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
        # 实体 pass 有界重试（默认 3 次）后终止；无实体 -> 不触发关系/属性 pass
        self.assertEqual(len(result["attempts"]), 3)
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
        )
        pred_keys = {(e["type"], e["name"]) for e in result["raw"]["entities"]}
        self.assertEqual(
            pred_keys,
            {("OperationalActivity", mention) for mention in mentions},
        )
        self.assertEqual(result["evidence_coverage"]["entities"]["rate"], 1.0)
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertNotIn("ENTITY_OUT_OF_SCOPE", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
