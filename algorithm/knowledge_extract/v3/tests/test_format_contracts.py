"""Contract tests for parser output handed to the domain independent V3 harness.

The strings below are representative parser output fixtures.  These tests do
not exercise XLSX, PDF, or DOCX parsing; they verify that already parsed text
and location metadata retain the same ``ExtractionChunk`` / ``extract_detailed``
interface and reach the LLM contract intact.
"""
import json
import unittest

from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v3 import extract_detailed


ONTOLOGY = """@startuml
class Artifact {
  artifact_id: string
  amount: decimal
}
class Finding {
  finding_id: string
}
Artifact --> Finding : records
@enduml"""


class ContractFakeLLM:
    """Fake provider that records the exact structured prompt it receives."""

    def __init__(self, response):
        self.response = response
        self.payloads = []

    def __call__(self, messages):
        self.payloads.append(json.loads(messages[1]["content"]))
        return json.dumps(self.response, ensure_ascii=False)


class AdaptiveFakeLLM:
    def __init__(self):
        self.payloads = []

    def __call__(self, messages):
        payload = json.loads(messages[1]["content"])
        self.payloads.append(payload)
        if not payload["known_entities"]:
            response = {
                "entities": [{
                    "type": "Artifact", "name": "试验样件",
                    "properties": {"artifact_id": "A-7"}, "confidence": 0.9,
                    "support": "direct", "source_refs": [{"chunk_id": "part-1"}],
                }],
                "relations": [],
            }
        else:
            known = payload["known_entities"][0]
            response = {
                "entities": [{
                    "type": "Finding", "name": "裂纹",
                    "properties": {"finding_id": "F-2"}, "confidence": 0.9,
                    "support": "direct", "source_refs": [{"chunk_id": "part-2"}],
                }],
                "relations": [{
                    "from_type": known["type"], "from_name": known["name"], "rel_type": "records",
                    "to_type": "Finding", "to_name": "裂纹",
                    "source_refs": [{"chunk_id": "part-2"}],
                }],
            }
        return json.dumps(response, ensure_ascii=False)


def _entity(name, chunk_id):
    return {
        "entities": [{
            "type": "Artifact", "name": name,
            "properties": {"artifact_id": "same-local-id"}, "confidence": 0.9,
            "support": "direct", "source_refs": [{"chunk_id": chunk_id}],
        }],
        "relations": [],
    }


class FormatContractTests(unittest.TestCase):
    def test_markdown_chunk_uses_common_interface(self):
        llm = ContractFakeLLM(_entity("Markdown artifact", "md-1"))
        result = extract_detailed([
            ExtractionChunk("md-1", "## Results\nMarkdown artifact passed.", "markdown-doc",
                            section_title="Results")
        ], ONTOLOGY, llm=llm, audit=False)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["entities"][0]["name"], "Markdown artifact")
        self.assertEqual(llm.payloads[0]["source_windows"][0]["text"], "## Results\nMarkdown artifact passed.")

    def test_excel_parser_output_preserves_json_rows_and_table_coordinates(self):
        parsed_table = '{"columns":["artifact_id","amount"],"rows":[["A-7",12.5]]}'
        llm = ContractFakeLLM(_entity("A-7", "xlsx-1"))
        result = extract_detailed([
            ExtractionChunk("xlsx-1", parsed_table, "workbook-1", metadata={
                "source_kind": "excel", "sheet_name": "Measurements",
                "row_start": 4, "row_end": 4, "headers": ["artifact_id", "amount"],
            })
        ], ONTOLOGY, llm=llm, audit=False)
        self.assertEqual(result["status"], "success")
        window = llm.payloads[0]["source_windows"][0]
        self.assertEqual(json.loads(window["text"])["columns"], ["artifact_id", "amount"])
        self.assertEqual(window["metadata"]["sheet_name"], "Measurements")
        self.assertEqual(window["metadata"]["row_start"], 4)
        self.assertEqual(window["metadata"]["row_end"], 4)

    def test_pdf_ocr_markdown_carries_page_location_and_evidence_ref(self):
        ocr_markdown = "# Inspection\n\nThe artifact shows a crack."
        llm = ContractFakeLLM({
            "entities": [{"type": "Artifact", "name": "artifact",
                          "properties": {}, "confidence": 0.8, "support": "direct",
                          "source_refs": [{"chunk_id": "pdf-p3", "quote": "artifact"}]}],
            "relations": [],
        })
        result = extract_detailed([
            ExtractionChunk("pdf-p3", ocr_markdown, "report.pdf", page_no=3,
                            metadata={"source_kind": "pdf_ocr", "source_file": "report.pdf"})
        ], ONTOLOGY, llm=llm, audit=False)
        self.assertEqual(result["status"], "success")
        window = llm.payloads[0]["source_windows"][0]
        self.assertEqual(window["page_no"], 3)
        self.assertEqual(window["metadata"]["source_kind"], "pdf_ocr")
        ref = result["entities"][0]["source_refs"][0]
        self.assertEqual((ref['chunk_id'],ref['quote'],ref['page_no']),('pdf-p3','artifact',3))
        self.assertEqual(result["entities"][0]["locator_status"], "exact")

    def test_docx_parsed_content_and_section_context_are_forwarded(self):
        parsed_docx = "The artifact was approved by the review board."
        llm = ContractFakeLLM(_entity("artifact", "docx-2"))
        result = extract_detailed([
            ExtractionChunk("docx-2", parsed_docx, "review.docx", section_id="sec-2",
                            section_title="Approval", metadata={"source_kind": "docx"})
        ], ONTOLOGY, llm=llm, audit=False)
        self.assertEqual(result["status"], "success")
        window = llm.payloads[0]["source_windows"][0]
        self.assertEqual(window["text"], parsed_docx)
        self.assertEqual(window["section_id"], "sec-2")
        self.assertEqual(window["section_title"], "Approval")

    def test_same_document_chunks_resolve_relation_by_endpoint_names(self):
        llm = AdaptiveFakeLLM()
        result = extract_detailed([
            ExtractionChunk("part-1", "试验样件 A-7。", "report"),
            ExtractionChunk("part-2", "该样件记录裂纹 F-2。", "report"),
        ], ONTOLOGY, llm=llm, audit=False)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["entities"]), 2)
        self.assertEqual(len(result["relations"]), 1)
        self.assertEqual({key:result["relations"][0][key] for key in ('from_type','from_name','rel_type','to_type','to_name')}, {
            "from_type": "Artifact", "from_name": "试验样件", "rel_type": "records",
            "to_type": "Finding", "to_name": "裂纹",
        })
        self.assertEqual(llm.payloads[1]["known_entities"][0], {
            "type": "Artifact", "name": "试验样件", "properties": {"artifact_id": "A-7"},
        })

    def test_cross_document_endpoint_names_cannot_reference_unseen_entities(self):
        class TwoResponses(ContractFakeLLM):
            def __init__(self):
                super().__init__(None)

            def __call__(self, messages):
                payload = json.loads(messages[1]["content"])
                self.payloads.append(payload)
                if payload["source_windows"][0]["document_id"] == "doc-b":
                    return json.dumps({
                        "entities": [{"type": "Artifact", "name": "other name"}],
                        "relations": [{
                            "from_type": "Artifact", "from_name": "same name",
                            "rel_type": "records", "to_type": "Finding", "to_name": "missing",
                        }],
                    })
                return json.dumps(_entity("same name", payload["source_windows"][0]["chunk_id"]))

        llm = TwoResponses()
        result = extract_detailed([
            ExtractionChunk("doc-a-1", "same name", "doc-a"),
            ExtractionChunk("doc-b-1", "same name", "doc-b"),
        ], ONTOLOGY, llm=llm, audit=False)
        # The invalid cross-document relation may make extraction incomplete,
        # but it must never be accepted into the graph.
        self.assertIn(result["status"], {"success", "incomplete"})
        self.assertEqual([entity["name"] for entity in result["entities"]], ["same name", "other name"])
        self.assertEqual(result["relations"], [])
        self.assertEqual([p["known_entities"] for p in llm.payloads[:2]], [[], []])


if __name__ == "__main__":
    unittest.main()
