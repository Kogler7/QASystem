"""v0：rule 文档路由与规则抽取 prompt 扩展。

运行（仓库根目录，需 openai 可导入）：
    python3 -m pytest algorithm/knowledge_extract/v0/tests/test_rule_route.py -q

覆盖：
- 规则本体（test_data/eval/rule/ontology/rule.puml）把 rule 域打到阈值以上；
- 故障记录表 chunk → 路由到 ``rule``，并挂上 ``RULE_PROMPT_EXTENSION``；
- 该扩展真的进入 system prompt（抽取时对模型生效）；
- 扩展文本覆盖「修正 rule_01 golden」时确立的评审要点
  （connectionSymbol 规范取值、处置流程不算复合条件、同码冲突只出一条、重叠合并、原因/处理列也要出规则、无标识留空）；
- 非规则文档不会被误判成 rule。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from algorithm.knowledge_extract.v0.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v0.ontology_compiler import OntologyCompiler
from algorithm.knowledge_extract.v0.prompt_router import (
    RULE_PROMPT_EXTENSION,
    build_chat_messages,
    resolve_document_route,
)

ROOT = Path(__file__).resolve().parents[4]
RULE_ONTOLOGY_PATH = ROOT / "test_data/eval/rule/ontology/rule.puml"

RULE_TABLE_TEXT = (
    "## Sheet1\n\n```json\n"
    '{"columns": ["序号", "故障状况", "故障原因", "处理方法", "故障详细分类"], '
    '"rows": ['
    '{"序号": "1", "故障状况": "屏蔽门没有联动", "故障原因": "CPU850工作不稳定", "处理方法": "更换CPU850", "故障详细分类": "屏蔽门不能联动"}, '
    '{"序号": "2", "故障状况": "轨道区段粉红光带", "故障原因": "接收1板不稳定", "处理方法": "更换接收1板", "故障详细分类": "轨道电路"}'
    "]}\n```\n"
)
NEUTRAL_ONTOLOGY = """
@startuml neutral
class "文档" <<Document>> {
  - 标题 (Title) //文档标题
}
@enduml
"""


def _plan(ontology: str):
    return OntologyCompiler().compile(ontology=ontology)


@unittest.skipUnless(RULE_ONTOLOGY_PATH.exists(), "缺少评测本体: %s" % RULE_ONTOLOGY_PATH)
class RuleRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rule_ontology = RULE_ONTOLOGY_PATH.read_text(encoding="utf-8")
        cls.chunk = ExtractionChunk(chunk_id="chunk-0001", text=RULE_TABLE_TEXT)

    def test_rule_ontology_scores_rule_domain(self) -> None:
        plan = _plan(self.rule_ontology)
        route = resolve_document_route(plan, [self.chunk])
        self.assertEqual(route.kind, "rule")
        scores = dict(route.scores)
        self.assertGreater(scores["rule"], scores.get("official_document", 0))
        self.assertGreater(scores["rule"], scores.get("standard", 0))

    def test_rule_route_carries_extension(self) -> None:
        plan = _plan(self.rule_ontology)
        route = resolve_document_route(plan, [self.chunk])
        self.assertIn("RULE_PROMPT_EXTENSION", route.prompt_extension)
        self.assertIn("处置后恢复", route.prompt_extension)

    def test_extension_reaches_system_prompt(self) -> None:
        plan = _plan(self.rule_ontology)
        route = resolve_document_route(plan, [self.chunk])
        messages = build_chat_messages(task="entity", plan=plan, route=route, chunk=self.chunk)
        system = "\n".join(item["content"] for item in messages if item["role"] == "system")
        self.assertIn("连接词（connectionSymbol）", system)
        self.assertIn("规则抽取规则", system)

    def test_extension_covers_review_points(self) -> None:
        """扩展必须覆盖修正 rule_01 golden 时确立的评审要点。"""
        for marker in (
            "导致",            # connectionSymbol 规范取值
            "处置后恢复",
            "指示",
            "ImplicationRuleType",
            "CompositeRuleType",
            "处置流程不是多条件组合",
            "只输出一条规则",
            "合并为一条",
            "故障原因列与处理方法列同样是规则来源",
            "原文没有唯一标识时必须留空",
        ):
            self.assertIn(marker, RULE_PROMPT_EXTENSION, "规则扩展缺少要点: %s" % marker)

    def test_neutral_document_not_routed_as_rule(self) -> None:
        plan = _plan(NEUTRAL_ONTOLOGY)
        chunk = ExtractionChunk(chunk_id="chunk-0002", text="本文档说明了系统的基本操作流程与注意事项。")
        route = resolve_document_route(plan, [chunk])
        self.assertNotEqual(route.kind, "rule")


if __name__ == "__main__":
    unittest.main(verbosity=2)
