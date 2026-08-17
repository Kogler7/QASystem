"""Phase 1 Step 4 单元测试：Entity / Relation 拆分（Two-Pass）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_split_extractor.py

覆盖：
- §15.1/§15.2 prompt 契约（实体 pass 禁关系、关系 pass 引用目录 id）；
- §20 实体解析/去重（跨 chunk 合并属性与证据）；
- §19 + AC-7 关系校验（端点存在、签名匹配、cardinality、拒绝悬空/越界）；
- §25/§26/§27 O1/O2 组装与 UID 稳定性；
- AC-2 范围纪律、失败归因（JSON/请求）、空输入；
- benchmark gold 冒烟（实体全覆盖、证据 100%）。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
BENCHMARK_DIR = PACKAGE_DIR / "benchmark"
ONTOLOGIES_DIR = BENCHMARK_DIR / "ontologies"
CASES_DIR = BENCHMARK_DIR / "cases"


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    parent = types.ModuleType(package_name)
    parent.__path__ = [str(directory)]
    parent.__package__ = package_name
    sys.modules[package_name] = parent
    loaded: dict[str, types.ModuleType] = {}
    for stem in stems:
        module_name = f"{package_name}.{stem}"
        spec = importlib.util.spec_from_file_location(module_name, directory / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded[stem] = module
    return loaded


MODULES = _load_package_modules(
    "knowledge_extract_v2",
    PACKAGE_DIR,
    [
        "config",
        "scheduler",
        "graph_utils",
        "ontology_schema",
        "ontology_compiler",
        "chunk_context",
        "evidence",
        "split_extractor",
    ],
)
SE = MODULES["split_extractor"]
CC = MODULES["chunk_context"]


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


class StubLLM:
    """按 prompt 段落路由到 entity/relation handler 的测试桩。"""

    def __init__(self, entity_handler=None, relation_handler=None):
        self.entity_handler = entity_handler or (lambda prompt: {"entities": []})
        self.relation_handler = relation_handler or (lambda prompt: {"relations": []})
        self.calls: list[tuple[str, str]] = []

    def __call__(self, prompt: str) -> str:
        if "实体候选提议" in prompt:
            payload = self.entity_handler(prompt)
            self.calls.append(("entity", prompt))
        elif "关系候选提议" in prompt:
            payload = self.relation_handler(prompt)
            self.calls.append(("relation", prompt))
        else:
            raise AssertionError("无法识别的 prompt 段落")
        return json.dumps(payload, ensure_ascii=False)


def catalog_ids(prompt: str) -> dict[str, dict[str, str]]:
    """从关系 pass prompt 的目录表解析 id -> {type, name}。"""
    entries: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"\|\s*`(entity-\d+)`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|", prompt):
        entries[match.group(1)] = {"type": match.group(2), "name": match.group(3).strip()}
    return entries


def prompt_chunk_text(prompt: str) -> str:
    """从实体 pass prompt 中提取“当前 chunk”正文，避免局部上下文干扰路由。"""
    marker = "## 待抽取文本（当前 chunk）"
    rest = prompt.split(marker, 1)[1]
    return rest.split("\n## ", 1)[0].strip()


def composite_entities() -> dict:
    return {
        "entities": [
            {
                "type": "CompositeRule",
                "name": "CR-001",
                "properties": {
                    "ruleId": "CR-001",
                    "ruleText": COMPOSITE_TEXT,
                    "combinationType": "AND",
                },
                "evidence": {"quote": COMPOSITE_TEXT},
            },
            {
                "type": "SimpleRule",
                "name": "R-001",
                "properties": {
                    "ruleId": "R-001",
                    "ruleText": "未经本企业充分论证和安全测试，不得部署安装各类开源智能体。",
                    "triggerCondition": "未经本企业充分论证和安全测试",
                    "action": "不得部署安装各类开源智能体",
                },
                "evidence": {"quote": "未经本企业充分论证和安全测试"},
            },
            {
                "type": "SimpleRule",
                "name": "R-002",
                "properties": {
                    "ruleId": "R-002",
                    "ruleText": "未经集团批准，不得部署安装各类开源智能体。",
                    "triggerCondition": "未经集团批准",
                    "action": "不得部署安装各类开源智能体",
                },
                "evidence": {"quote": "未经集团批准"},
            },
        ],
        "relations": [],
    }


def composite_relations(prompt: str) -> dict:
    entries = catalog_ids(prompt)
    composite = next(item for item in entries.values() if item["type"] == "CompositeRule")
    simples = [item for item in entries.values() if item["type"] == "SimpleRule"]
    composite_id = next(id_ for id_, item in entries.items() if item is composite)
    simple_ids = [id_ for id_, item in entries.items() if item in simples]
    return {
        "relations": [
            {
                "from_id": composite_id,
                "rel_type": "hasSubRule",
                "to_id": simple_id,
                "evidence": {"quote": "未经本企业充分论证和安全测试" if index == 0 else "未经集团批准"},
            }
            for index, simple_id in enumerate(simple_ids)
        ]
    }


class PromptTest(unittest.TestCase):
    def test_entity_prompt_scope_and_features_no_relations(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY, target_types=["Rule"])
        prompt = SE.build_entity_prompt(plan, COMPOSITE_TEXT, document_type="general")
        self.assertIn("实体候选提议", prompt)
        self.assertIn("SimpleRule", prompt)  # 父类子类展开
        self.assertIn("ruleId", prompt)
        self.assertNotIn("hasSubRule", prompt)  # 实体 pass 不提供关系

    def test_relation_prompt_catalog_and_signatures(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        catalog = [
            {"id": "entity-0", "type": "CompositeRule", "name": "CR-001", "properties": {"ruleId": "CR-001"}},
            {"id": "entity-1", "type": "SimpleRule", "name": "R-001", "properties": {"ruleId": "R-001"}},
        ]
        prompt = SE.build_relation_prompt(plan, catalog, COMPOSITE_TEXT)
        self.assertIn("entity-0", prompt)
        self.assertIn("hasSubRule", prompt)
        self.assertIn("禁止输出实体", prompt)


class TwoPassPipelineTest(unittest.TestCase):
    def test_end_to_end_composite_rule(self) -> None:
        llm = StubLLM(entity_handler=lambda prompt: composite_entities(), relation_handler=composite_relations)
        result = SE.extract_split(
            RULE_ONTOLOGY,
            text=COMPOSITE_TEXT,
            llm=llm,
            document_type="general",
        )
        raw = result["raw"]
        self.assertEqual([entity["type"] for entity in raw["entities"]], ["CompositeRule", "SimpleRule", "SimpleRule"])
        self.assertEqual([entity["name"] for entity in raw["entities"]], ["CR-001", "R-001", "R-002"])
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
        self.assertEqual(model["objects"][0]["identity"], "CR-001")
        # 证据覆盖
        self.assertEqual(result["evidence_coverage"]["entities"]["rate"], 1.0)
        self.assertEqual(result["evidence_coverage"]["relations"]["rate"], 1.0)
        # 两段式：实体 pass 1 次 + 关系 pass 1 次
        self.assertEqual([call for call, _ in llm.calls], ["entity", "relation"])
        # 无 schema/证据告警
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(result["error_code"])

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
            CC.ExtractionChunk(chunk_id="c1", text="开展关键主数据治理，建立健全主数据识别、采集、校验机制。"),
            CC.ExtractionChunk(chunk_id="c2", text="主数据治理也称主数据管理（MDM）。"),
        ]

        def entity_handler(prompt: str) -> dict:
            if "主数据管理（MDM）" in prompt_chunk_text(prompt):
                return {
                    "entities": [
                        {
                            "type": "Terminology",
                            "name": "主数据治理",
                            "properties": {"TermName": "主数据治理", "Synonyms": ["主数据管理"]},
                            "evidence": {"quote": "主数据治理也称主数据管理"},
                        }
                    ]
                }
            else:
                return {
                    "entities": [
                        {
                            "type": "Terminology",
                            "name": "主数据治理",
                            "properties": {
                                "TermName": "主数据治理",
                                "TermDefinition": "对主数据的识别、采集、校验等治理活动",
                            },
                            "evidence": {"quote": "开展关键主数据治理"},
                        }
                    ]
                }

        llm = StubLLM(entity_handler=entity_handler)
        result = SE.extract_split(
            terminology_ontology,
            chunks=chunks,
            llm=llm,
            document_type="general",
        )
        entities = result["raw"]["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["candidate_id"], "entity-0")
        self.assertEqual(entities[0]["properties"]["TermDefinition"], "对主数据的识别、采集、校验等治理活动")
        self.assertEqual(entities[0]["properties"]["Synonyms"], ["主数据管理"])
        # 两条 evidence（不同 chunk span）都保留
        self.assertEqual(len(result["intermediate_model"]["objects"][0]["evidence"]), 2)

    def test_out_of_scope_entity_rejected(self) -> None:
        official = (ONTOLOGIES_DIR / "official_document_ontology.puml").read_text(encoding="utf-8")
        text = "五、加强网络和数据安全监测，一旦发现或获知开源智能体引发的本企业安全事件，第一时间处置，并向集团报告。"

        def entity_handler(prompt: str) -> dict:
            return {
                "entities": [
                    {"type": "OperationalActivity", "name": "加强网络和数据安全监测", "properties": {}, "evidence": {"quote": "加强网络和数据安全监测"}},
                    {"type": "OperationalAgent", "name": "各企业", "properties": {}, "evidence": {"quote": "各企业"}},
                ]
            }

        llm = StubLLM(entity_handler=entity_handler)
        result = SE.extract_split(
            official,
            text=text,
            llm=llm,
            target_types=["OperationalActivity"],
            document_type="official_document",
        )
        self.assertEqual([entity["type"] for entity in result["raw"]["entities"]], ["OperationalActivity"])
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("ENTITY_OUT_OF_SCOPE", codes)
        self.assertNotIn("ENTITY_OUT_OF_SCOPE", [entity.get("type") for entity in result["raw"]["entities"]])


class RelationValidationTest(unittest.TestCase):
    def _simple_rule_scene(self) -> tuple[str, dict, dict]:
        text = "禁止计算机等办公设备内、外网混用。未经集团批准，不得部署安装各类开源智能体。"
        entities = {
            "entities": [
                {
                    "type": "SimpleRule",
                    "name": "R-001",
                    "properties": {
                        "ruleId": "R-001",
                        "ruleText": "未经集团批准，不得部署安装各类开源智能体。",
                        "triggerCondition": "未经集团批准",
                        "action": "不得部署安装各类开源智能体",
                    },
                    "evidence": {"quote": "未经集团批准，不得部署安装各类开源智能体。"},
                },
                {
                    "type": "Object",
                    "name": "办公设备",
                    "properties": {"objectName": "办公设备"},
                    "evidence": {"quote": "办公设备"},
                },
            ],
            "relations": [],
        }
        return text, entities

    def test_unknown_endpoint_rejected(self) -> None:
        text, entities = self._simple_rule_scene()
        llm = StubLLM(
            entity_handler=lambda prompt: entities,
            relation_handler=lambda prompt: {
                "relations": [
                    {"from_id": "entity-99", "rel_type": "constrainsObject", "to_id": "entity-1", "evidence": {"quote": "办公设备"}}
                ]
            },
        )
        result = SE.extract_split(RULE_ONTOLOGY, text=text, llm=llm)
        self.assertEqual(result["raw"]["relations"], [])
        self.assertEqual(len(result["graph"]["relationships"]), 0)
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("UNKNOWN_RELATION_ENDPOINT_ID", codes)

    def test_relation_type_mismatch_rejected(self) -> None:
        text, entities = self._simple_rule_scene()
        llm = StubLLM(
            entity_handler=lambda prompt: entities,
            relation_handler=lambda prompt: {
                "relations": [
                    {"from_id": "entity-0", "rel_type": "hasSubRule", "to_id": "entity-1", "evidence": {"quote": "未经集团批准"}}
                ]
            },
        )
        result = SE.extract_split(RULE_ONTOLOGY, text=text, llm=llm)
        self.assertEqual(result["raw"]["relations"], [])
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("RELATION_ENDPOINT_TYPE_MISMATCH", codes)

    def test_unknown_relation_type_rejected(self) -> None:
        text, entities = self._simple_rule_scene()
        llm = StubLLM(
            entity_handler=lambda prompt: entities,
            relation_handler=lambda prompt: {
                "relations": [
                    {"from_id": "entity-0", "rel_type": "NOT_IN_ONTOLOGY", "to_id": "entity-1", "evidence": {"quote": "办公设备"}}
                ]
            },
        )
        result = SE.extract_split(RULE_ONTOLOGY, text=text, llm=llm)
        self.assertEqual(result["raw"]["relations"], [])
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("UNKNOWN_RELATION_TYPE", codes)


class FailureAndEdgeTest(unittest.TestCase):
    def test_json_parse_failure_recorded(self) -> None:
        llm = StubLLM(
            entity_handler=lambda prompt: "not json",
            relation_handler=lambda prompt: "not json",
        )
        result = SE.extract_split(RULE_ONTOLOGY, text=COMPOSITE_TEXT, llm=llm)
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        self.assertEqual(len(result["attempts"]), 2)
        self.assertTrue(all(attempt["type"] == "json" for attempt in result["attempts"]))
        self.assertEqual(result["error_code"], "LLM_JSON_PARSE_FAILED")

    def test_request_failure_recorded(self) -> None:
        def boom(prompt: str) -> str:
            raise RuntimeError("network down")

        llm = StubLLM(entity_handler=boom, relation_handler=lambda prompt: {"relations": []})
        result = SE.extract_split(RULE_ONTOLOGY, text=COMPOSITE_TEXT, llm=llm)
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        self.assertEqual(result["attempts"][0]["type"], "request")
        self.assertEqual(result["error_code"], "LLM_REQUEST_FAILED")

    def test_empty_input(self) -> None:
        llm = StubLLM()
        result = SE.extract_split(RULE_ONTOLOGY, llm=llm)
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("EMPTY_INPUT", codes)
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        self.assertEqual(result["profile"]["chunks"], 0)

    def test_uid_stable_regardless_of_properties(self) -> None:
        plan = SE.compile_plan(RULE_ONTOLOGY)
        entities = [
            SE.EntityCandidate(
                candidate_id="entity-0",
                proposed_type="SimpleRule",
                canonical_type="SimpleRule",
                name="R-001",
                properties={"ruleId": "R-001", "action": "A"},
                evidence=[],
                source="",
            ),
            SE.EntityCandidate(
                candidate_id="entity-1",
                proposed_type="SimpleRule",
                canonical_type="SimpleRule",
                name="R-001",
                properties={"ruleId": "R-001", "action": "B"},
                evidence=[],
                source="",
            ),
        ]
        graph = SE._build_graph(entities, [])
        self.assertEqual(len(graph["nodes"]), 1)  # 同一 identity 合并为同一 UID
        self.assertEqual(
            graph["nodes"][0]["uid"],
            MODULES["graph_utils"].entity_uid("SimpleRule", "R-001", None),
        )


class BenchmarkSmokeTest(unittest.TestCase):
    def test_ontology_class_extraction_gold(self) -> None:
        case = json.loads((CASES_DIR / "04_ontology_class_extraction.json").read_text(encoding="utf-8"))
        ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")

        def entity_handler(prompt: str) -> dict:
            return {"entities": case["expected"]["entities"]}

        llm = StubLLM(entity_handler=entity_handler)
        result = SE.extract_split(
            ontology,
            text=case["text"],
            llm=llm,
            target_types=case["target_types"],
            include_subclasses=case["include_subclasses"],
            document_type=case["document_type"],
            validation_mode=case["validation_mode"],
        )
        gold_keys = {(e["type"], e["name"]) for e in case["expected"]["entities"]}
        pred_keys = {(e["type"], e["name"]) for e in result["raw"]["entities"]}
        self.assertEqual(pred_keys, gold_keys)
        self.assertEqual(result["evidence_coverage"]["entities"]["rate"], 1.0)
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertNotIn("ENTITY_OUT_OF_SCOPE", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
