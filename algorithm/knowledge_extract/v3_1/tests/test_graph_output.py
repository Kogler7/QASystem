"""Current extraction output compatibility and graph consistency tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PACKAGE_DIR / "tests" / "fixtures"
ONTOLOGIES_DIR = FIXTURES_DIR / "ontologies"
CASES_DIR = FIXTURES_DIR / "cases"
TESTS_DIR = PACKAGE_DIR / "tests"


from .. import graph_utils as GU
from .. import split_extractor as SE

from .stub_llm import StubLLM, parse_known_entities, span_item  # noqa: E402


class IntegrationTest(unittest.TestCase):
    def test_result_contract_and_graph_consistency(self) -> None:
        case = json.loads((CASES_DIR / "06_composite_rule.json").read_text(encoding="utf-8"))
        ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")
        text = case["text"]
        # 新管线为 span-first：实体名 = 原文 mention（gold 中的 CR-001/R-001 等
        # 编号不在原文中，属性补全也要求标识类属性逐字可定位，因此按 mention 断言）。
        composite_mention = "未经本企业充分论证和安全测试，未经集团批准，不得部署安装各类开源智能体。"
        member_mentions = ["未经本企业充分论证和安全测试", "未经集团批准"]
        member_rule_texts = [
            "未经本企业充分论证和安全测试，不得部署安装各类开源智能体。",
            "未经集团批准，不得部署安装各类开源智能体。",
        ]

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            return [
                span_item("CompositeRule", composite_mention),
                span_item("SimpleRule", member_mentions[0]),
                span_item("SimpleRule", member_mentions[1]),
            ]

        def property_handler(prompt: str, chunk: dict) -> dict:
            updates = []
            for row in parse_known_entities(prompt):
                if row["type"] == "CompositeRule":
                    properties = {"ruleText": composite_mention, "combinationType": "AND"}
                else:
                    index = member_mentions.index(row["name"].strip())
                    properties = {
                        "ruleText": member_rule_texts[index],
                        "triggerCondition": member_mentions[index],
                        "action": "不得部署安装各类开源智能体",
                    }
                updates.append(
                    {
                        "type": row["type"], "name": row["name"],
                        "properties": properties,
                        "evidence": {"quote": composite_mention},
                    }
                )
            return {"entities": updates}


        llm = StubLLM(
            entity_handler=entity_handler,
            property_handler=property_handler,
        )
        result = SE.extract_split(
            ontology,
            chunks=[SE.ExtractionChunk(chunk_id="chunk-0", text=text)],
            llm=llm,
        )
        # 保留现有 API + 新字段
        for key in ("raw", "graph", "warnings", "attempts", "profile", "error_code"):
            self.assertIn(key, result)
        for key in ("triples", "intermediate_model", "evidence_coverage", "coverage_gaps", "mapping"):
            self.assertIn(key, result)
        # raw 实体：1 CompositeRule + 2 SimpleRule，均带 hasSubRule
        canonical = GU.canonical_name
        pred_entity_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        self.assertEqual(
            pred_entity_keys,
            {
                ("CompositeRule", canonical(composite_mention)),
                ("SimpleRule", canonical(member_mentions[0])),
                ("SimpleRule", canonical(member_mentions[1])),
            },
        )
        pred_relation_keys = {
            (r["from_type"], r["rel_type"], r["to_type"]) for r in result["raw"]["relations"]
        }
        self.assertEqual(pred_relation_keys, set())
        self.assertEqual(result["raw"]["relations"], [])
        # graph 校验：无悬空、uid 可复算、triples 一致
        node_uids = {node["uid"] for node in result["graph"]["nodes"]}
        for entity in result["raw"]["entities"]:
            self.assertIn(
                GU.entity_uid(entity["type"], entity["name"], None),
                node_uids,
            )
        relationships = result["graph"]["relationships"]
        self.assertEqual(len(result["triples"]), len(relationships))
        self.assertEqual(
            {(t["head"], t["relation"], t["tail"]) for t in result["triples"]},
            {(r["from_uid"], r["type"], r["to_uid"]) for r in relationships},
        )
        # 兼容既有批量合并消费者：关系端点全部在节点集内
        kept = GU.filter_relationships_with_known_nodes(relationships, node_uids)
        self.assertEqual(len(kept), len(relationships))


if __name__ == "__main__":
    unittest.main(verbosity=2)
