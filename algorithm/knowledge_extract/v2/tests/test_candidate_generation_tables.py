from __future__ import annotations

import json
import unittest

from algorithm.knowledge_extract.v2.candidate_generation import _structured_row_segments
from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk


def _fenced_table(rows: list[dict[str, str]]) -> str:
    payload = {"columns": ["id", "value"], "rows": rows}
    return "## Sheet-A\n\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def _payload(text: str) -> dict:
    if "```" not in text:
        return json.loads(text)
    start = text.index("\n", text.index("```")) + 1
    end = text.rindex("```")
    return json.loads(text[start:end])


class StructuredTableWindowTests(unittest.TestCase):
    def test_few_large_rows_split_by_character_budget(self):
        rows = [{"id": str(index), "value": "甲" * 2200} for index in range(5)]
        chunk = ExtractionChunk(
            chunk_id="c1",
            text=_fenced_table(rows),
            metadata={"source_row_start": 2, "sheet_name": "Sheet-A"},
        )

        parts = _structured_row_segments(chunk)

        self.assertGreater(len(parts), 1)
        rebuilt_rows = [row for _, part in parts for row in _payload(part.text)["rows"]]
        self.assertEqual(rebuilt_rows, rows)
        self.assertTrue(all(len(part.text) <= 6000 for _, part in parts))
        self.assertTrue(all(part.metadata["table_windowed"] for _, part in parts))
        self.assertEqual(
            [
                (part.metadata["source_row_start"], part.metadata["source_row_end"])
                for _, part in parts
            ],
            [(2, 3), (4, 5), (6, 6)],
        )

    def test_twenty_one_short_rows_split_at_row_limit(self):
        rows = [{"id": str(index), "value": "ok"} for index in range(21)]
        parts = _structured_row_segments(ExtractionChunk(chunk_id="c2", text=_fenced_table(rows)))

        self.assertEqual([len(_payload(part.text)["rows"]) for _, part in parts], [20, 1])

    def test_single_oversized_row_is_preserved_without_recursion(self):
        row = {"id": "1", "value": "甲" * 7000}
        parts = _structured_row_segments(ExtractionChunk(chunk_id="c3", text=_fenced_table([row])))

        self.assertEqual(len(parts), 1)
        self.assertEqual(_payload(parts[0][1].text)["rows"], [row])
        self.assertEqual(_structured_row_segments(parts[0][1]), [])

    def test_naked_json_table_is_also_windowed(self):
        rows = [{"id": str(index), "value": "ok"} for index in range(21)]
        text = json.dumps({"columns": ["id", "value"], "rows": rows}, ensure_ascii=False)

        parts = _structured_row_segments(ExtractionChunk(chunk_id="c4", text=text))

        self.assertEqual([len(json.loads(part.text)["rows"]) for _, part in parts], [20, 1])

    def test_multiple_sheet_blocks_are_split_before_row_windowing(self):
        first = _fenced_table([{"id": "1", "value": "A"}])
        second = _fenced_table([{"id": "2", "value": "B"}]).replace("Sheet-A", "Sheet-B")
        metadata = {
            "tables": [
                {"sheet_name": "Sheet-A", "source_row_start": 2, "source_row_end": 2},
                {"sheet_name": "Sheet-B", "source_row_start": 2, "source_row_end": 2},
            ]
        }

        parts = _structured_row_segments(
            ExtractionChunk(chunk_id="c5", text=first + "\n\n" + second, metadata=metadata)
        )

        self.assertEqual(len(parts), 2)
        self.assertEqual([part.metadata["sheet_name"] for _, part in parts], ["Sheet-A", "Sheet-B"])
        self.assertEqual([_payload(part.text)["rows"][0]["id"] for _, part in parts], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
