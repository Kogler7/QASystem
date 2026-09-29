from __future__ import annotations

import json
import unittest
from pathlib import Path

from algorithm.knowledge_extract_v2.candidate_generation import entity_pass
from algorithm.knowledge_extract_v2.chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from algorithm.knowledge_extract_v2.ontology_compiler import compile_plan
from algorithm.knowledge_extract_v2.ontology_schema import OntologySchema
from algorithm.knowledge_extract_v2.prompt_router import DocumentRoute, build_chat_messages
from algorithm.knowledge_extract_v2.table_semantics import analyze_table_semantics
from algorithm.knowledge_extract_v2.split_extractor import extract_split
from algorithm.knowledge_extract_v2.tests.stub_llm import (
    StubLLM,
    current_chunk,
    parse_known_entities,
    span_item,
    user_prompt,
)


ROOT = Path(__file__).resolve().parents[3]
RULE_ONTOLOGY = (ROOT / "offline_ingestion/规则/ontology/rule.puml").read_text(
    encoding="utf-8"
)

DEVICE_ONTOLOGY = """
@startuml
class "设备" as Equipment {
  + 设备编号 (equipmentId): String
  + 名称 (name): String
}
' @identity Equipment: equipmentId
@enduml
"""

MIXED_ONTOLOGY = DEVICE_ONTOLOGY + """
class "规则" as Rule {
  + 规则编号 (ruleId): String
  + 规则内容 (ruleText): String
}
class "简单规则" as SimpleRule {
  + 规则编号 (ruleId): String
  + 触发条件 (triggerCondition): String
  + 动作 (action): String
}
Rule <|-- SimpleRule
' @identity Rule: ruleId
' @identity SimpleRule: ruleId
@enduml
"""

PARAMETER_ONTOLOGY = MIXED_ONTOLOGY + """
class "参数" as Parameter {
  + 参数名称 (parameterName): String
  + 参数值 (parameterValue): String
}
' @identity Parameter: parameterName
@enduml
"""

RULE_ENGINE_ONTOLOGY = """
@startuml
class "规则引擎" as RuleEngine {
  + 引擎名称 (engineName): String
}
' @identity RuleEngine: engineName
@enduml
"""

INHERITED_MIXED_ONTOLOGY = """
@startuml
abstract class "实体" as Entity
class "设备" as Equipment {
  + 设备编号 (equipmentId): String
}
class "规则" as Rule {
  + 规则编号 (ruleId): String
}
Entity <|-- Equipment
Entity <|-- Rule
' @identity Equipment: equipmentId
' @identity Rule: ruleId
@enduml
"""


def _table(columns: list[str], rows: list[dict[str, object]], *, title: str = "") -> str:
    payload = json.dumps({"columns": columns, "rows": rows}, ensure_ascii=False)
    prefix = f"## {title}\n\n" if title else ""
    return f"{prefix}```json\n{payload}\n```"


def _chunk(text: str, **metadata: object) -> ExtractionChunk:
    return ExtractionChunk("table-1", text, metadata=dict(metadata))


class TableSemanticsTests(unittest.TestCase):
    def test_non_structured_text_is_facts_without_rule_rows(self) -> None:
        result = analyze_table_semantics(
            compile_plan(DEVICE_ONTOLOGY), _chunk("设备 RTU 安装在 1 号站。")
        )

        self.assertEqual(result.mode, "facts")
        self.assertEqual(result.rule_rows, ())

    def test_device_only_ontology_does_not_turn_table_into_rules(self) -> None:
        text = _table(
            ["设备编号", "名称", "类别"],
            [{"设备编号": "E-1", "名称": "RTU", "类别": "控制设备"}],
        )
        result = analyze_table_semantics(compile_plan(DEVICE_ONTOLOGY), _chunk(text))

        self.assertEqual(result.mode, "facts")
        self.assertEqual(result.rule_rows, ())
        self.assertEqual(result.rule_types, ())

    def test_rule_ontology_requires_condition_and_action(self) -> None:
        text = _table(
            ["规则编号", "分类", "触发条件", "动作"],
            [
                {
                    "规则编号": "R-1",
                    "分类": "温度控制",
                    "触发条件": "温度超过 80℃",
                    "动作": "停止设备",
                }
            ],
        )
        result = analyze_table_semantics(compile_plan(RULE_ONTOLOGY), _chunk(text))

        self.assertEqual(result.mode, "rules")
        self.assertEqual(result.rule_rows, ((1, 1),))
        self.assertTrue(result.rule_types)
        self.assertTrue(set(result.rule_types) <= set(compile_plan(RULE_ONTOLOGY).allowed_types))

    def test_rule_id_or_classification_alone_does_not_enable_rules(self) -> None:
        text = _table(
            ["RuleID", "分类", "名称"],
            [{"RuleID": "R-1", "分类": "温度控制", "名称": "高温"}],
        )
        result = analyze_table_semantics(compile_plan(RULE_ONTOLOGY), _chunk(text))

        self.assertNotEqual(result.mode, "rules")
        self.assertEqual(result.rule_rows, ())

    def test_missing_rule_id_still_keeps_rule_semantics(self) -> None:
        text = _table(
            ["故障现象", "原因", "处置"],
            [{"故障现象": "设备掉线", "原因": "通信中断", "处置": "重启通信模块"}],
        )
        result = analyze_table_semantics(compile_plan(RULE_ONTOLOGY), _chunk(text))

        self.assertEqual(result.mode, "rules")
        self.assertEqual(result.rule_rows, ((1, 1),))

    def test_target_types_excluding_rule_suppresses_rule_mode(self) -> None:
        plan = compile_plan(MIXED_ONTOLOGY, target_types=["Equipment"])
        text = _table(
            ["触发条件", "动作", "设备编号"],
            [{"触发条件": "温度过高", "动作": "停机", "设备编号": "E-1"}],
        )

        result = analyze_table_semantics(plan, _chunk(text))

        self.assertEqual(result.mode, "facts")
        self.assertEqual(result.rule_rows, ())

    def test_same_chunk_tables_are_classified_by_table_and_row(self) -> None:
        equipment = _table(
            ["设备编号", "名称"], [{"设备编号": "E-1", "名称": "RTU"}], title="设备"
        )
        rules = _table(
            ["条件", "结果"], [{"条件": "电压低", "结果": "告警"}], title="规则"
        )
        result = analyze_table_semantics(
            compile_plan(MIXED_ONTOLOGY), _chunk(equipment + "\n\n" + rules)
        )

        self.assertEqual(result.mode, "mixed")
        self.assertEqual(result.rule_rows, ((2, 1),))

    def test_mixed_rows_only_mark_rule_rows(self) -> None:
        text = _table(
            ["设备编号", "名称", "触发条件", "结果"],
            [
                {"设备编号": "E-1", "名称": "RTU"},
                {"设备编号": "E-2", "名称": "PLC", "触发条件": "温度高", "结果": "停机"},
            ],
        )
        result = analyze_table_semantics(compile_plan(MIXED_ONTOLOGY), _chunk(text))

        self.assertEqual(result.mode, "mixed")
        self.assertEqual(result.rule_rows, ((1, 2),))

    def test_empty_table_does_not_enable_rules(self) -> None:
        text = _table(["RuleID", "触发条件", "动作"], [])
        result = analyze_table_semantics(compile_plan(RULE_ONTOLOGY), _chunk(text))

        self.assertNotEqual(result.mode, "rules")
        self.assertEqual(result.rule_rows, ())

    def test_parameter_table_with_result_column_is_still_facts(self) -> None:
        plan = compile_plan(PARAMETER_ONTOLOGY, target_types=["Parameter", "Rule"])
        text = _table(
            ["参数名称", "参数值", "单位", "结果"],
            [{"参数名称": "额定电压", "参数值": "220", "单位": "V", "结果": "合格"}],
        )

        result = analyze_table_semantics(plan, _chunk(text))

        self.assertEqual(result.mode, "facts")
        self.assertEqual(result.rule_rows, ())

    def test_official_form_fields_and_conditional_text_are_not_rules(self) -> None:
        plan = compile_plan(MIXED_ONTOLOGY, target_types=["Equipment", "Rule"])
        text = _table(
            ["收文号", "来文字号", "来文单位", "拟办意见"],
            [{"收文号": "收-1", "来文字号": "来-2", "来文单位": "技术中心", "拟办意见": "若无异议，请阅办"}],
            title="收文处理单",
        )

        result = analyze_table_semantics(
            plan,
            _chunk(text, source_file="收文处理单.xlsx"),
        )

        self.assertEqual(result.mode, "facts")
        self.assertEqual(result.rule_rows, ())

    def test_measurement_condition_result_values_are_not_rule_evidence(self) -> None:
        plan = compile_plan(PARAMETER_ONTOLOGY, target_types=["Parameter", "Rule"])
        text = _table(
            ["测量对象", "条件", "结果"],
            [{"测量对象": "温度传感器", "条件": "温度", "结果": "25℃ / 220V"}],
        )

        result = analyze_table_semantics(plan, _chunk(text))

        self.assertEqual(result.mode, "facts")
        self.assertEqual(result.rule_rows, ())

    def test_rule_engine_type_name_is_not_rule_ontology_semantics(self) -> None:
        text = _table(
            ["条件", "动作"], [{"条件": "温度高", "动作": "停机"}]
        )

        result = analyze_table_semantics(compile_plan(RULE_ENGINE_ONTOLOGY), _chunk(text))

        self.assertEqual(result.mode, "facts")
        self.assertEqual(result.rule_rows, ())

    def test_generic_test_status_pair_needs_a_predicate(self) -> None:
        text = _table(["设备", "条件", "结果"], [{"设备": "RTU", "条件": "电压", "结果": "合格"}])
        result = analyze_table_semantics(compile_plan(MIXED_ONTOLOGY), _chunk(text))
        self.assertEqual(result.mode, "uncertain")
        self.assertEqual(result.rule_rows, ())

    def test_single_approval_field_is_not_a_diagnostic_rule(self) -> None:
        text = _table(["拟办意见"], [{"拟办意见": "若无异议则应于周五报送"}])
        result = analyze_table_semantics(compile_plan(MIXED_ONTOLOGY), _chunk(text))
        self.assertEqual(result.mode, "facts")
        self.assertIn("source.official_form", result.reasons)

    def test_vertical_pdf_form_normalizes_radicals_and_label_punctuation(self) -> None:
        text = _table(["收⽂号：", "信息公司收〔2025〕195号"], [
            {"收⽂号：": "来⽂单位：", "信息公司收〔2025〕195号": "信息公司"},
            {"收⽂号：": "拟办意⻅：*", "信息公司收〔2025〕195号": "若无异议则应报送"},
        ])
        result = analyze_table_semantics(compile_plan(MIXED_ONTOLOGY), _chunk(text))
        self.assertEqual(result.mode, "facts")
        self.assertIn("source.official_form", result.reasons)

    def test_ordinary_mixed_ontology_table_removes_rule_from_entity_catalog(self) -> None:
        plan = compile_plan(MIXED_ONTOLOGY)
        chunk = _chunk(_table(["设备编号", "名称"], [{"设备编号": "E-1", "名称": "RTU"}]))
        messages = build_chat_messages(
            task="entity",
            plan=plan,
            route=DocumentRoute(kind="generic", source="fallback"),
            chunk=chunk,
        )
        prompt = "\n".join(message["content"] for message in messages)

        self.assertNotIn("-> type `Rule`", prompt)
        self.assertNotIn("-> type `SimpleRule`", prompt)

    def test_equipment_known_type_suppresses_rule_property_instructions(self) -> None:
        plan = compile_plan(MIXED_ONTOLOGY)
        chunk = _chunk(
            _table(["条件", "动作"], [{"条件": "温度高", "动作": "停机"}])
        )
        messages = build_chat_messages(
            task="property",
            plan=plan,
            route=DocumentRoute(kind="generic", source="fallback"),
            chunk=chunk,
            known_entities=[{"id": "equipment-1", "type": "Equipment", "name": "RTU"}],
        )
        prompt = "\n".join(message["content"] for message in messages)

        self.assertNotIn("规则模板按", prompt)

    def test_real_rule_in_body_does_not_remove_rule_from_entity_catalog(self) -> None:
        plan = compile_plan(MIXED_ONTOLOGY)
        chunk = _chunk(
            "若温度超过80℃，则停机。\n\n"
            + _table(["设备编号", "名称"], [{"设备编号": "E-1", "名称": "RTU"}])
        )
        messages = build_chat_messages(
            task="entity",
            plan=plan,
            route=DocumentRoute(kind="generic", source="fallback"),
            chunk=chunk,
        )
        prompt = "\n".join(message["content"] for message in messages)

        self.assertIn("-> type `Rule`", prompt)

    def test_entity_pass_discards_model_rule_candidate_from_ordinary_table(self) -> None:
        ontology = MIXED_ONTOLOGY
        text = _table(["设备编号", "名称"], [{"设备编号": "E-1", "名称": "RTU"}])
        entity_calls = 0

        def entity_handler(_prompt: str, _chunk: dict[str, object]) -> list[dict[str, object]]:
            nonlocal entity_calls
            entity_calls += 1
            # The first response violates the table-specific schema.  The retry
            # is a valid response, matching the strict JSON-schema contract.
            return [span_item("Rule", "RTU")] if entity_calls == 1 else [span_item("Equipment", "RTU")]

        def identity_handler(_prompt: str, chunk: dict[str, object]) -> dict[str, object]:
            return {
                "updates": [
                    {
                        "entity_id": "entity-0",
                        "properties": {"equipmentId": "E-1"},
                        "evidence": {"quote": "E-1"},
                    }
                ]
            }

        result = extract_split(
            ontology,
            chunks=[ExtractionChunk("ordinary-table", text)],
            llm=StubLLM(entity_handler=entity_handler, identity_handler=identity_handler),
        )

        self.assertEqual(
            {(item["type"], item["name"]) for item in result["raw"]["entities"]},
            {("Equipment", "RTU")},
        )
        self.assertGreaterEqual(entity_calls, 2)
        self.assertTrue(any(item.get("type") == "schema" for item in result["attempts"]))
        self.assertNotIn("Rule", {item["type"] for item in result["raw"]["entities"]})

    def test_entity_pass_does_not_admit_rule_sibling_via_common_entity_parent(self) -> None:
        text = _table(["设备编号"], [{"设备编号": "E-1"}])
        entity_calls = 0

        def entity_handler(_prompt: str, _chunk: dict[str, object]) -> list[dict[str, object]]:
            nonlocal entity_calls
            entity_calls += 1
            return [span_item("Rule", "E-1")] if entity_calls == 1 else [span_item("Equipment", "E-1")]

        def identity_handler(_prompt: str, _chunk: dict[str, object]) -> dict[str, object]:
            return {
                "updates": [
                    {
                        "entity_id": "entity-0",
                        "properties": {"equipmentId": "E-1"},
                        "evidence": {"quote": "E-1"},
                    }
                ]
            }

        result = extract_split(
            INHERITED_MIXED_ONTOLOGY,
            chunks=[ExtractionChunk("ordinary-table", text)],
            target_types=["Equipment"],
            llm=StubLLM(entity_handler=entity_handler, identity_handler=identity_handler),
        )

        self.assertEqual(
            {(item["type"], item["name"]) for item in result["raw"]["entities"]},
            {("Equipment", "E-1")},
        )
        self.assertGreaterEqual(entity_calls, 2)

    def test_entity_pass_gates_rule_candidates_by_mixed_table_row(self) -> None:
        text = _table(
            ["设备编号", "名称", "触发条件", "动作"],
            [
                {"设备编号": "E-1", "名称": "RTU"},
                {"设备编号": "E-2", "名称": "PLC", "触发条件": "温度高", "动作": "停机"},
            ],
        )

        def entity_handler(_prompt: str, _chunk: dict[str, object]) -> list[dict[str, object]]:
            return [
                span_item("Rule", "RTU"),
                span_item("Rule", "PLC"),
                span_item("Equipment", "RTU"),
            ]

        def identity_handler(prompt: str, _chunk: dict[str, object]) -> dict[str, object]:
            updates = []
            for row in parse_known_entities(prompt):
                type_id = row["type"]
                updates.append(
                    {
                        "entity_id": row["id"],
                        "properties": {"equipmentId" if type_id == "Equipment" else "ruleId": row["name"]},
                        "evidence": {"quote": row["name"]},
                    }
                )
            return {"updates": updates}

        result = extract_split(
            MIXED_ONTOLOGY,
            chunks=[ExtractionChunk("mixed-table", text)],
            llm=StubLLM(entity_handler=entity_handler, identity_handler=identity_handler),
        )
        entities = {(item["type"], item["name"]) for item in result["raw"]["entities"]}

        self.assertIn(("Equipment", "RTU"), entities)
        self.assertIn(("Rule", "PLC"), entities)
        self.assertNotIn(("Rule", "RTU"), entities)
        self.assertIn(
            "RULE_TABLE_ROW_OUT_OF_SCOPE",
            {warning["code"] for warning in result["warnings"]},
        )

    def test_entity_pass_preserves_same_name_rule_span_from_allowed_second_row(self) -> None:
        text = _table(
            ["名称", "触发条件", "动作"],
            [
                {"名称": "同名"},
                {"名称": "同名", "触发条件": "温度高", "动作": "停机"},
            ],
        )
        first = text.index("同名")
        second = text.index("同名", first + 1)

        class SameNameSpanLLM:
            def __call__(self, _messages: object) -> str:
                return json.dumps(
                    {
                        "entities": [
                            {
                                "type": "Rule",
                                "mention": "同名",
                                "start_char": first,
                                "end_char": first + len("同名"),
                                "evidence": {"quote": "同名"},
                            },
                            {
                                "type": "Rule",
                                "mention": "同名",
                                "start_char": second,
                                "end_char": second + len("同名"),
                                "evidence": {"quote": "同名"},
                            },
                        ]
                    },
                    ensure_ascii=False,
                )

        chunk = ExtractionChunk("same-name-table", text)
        plan = compile_plan(MIXED_ONTOLOGY)
        warnings: list[dict[str, object]] = []
        attempts: list[dict[str, object]] = []
        results = entity_pass(
            plan,
            OntologySchema.parse(MIXED_ONTOLOGY),
            ChunkContextManager([chunk]),
            ExtractionRequest([chunk]),
            SameNameSpanLLM(),
            route=DocumentRoute(kind="generic", source="fallback"),
            max_workers=1,
            use_scheduler=False,
            attempts=attempts,
            warnings=warnings,
            local_previous=0,
            local_next=0,
            same_section=False,
            max_attempts=1,
        )
        entities = results[0][1]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]["type"], "Rule")
        self.assertEqual(entities[0]["evidence"]["start_char"], second)
        self.assertEqual(entities[0]["evidence"]["end_char"], second + len("同名"))
        self.assertEqual(
            sum(item["code"] == "RULE_TABLE_ROW_OUT_OF_SCOPE" for item in warnings),
            1,
        )

    def test_entity_pass_keeps_second_row_span_when_rule_quote_is_long(self) -> None:
        text = _table(
            ["名称", "触发条件", "动作"],
            [{"名称": "同名"}, {"名称": "同名", "触发条件": "温度高", "动作": "停机"}],
        )
        first = text.index("同名")
        second = text.index("同名", first + 1)
        quote = '"名称": "同名"'
        quote_start = text.rfind(quote)

        class LongQuoteLLM:
            def __call__(self, messages: object) -> str:
                prompt = user_prompt(messages)
                source = str(current_chunk(prompt).get("text") or "")
                starts = [index for index in range(len(source)) if source.startswith("同名", index)]
                return json.dumps(
                    {
                        "entities": [
                            {
                                "type": "Rule",
                                "mention": "同名",
                                "start_char": start,
                                "end_char": start + len("同名"),
                                "evidence": {"quote": '"名称": "同名"'},
                            }
                            for start in starts
                        ]
                    },
                    ensure_ascii=False,
                )

        chunk = ExtractionChunk("long-quote-table", text)
        plan = compile_plan(MIXED_ONTOLOGY)
        warnings: list[dict[str, object]] = []
        results = entity_pass(
            plan,
            OntologySchema.parse(MIXED_ONTOLOGY),
            ChunkContextManager([chunk]),
            ExtractionRequest([chunk]),
            LongQuoteLLM(),
            route=DocumentRoute(kind="generic", source="fallback"),
            max_workers=1,
            use_scheduler=False,
            attempts=[],
            warnings=warnings,
            local_previous=0,
            local_next=0,
            same_section=False,
            max_attempts=1,
        )

        entities = results[0][1]
        self.assertEqual(len(entities), 1)
        evidence = entities[0]["evidence"]
        self.assertEqual(evidence["start_char"], quote_start)
        self.assertEqual(evidence["end_char"], quote_start + len(quote))
        self.assertEqual(text[evidence["start_char"] : evidence["end_char"]], quote)
        self.assertIn("同名", text[evidence["start_char"] : evidence["end_char"]])
        self.assertNotIn(first, range(evidence["start_char"], evidence["end_char"]))
        self.assertLess(evidence["start_char"], second)

    def test_entity_pass_maps_rule_from_row_window_back_to_original_row_24(self) -> None:
        rows = [
            (
                {"名称": "同名", "触发条件": "温度高", "动作": "停机"}
                if row == 24
                else {"名称": "同名" if row == 1 else f"普通-{row}"}
            )
            for row in range(1, 25)
        ]
        text = _table(["名称", "触发条件", "动作"], rows)
        first_same_name = text.index("同名")
        original_row_24 = text.index("同名", first_same_name + 1)
        original_row_24_start = text.index('{"名称": "同名", "触发条件"')

        class WindowedSpanLLM:
            def __call__(self, messages: object) -> str:
                prompt = user_prompt(messages)
                source = str(current_chunk(prompt).get("text") or "")
                start = source.find("同名")
                # The first window contains the ordinary row 1; the final
                # window contains the rule row 24. Both are grounded spans.
                return json.dumps(
                    {
                        "entities": [
                            {
                                "type": "Rule",
                                "mention": "同名",
                                "start_char": start,
                                "end_char": start + len("同名"),
                                "evidence": {"quote": "同名"},
                            }
                        ]
                    },
                    ensure_ascii=False,
                ) if start >= 0 else json.dumps({"entities": []})

        chunk = ExtractionChunk("windowed-table", text)
        plan = compile_plan(MIXED_ONTOLOGY)
        warnings: list[dict[str, object]] = []
        results = entity_pass(
            plan,
            OntologySchema.parse(MIXED_ONTOLOGY),
            ChunkContextManager([chunk]),
            ExtractionRequest([chunk]),
            WindowedSpanLLM(),
            route=DocumentRoute(kind="generic", source="fallback"),
            max_workers=1,
            use_scheduler=False,
            attempts=[],
            warnings=warnings,
            local_previous=0,
            local_next=0,
            same_section=False,
            max_attempts=1,
        )

        entities = results[0][1]
        self.assertEqual(len(entities), 1)
        evidence = entities[0]["evidence"]
        self.assertGreaterEqual(evidence["start_char"], original_row_24_start)
        self.assertLess(evidence["start_char"], original_row_24)
        self.assertGreater(evidence["end_char"], original_row_24)
        self.assertEqual(text[evidence["start_char"] : evidence["end_char"]], evidence["quote"])
        self.assertIn("同名", text[evidence["start_char"] : evidence["end_char"]])
        self.assertNotIn(first_same_name, range(evidence["start_char"], evidence["end_char"]))

    def test_rule_prompt_extension_is_only_added_for_rule_semantics(self) -> None:
        plan = compile_plan(RULE_ONTOLOGY)
        route = DocumentRoute(kind="generic", source="fallback")
        common = "\n".join(message["content"] for message in build_chat_messages(
            task="entity", plan=plan, route=route, chunk=_chunk(_table(["RuleID", "名称"], [{"RuleID": "R-1", "名称": "高温"}]))
        ))
        rule = "\n".join(message["content"] for message in build_chat_messages(
            task="entity", plan=plan, route=route, chunk=_chunk(_table(["条件", "动作"], [{"条件": "温度高", "动作": "停机"}]))
        ))

        self.assertIn("通用表格实体约束", common)
        self.assertLess(common.count("条件"), rule.count("条件"))


if __name__ == "__main__":
    unittest.main()
