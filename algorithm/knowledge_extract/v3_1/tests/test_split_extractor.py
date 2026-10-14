"""Phase 1 Step 4 单元测试：Entity / Relation 拆分（Two-Pass）。

运行（在 88 测试环境执行）：
    python algorithm/knowledge_extract/v3_1/tests/test_split_extractor.py

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

from .stub_llm import source_documents

import json
import unittest
from .. import ontology_schema
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PACKAGE_DIR / "tests" / "fixtures"
ONTOLOGIES_DIR = FIXTURES_DIR / "ontologies"
CASES_DIR = FIXTURES_DIR / "cases"
TESTS_DIR = PACKAGE_DIR / "tests"


from .. import split_extractor as SE
from .. import chunk_context as CC
from .. import graph_utils as GU
from .. import prompt_router as PR
from .. import output_contracts as OUT

from .stub_llm import StubLLM, parse_known_entities, span_item  # noqa: E402


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
                "type": row["type"], "name": row["name"],
                "properties": properties,
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        )
    return {"entities": updates}




class PromptTest(unittest.TestCase):
    def test_entity_prompt_scope_and_features_no_relations(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY, target_types=["Rule"])
        chunk = CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)
        route = PR.resolve_document_route(plan, [chunk])
        messages = PR.build_chat_messages(task="entity", plan=plan, route=route, chunk=chunk)
        prompt = messages[1]["content"]
        self.assertIn("同时提取实体候选和当前片段支持的本体属性，禁止输出关系", prompt)
        self.assertIn("`SimpleRule`", prompt)  # 父类子类展开进类型目录
        self.assertIn("`CompositeRule`", prompt)
        self.assertNotIn("relation_signatures", prompt)
        self.assertNotIn("hasSubRule", prompt)  # 实体 pass 不提供关系



class TwoPassPipelineTest(unittest.TestCase):
    def test_end_to_end_composite_rule(self) -> None:
        llm = StubLLM(
            entity_handler=composite_entity_handler,
            property_handler=composite_property_handler,
            verification_handler=lambda item, chunk: "yes" if item["value"] in [COMPOSITE_TEXT, "AND", *MEMBER_RULE_TEXTS, *MEMBER_MENTIONS, "不得部署安装各类开源智能体"] else "no",
            entity_verification_handler=lambda item, chunk: 'yes')
        result = SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)],
            llm=llm,
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
        self.assertEqual(raw["relations"], [])
        for relation in raw["relations"]:
            self.assertEqual(relation["rel_type"], "hasSubRule")
            self.assertEqual(relation["from_type"], "CompositeRule")
            self.assertEqual(relation["to_type"], "SimpleRule")
            self.assertIn("span_id", relation["evidence"])
            self.assertEqual(relation["evidence"]["source_block_ids"], ["chunk-0"])
        # AC-7：无悬空关系
        self.assertEqual(len(result["graph"]["nodes"]), 3)
        self.assertEqual(result["graph"]["relationships"], [])
        self.assertEqual(result["triples"], [])
        # O1
        model = result["intermediate_model"]
        self.assertEqual(len(model["objects"]), 3)
        self.assertEqual(model["relations"], [])
        self.assertEqual(model["objects"][0]["mapping"]["status"], result["mapping"]["entities"][0]["status"])
        self.assertEqual(model["objects"][0]["identity"], COMPOSITE_TEXT)
        # 证据覆盖
        self.assertEqual(result["evidence_coverage"]["entities"]["rate"], 1.0)
        self.assertEqual(result["evidence_coverage"]["relations"]["rate"], 1.0)
        # 实体 -> 按类型批量补全 -> 每个实体独立属性核验
        stages = [task for task, _ in llm.calls]
        self.assertEqual(stages[:2], ["skill_selection", "entity"])
        self.assertEqual(stages[2], "evidence_verification")
        self.assertCountEqual(stages[3:], ["property", "evidence_verification", "property", "evidence_verification"])
        # 无 schema/证据告警
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(result["error_code"])

    def test_same_type_name_entities_completed_once_across_chunks(self) -> None:
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
            if chunk.get("text") == "主数据治理是数据治理的核心。":
                return [span_item("Terminology", "主数据治理", quote="主数据治理是数据治理的核心")]
            return [span_item("Terminology", "主数据治理", quote="开展关键主数据治理（也称主数据管理）")]

        def property_handler(prompt: str, chunk: dict) -> dict:
            return {
                "entities": [
                    {
                        "type": row["type"], "name": row["name"],
                        "properties": {
                            "TermName": "主数据治理",
                            "TermDefinition": "对主数据的识别、采集、校验等治理活动",
                            "Synonyms": ["主数据管理"],
                        },
                        "evidence": {"quote": chunks[0].text},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        llm = StubLLM(entity_handler=entity_handler, property_handler=property_handler,
                      verification_handler=lambda item, chunk: "yes" if item["property"] in
                      {"TermName", "TermDefinition", "Synonyms"} else "uncertain", entity_verification_handler=lambda item, chunk: 'yes')
        result = SE.extract_split(terminology_ontology, chunks=chunks, llm=llm)
        entities = result["raw"]["entities"]
        self.assertEqual(len(entities), 1)
        self.assertTrue(all("candidate_id" not in e for e in entities))
        properties = result["intermediate_model"]["objects"][0]["properties"]
        self.assertEqual(properties["TermDefinition"], "对主数据的识别、采集、校验等治理活动")
        self.assertEqual(properties["Synonyms"], ["主数据管理"])
        # 两条 evidence（不同 chunk span）都保留
        self.assertEqual({cid for e in result["intermediate_model"]["objects"][0]["evidence"] for cid in e["source_block_ids"]}, {"c1", "c2"})
        self.assertEqual(len([t for t, _ in llm.calls if t == "property"]), 1)
        import json
        payload = json.loads(next(p for t, p in llm.calls if t == "property"))
        self.assertEqual([part['text'] for part in source_documents(payload)], [c.text for c in chunks])
        self.assertEqual(len(payload["task"]["known_entities"][0]["occurrences"]), 2)

    def test_out_of_scope_entity_rejected(self) -> None:
        # 重构后 entity pass 的 output schema enum 已限定在目标范围内，范围外类型
        # 在结构化校验阶段就会被拒；ENTITY_OUT_OF_SCOPE 的兜底防线在 CandidatePool。
        official = (ONTOLOGIES_DIR / "official_document_ontology.puml").read_text(encoding="utf-8")
        llm = StubLLM(entity_handler=lambda p, c: [span_item("OperationalAgent", "集团")])
        result = SE.extract_split(official, chunks=[CC.ExtractionChunk("c0", "集团")],
                                  target_types=["OperationalActivity"], llm=llm, max_attempts=1)
        self.assertEqual(result["raw"]["entities"], [])
        self.assertEqual(result["error_code"], "LLM_SCHEMA_INVALID")





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
        def run(value):
            def properties(prompt, chunk):
                return {"entities": [{"type": e["type"], "name": e["name"],
                    "properties": {"ruleText": value}, "evidence": {"quote": "规则内容：" + value}}
                    for e in parse_known_entities(prompt)]}
            return SE.extract_split(RULE_ONTOLOGY, chunks=[CC.ExtractionChunk("c", "规则内容：" + value)],
                llm=StubLLM(entity_handler=lambda p, c: [span_item("SimpleRule", "规则")],
                            property_handler=properties,
                            verification_handler=lambda item, chunk: 'yes' if item['value'] == value else 'no', entity_verification_handler=lambda item, chunk: 'yes'))
        first, second = run("A"), run("B")
        self.assertEqual(first["graph"]["nodes"][0]["uid"], second["graph"]["nodes"][0]["uid"])
        self.assertNotEqual(first["raw"]["entities"][0]["properties"], second["raw"]["entities"][0]["properties"])



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

        llm = StubLLM(entity_handler=entity_handler, verification_handler=lambda item, chunk: 'yes')
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
