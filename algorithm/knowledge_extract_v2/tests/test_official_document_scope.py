from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from algorithm.knowledge_extract_v2.chunk_context import ExtractionChunk
from algorithm.knowledge_extract_v2.official_document_scope import (
    annotate_official_roles,
    looks_like_official_form,
    management_type_ids,
    official_entity_types,
    official_role,
)
from algorithm.knowledge_extract_v2.ontology_compiler import compile_plan
from algorithm.knowledge_extract_v2.prompt_router import DocumentRoute
from algorithm.knowledge_extract_v2.split_extractor import extract_split
from algorithm.knowledge_extract_v2.verification import VerificationIssue, VerificationReport


ONTOLOGY = """
@startuml
class "公文管理单元" as OfficialDocument <<OfficialDocument>> {
  + 标题 (title): String
}
class "正文条目" as DocumentUnit {
  + 内容 (content): String
}
class "工作事项" as OperationalActivity {
  + 事项 (activity): String
}
class "附件信息" as OperationalInformation {
  + 名称 (name): String
  + 信息类型 (informationType): String
  + 来源文本 (sourceText): String
  + 文件引用 (fileReference): String
}
@enduml
"""


def _plan():
    return compile_plan(ONTOLOGY)


def _prop(properties: dict[str, object], identifier: str, display: str) -> object:
    """Accept raw identifier keys and graph-compatible display keys."""
    return properties.get(identifier, properties.get(display))


class RecordingLLM:
    def __init__(
        self,
        entity_payload: dict[str, object] | None = None,
        identity_payload: dict[str, object] | None = None,
    ) -> None:
        self.prompts: list[str] = []
        self.calls = 0
        self.entity_payload = entity_payload
        self.identity_payload = identity_payload

    def __call__(self, messages: object) -> str:
        self.calls += 1
        if isinstance(messages, list):
            self.prompts.append("\n".join(str(item.get("content") or "") for item in messages if isinstance(item, dict)))
        combined = "\n".join(str(item.get("content") or "") for item in messages if isinstance(item, dict))
        if "## task\nevidence_verification" in combined:
            return json.dumps(
                {"verdicts": [{"item_id": "entity-0", "supported": "yes", "reason": "test"}]},
                ensure_ascii=False,
            )
        if "## task\nidentity" in combined:
            return json.dumps(self.identity_payload or {"updates": []}, ensure_ascii=False)
        return json.dumps(self.entity_payload or {"entities": []}, ensure_ascii=False)


class OfficialDocumentScopeTests(unittest.TestCase):
    def test_attachment_keeps_trailing_newlines_without_business_coverage_gaps(self) -> None:
        ontology = ONTOLOGY.replace("@enduml", 'OfficialDocument "1" --> "1" OperationalInformation : HAS_INFO\n@enduml')
        text = "\n附件原文\n\n"
        llm = RecordingLLM()
        result = extract_split(ontology, chunks=[ExtractionChunk(
            "attachment", text, metadata={"source_file": "附件.docx"},
        )], llm=llm)
        self.assertEqual(llm.calls, 0)
        self.assertTrue(result["verification"]["valid"])
        self.assertEqual(result["coverage_gaps"], [])
        self.assertEqual(result["status"], "succeeded")
        node = result["graph"]["nodes"][0]
        self.assertEqual(_prop(node["properties"], "sourceText", "来源文本"), text)

    def test_form_allows_management_and_operational_types(self) -> None:
        plan = _plan()
        for text, metadata in (
            (
                "## 收文处理单\n\n```json\n{\"columns\":[\"收文号\",\"拟办意见\"],\"rows\":[{\"收文号\":\"R-1\",\"拟办意见\":\"请阅办\"}]}\n```",
                {},
            ),
            ("# 收文处理单\n来文字号：L-2\n领导批示：请办理。", {}),
        ):
            chunk = ExtractionChunk("form", text, metadata=metadata)
            self.assertTrue(looks_like_official_form(chunk))
            self.assertEqual(official_role(plan, chunk), "form")
            # 表单不放行结构单元，但放行管理类型与 operational 业务类型。
            self.assertEqual(
                official_entity_types(plan, chunk, plan.allowed_types),
                {"OfficialDocument", "OperationalActivity", "OperationalInformation"},
            )

    def test_form_filename_and_metadata_are_strong_role_signals(self) -> None:
        plan = _plan()
        filename_chunk = ExtractionChunk(
            "f", "字段A\n字段B", metadata={"source_file": "/tmp/收文处理单.xlsx"}
        )
        metadata_chunk = ExtractionChunk(
            "m", "普通表格内容", metadata={"official_document_role": "form"}
        )

        self.assertEqual(official_role(plan, filename_chunk), "form")
        self.assertEqual(official_role(plan, metadata_chunk), "form")

    def test_body_allows_operational_types_but_excludes_management_type(self) -> None:
        plan = _plan()
        body = ExtractionChunk("body", "正文要求：请于周五报送。")
        route = DocumentRoute(kind="official_document", source="chunks", labels=("official_document",))

        self.assertEqual(official_role(plan, body, route), "body")
        allowed = official_entity_types(plan, body, plan.allowed_types, route)
        self.assertNotIn("OfficialDocument", allowed)
        self.assertIn("DocumentUnit", allowed)
        self.assertIn("OperationalActivity", allowed)

    def test_generic_route_does_not_apply_official_scope(self) -> None:
        plan = _plan()
        chunk = ExtractionChunk("generic", "普通文本")
        route = DocumentRoute(kind="generic", source="fallback")

        self.assertEqual(official_role(plan, chunk, route), "")
        self.assertEqual(official_entity_types(plan, chunk, plan.allowed_types, route), plan.allowed_types)

    def test_attachment_role_hides_all_entity_types_from_llm(self) -> None:
        plan = _plan()
        attachment = ExtractionChunk(
            "attachment",
            "附件1：排查反馈表 ATTACHMENT_SECRET",
            metadata={"official_document_role": "attachment", "source_file": "附件1.xlsx"},
        )

        self.assertEqual(official_role(plan, attachment), "attachment")
        self.assertEqual(official_entity_types(plan, attachment, plan.allowed_types), frozenset())

    def test_same_document_body_prompt_does_not_leak_attachment_context(self) -> None:
        body = ExtractionChunk(
            "body",
            "正文：请于周五报送。",
            metadata={"official_document_role": "body"},
        )
        attachment = ExtractionChunk(
            "attachment",
            "附件1：排查反馈表 ATTACHMENT_SECRET",
            metadata={"official_document_role": "attachment", "source_file": "附件1.xlsx"},
        )
        llm = RecordingLLM()
        result = extract_split(ONTOLOGY, chunks=[body, attachment], llm=llm)

        body_prompts = [prompt for prompt in llm.prompts if "正文：请于周五报送" in prompt]
        self.assertTrue(body_prompts)
        self.assertTrue(all("ATTACHMENT_SECRET" not in prompt for prompt in llm.prompts))
        self.assertNotIn("OfficialDocument", {item.get("type") for item in result["raw"]["entities"]})

        attachment_entities = [
            item for item in result["raw"]["entities"] if item.get("type") == "OperationalInformation"
        ]
        self.assertTrue(attachment_entities)
        self.assertTrue(
            any(
                _prop(item.get("properties", {}), "informationType", "信息类型") == "附件"
                and "ATTACHMENT_SECRET" in (_prop(item.get("properties", {}), "sourceText", "来源文本") or "")
                and _prop(item.get("properties", {}), "fileReference", "文件引用") == "附件1.xlsx"
                for item in attachment_entities
            )
        )
        nodes = result["graph"]["nodes"]
        self.assertTrue(
            any(
                node.get("source_files")
                and any(row.get("source_file") == "附件1.xlsx" for row in node["source_files"])
                for node in nodes
            )
        )
        self.assertTrue(
            any(
                any(row.get("source_file") == "附件1.xlsx" for row in item.get("source_files", []))
                for item in attachment_entities
            )
        )

    def test_single_attachment_is_deterministic_and_zero_llm_calls(self) -> None:
        attachment = ExtractionChunk(
            "attachment",
            "附件1：排查反馈表 ATTACHMENT_SECRET",
            metadata={"source_file": "/upload/附件1.xlsx"},
        )
        llm = RecordingLLM()

        result = extract_split(ONTOLOGY, chunks=[attachment], llm=llm)

        self.assertEqual(llm.calls, 0)
        attachment_entities = [
            item for item in result["raw"]["entities"] if item.get("type") == "OperationalInformation"
        ]
        self.assertTrue(attachment_entities)
        self.assertTrue(
            any(
                _prop(item.get("properties", {}), "informationType", "信息类型") == "附件"
                and "ATTACHMENT_SECRET" in (_prop(item.get("properties", {}), "sourceText", "来源文本") or "")
                and _prop(item.get("properties", {}), "fileReference", "文件引用") == "/upload/附件1.xlsx"
                for item in attachment_entities
            )
        )
        self.assertTrue(
            any(
                any(
                    row.get("source_file") == "/upload/附件1.xlsx"
                    for row in item.get("source_files", [])
                )
                for item in attachment_entities
            )
        )

    def test_custom_repairer_is_called_without_attachment_context(self) -> None:
        body = ExtractionChunk(
            "body",
            "正文：请核对设备清单。",
            metadata={"official_document_role": "body"},
        )
        attachment = ExtractionChunk(
            "attachment",
            "附件原文 ATTACHMENT_SECRET",
            metadata={
                "official_document_role": "attachment",
                "source_file": "附件1.xlsx",
            },
        )
        issue = VerificationIssue(
            code="REQUIRED_RELATION_MISSING",
            message="forced repair regression",
            repair_type="required_relation",
        )
        repair_payloads: list[dict[str, object]] = []

        def repairer(payload: dict[str, object]) -> dict[str, object]:
            repair_payloads.append(payload)
            return {"relations": []}

        with patch(
            "algorithm.knowledge_extract_v2.split_extractor.ExtractionVerifier.verify",
            return_value=VerificationReport((issue,)),
        ):
            result = extract_split(
                ONTOLOGY,
                chunks=[body, attachment],
                llm=RecordingLLM(),
                repairer=repairer,
                max_repair_rounds=1,
            )

        self.assertTrue(repair_payloads)
        serialized = json.dumps(repair_payloads[0], ensure_ascii=False)
        self.assertNotIn("ATTACHMENT_SECRET", serialized)
        self.assertEqual(
            {chunk["chunk_id"] for chunk in repair_payloads[0]["chunks"]},
            {"body"},
        )
        self.assertEqual(result["repair"]["stop_reason"], "repair_no_new_facts")

    def test_form_properties_are_isolated_without_source_or_document_metadata(self) -> None:
        class FormLLM(RecordingLLM):
            def __call__(self, messages: object) -> str:
                combined = "\n".join(
                    str(item.get("content") or "")
                    for item in messages
                    if isinstance(item, dict)
                )
                if "## task\nentity" in combined:
                    marker = "FORM_A_MARKER" if "FORM_A_MARKER" in combined else "FORM_B_MARKER"
                    self.entity_payload = {
                        "entities": [
                            {
                                "type": "OfficialDocument",
                                "mention": marker,
                                "start_char": combined.index(marker),
                                "end_char": combined.index(marker) + len(marker),
                                "evidence": {"quote": marker},
                            }
                        ]
                    }
                return super().__call__(messages)

        chunks = [
            ExtractionChunk(
                "form-a",
                "收文处理单 FORM_A_MARKER 标题：甲表单",
                metadata={"official_document_role": "form"},
            ),
            ExtractionChunk(
                "form-b",
                "收文处理单 FORM_B_MARKER 标题：乙表单",
                metadata={"official_document_role": "form"},
            ),
        ]
        llm = FormLLM()
        result = extract_split(
            ONTOLOGY,
            chunks=chunks,
            llm=llm,
            local_previous=1,
            local_next=1,
        )

        entity_prompts = [prompt for prompt in llm.prompts if "## task\nentity" in prompt]
        a_prompts = [prompt for prompt in entity_prompts if "FORM_A_MARKER" in prompt]
        b_prompts = [prompt for prompt in entity_prompts if "FORM_B_MARKER" in prompt]
        self.assertTrue(a_prompts)
        self.assertTrue(b_prompts)
        self.assertTrue(all("FORM_B_MARKER" not in prompt for prompt in a_prompts))
        self.assertTrue(all("FORM_A_MARKER" not in prompt for prompt in b_prompts))
        names = {
            item.get("name")
            for item in result["raw"]["entities"]
            if item.get("type") == "OfficialDocument"
        }
        self.assertTrue({"FORM_A_MARKER", "FORM_B_MARKER"}.issubset(names))

    def test_embedded_attachment_is_split_without_secret_in_any_llm_prompt(self) -> None:
        body = ExtractionChunk(
            "source-body",
            "正文条目：请于周五报送。\n# 附件1\n附件原文 ATTACHMENT_SECRET",
            document_id="doc-embedded",
            metadata={"source_file": "通知.docx"},
        )
        continuation = ExtractionChunk(
            "source-attachment-tail",
            "附件续文 ATTACHMENT_TAIL_SECRET",
            document_id="doc-embedded",
            metadata={"source_file": "通知.docx"},
        )
        llm = RecordingLLM()

        result = extract_split(ONTOLOGY, chunks=[body, continuation], llm=llm)

        self.assertTrue(llm.prompts)
        self.assertTrue(all("ATTACHMENT_SECRET" not in prompt for prompt in llm.prompts))
        self.assertTrue(all("ATTACHMENT_TAIL_SECRET" not in prompt for prompt in llm.prompts))
        source_ids = result["profile"].get("chunk_source_ids", {})
        self.assertTrue(source_ids)
        self.assertTrue(any(value == "source-body" for value in source_ids.values()))
        self.assertTrue(any(value == "source-attachment-tail" for value in source_ids.values()))
        attachment_entities = [
            item for item in result["raw"]["entities"] if item.get("type") == "OperationalInformation"
        ]
        self.assertTrue(attachment_entities)
        self.assertTrue(
            any(
                "ATTACHMENT_SECRET" in (_prop(item.get("properties", {}), "sourceText", "来源文本") or "")
                and "ATTACHMENT_TAIL_SECRET" in (_prop(item.get("properties", {}), "sourceText", "来源文本") or "")
                for item in attachment_entities
            )
        )

    def test_same_filename_different_documents_make_distinct_attachment_nodes(self) -> None:
        chunks = [
            ExtractionChunk(
                "attachment-a",
                "甲文 ATTACHMENT_A",
                document_id="doc-a",
                metadata={"source_file": "附件.xlsx", "official_document_role": "attachment"},
            ),
            ExtractionChunk(
                "attachment-b",
                "乙文 ATTACHMENT_B",
                document_id="doc-b",
                metadata={"source_file": "附件.xlsx", "official_document_role": "attachment"},
            ),
        ]
        llm = RecordingLLM()

        result = extract_split(ONTOLOGY, chunks=chunks, llm=llm)

        self.assertEqual(llm.calls, 0)
        nodes = [node for node in result["graph"]["nodes"] if node.get("label") == "OperationalInformation"]
        self.assertEqual(len(nodes), 2)
        self.assertEqual(len({node["uid"] for node in nodes}), 2)
        self.assertTrue(result["verification"]["valid"])
        self.assertNotEqual(result["status"], "failed")
        self.assertEqual(
            {row["source_file"] for node in nodes for row in node.get("source_files", [])},
            {"附件.xlsx"},
        )
        self.assertEqual(
            {row["document_id"] for node in nodes for row in node.get("source_files", [])},
            {"doc-a", "doc-b"},
        )

    def test_body_form_attachment_multi_passes_do_not_leak_attachment_text(self) -> None:
        body = ExtractionChunk(
            "body",
            "正文主体：请于周五报送。",
            document_id="doc-multi",
            metadata={"official_document_role": "body", "source_file": "通知.docx"},
        )
        form = ExtractionChunk(
            "form",
            "# 收文处理单\n标题：关于事项的通知\n拟办意见：请阅办。",
            document_id="doc-multi",
            metadata={"official_document_role": "form", "source_file": "收文处理单.docx"},
        )
        attachment = ExtractionChunk(
            "attachment",
            "附件原文 ATTACHMENT_SECRET",
            document_id="doc-multi",
            metadata={"official_document_role": "attachment", "source_file": "附件1.xlsx"},
        )

        class ScopedLLM(RecordingLLM):
            def __call__(self, messages: object) -> str:
                combined = "\n".join(
                    str(item.get("content") or "")
                    for item in messages
                    if isinstance(item, dict)
                )
                if "## task\nentity" in combined and "正文主体" in combined:
                    self.entity_payload = {
                        "entities": [
                            {
                                "type": "DocumentUnit",
                                "mention": "正文主体",
                                "start_char": 0,
                                "end_char": 4,
                                "evidence": {"quote": "正文主体"},
                            }
                        ]
                    }
                elif "## task\nentity" in combined and "关于事项的通知" in combined:
                    self.entity_payload = {
                        "entities": [
                            {
                                "type": "OfficialDocument",
                                "mention": "关于事项的通知",
                                "start_char": 0,
                                "end_char": 8,
                                "evidence": {"quote": "关于事项的通知"},
                            }
                        ]
                    }
                return super().__call__(messages)

        llm = ScopedLLM()
        result = extract_split(ONTOLOGY, chunks=[body, form, attachment], llm=llm)

        self.assertTrue(any(item.get("type") == "DocumentUnit" for item in result["raw"]["entities"]))
        self.assertTrue(any(item.get("type") == "OperationalInformation" for item in result["raw"]["entities"]))
        self.assertTrue(all("ATTACHMENT_SECRET" not in prompt for prompt in llm.prompts))

    def test_form_extracts_nonempty_management_unit_and_body_excludes_it(self) -> None:
        form = ExtractionChunk(
            "form",
            "# 收文处理单\n标题：关于设备排查的通知\n拟办意见：请阅办。",
            metadata={"source_file": "收文处理单.docx"},
        )
        form_llm = RecordingLLM(
            {
                "entities": [
                    {
                        "type": "OfficialDocument",
                        "mention": "关于设备排查的通知",
                        "start_char": 17,
                        "end_char": 27,
                        "evidence": {"quote": "关于设备排查的通知"},
                    }
                ]
            },
            {
                "updates": [
                    {
                        "entity_id": "entity-0",
                        "properties": {"title": "关于设备排查的通知"},
                        "evidence": {"quote": "关于设备排查的通知"},
                    }
                ]
            },
        )
        form_result = extract_split(ONTOLOGY, chunks=[form], llm=form_llm)

        self.assertTrue(
            any(item.get("type") == "OfficialDocument" for item in form_result["raw"]["entities"])
        )
        self.assertTrue(any("OfficialDocument" in prompt for prompt in form_llm.prompts))

        body = ExtractionChunk(
            "body-only",
            "正文条目：请于周五报送。",
            metadata={"official_document_role": "body"},
        )
        body_llm = RecordingLLM()
        extract_split(ONTOLOGY, chunks=[body], llm=body_llm)
        self.assertTrue(body_llm.prompts)
        self.assertTrue(any("DocumentUnit" in prompt for prompt in body_llm.prompts))
        self.assertTrue(all("-> type `OfficialDocument`" not in prompt for prompt in body_llm.prompts))


if __name__ == "__main__":
    unittest.main()
