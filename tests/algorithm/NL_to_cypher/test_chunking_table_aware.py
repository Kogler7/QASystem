"""表格感知切分（algorithm/knowledge_pipeline/chunking.py）的回归测试。

回归背景：切片层按长度切 fenced 表格会把 JSON 按顶层键拆开并重新序列化，围栏只落在
首尾两片——抽取层 ``_structured_row_segments`` 要求"同一 chunk 内既有开围栏又有闭围栏"
才能把超长表格按行拆成自洽子表，于是表格链路静默退化（几十行 Excel 抽不出来即此因）。

契约（两段式）：
  切片层：任意 ``` 围栏块（含紧邻的 Markdown 标题）整块成片，不设长度上限；
  抽取层：超长表格片按 ``TABLE_ROWS_PER_PART`` 行拆成多个自洽子表。
"""
from __future__ import annotations

import json
import unittest

from algorithm.knowledge_pipeline.chunking import chunk_document, chunk_document_items

TABLE_ROWS = 300
TABLE_COLUMNS = ["序号", "费用项目", "归口部门", "金额（元）", "备注"]


def _table_payload(rows: int = TABLE_ROWS) -> dict:
    return {
        "columns": TABLE_COLUMNS,
        "rows": [
            {
                "序号": str(index),
                "费用项目": f"成本项目{index}",
                "归口部门": "财务部",
                "金额（元）": str(index * 100),
                "备注": "按月分摊",
            }
            for index in range(1, rows + 1)
        ],
    }


def _table_markdown(name: str = "成本明细表", rows: int = TABLE_ROWS) -> str:
    payload = "```json" + "\n" + json.dumps(_table_payload(rows), ensure_ascii=False, indent=2) + "\n```"
    return f"## {name}\n{payload}"


def _document_with_big_table() -> str:
    return (
        "# 成本核算管理办法\n\n"
        + _table_markdown()
        + "\n\n## 附则\n本章程自发布之日起施行。"
        + "补充说明。" * 400
    )


def _verbatim(source: str, items: list[dict]) -> bool:
    return all(source[start:end] == item["text"] for item in items for start, end in [item["range"]])


class TableAwareChunkingTest(unittest.TestCase):
    def test_fenced_table_stays_in_one_chunk_and_ranges_are_verbatim(self) -> None:
        source = _document_with_big_table()
        self.assertGreater(len(source), 12000)

        items = chunk_document_items(source, chunk_type=0, chunk_size=4000, chunk_overlap=0)

        self.assertTrue(_verbatim(source, items), "每片的 range 必须逐字指回原文")
        table_items = [item for item in items if '"columns"' in item["text"]]
        self.assertEqual(len(table_items), 1, "表格必须完整落在同一个片里")
        table_text = table_items[0]["text"]
        self.assertEqual(table_text.count("```"), 2, "围栏必须成对，不能只剩一半")
        self.assertIn("## 成本明细表", table_text, "紧邻表格的标题应与表格同片")
        self.assertGreater(len(table_text), 4000, "表格片允许超过 chunk_size")

    def test_table_chunk_feeds_self_contained_row_subtables(self) -> None:
        """切片层产出的表格片必须满足抽取层按行拆子表的前提。"""
        from algorithm.knowledge_extract.v2.candidate_generation import (
            TABLE_ROWS_PER_PART,
            _structured_row_segments,
        )
        from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk

        source = _document_with_big_table()
        items = chunk_document_items(source, chunk_type=0, chunk_size=4000, chunk_overlap=0)
        table_text = next(item["text"] for item in items if '"columns"' in item["text"])

        parts = _structured_row_segments(ExtractionChunk(chunk_id="c0", text=table_text))

        self.assertGreater(len(parts), 1, "300 行表格应被拆成多个自洽子表")
        total_rows = 0
        for _offset, part in parts:
            text = part.text
            self.assertIn("```json", text, "子表必须自带开围栏")
            self.assertEqual(text.count("```"), 2, "每个子表必须自带完整围栏")
            body = json.loads(text[text.index("{"):text.rindex("}") + 1])
            self.assertEqual(body["columns"], TABLE_COLUMNS)
            self.assertLessEqual(len(body["rows"]), TABLE_ROWS_PER_PART)
            total_rows += len(body["rows"])
        self.assertEqual(total_rows, TABLE_ROWS)

    def test_two_tables_stay_separate_and_whole(self) -> None:
        first = "```json\n" + json.dumps({"columns": ["a"], "rows": [{"a": "1" * 200}]}, ensure_ascii=False) + "\n```"
        second = "```json\n" + json.dumps({"columns": ["b"], "rows": [{"b": "2" * 200}]}, ensure_ascii=False) + "\n```"
        source = f"前言{'前' * 600}\n{first}\n中间{'中' * 600}\n{second}\n结尾"

        items = chunk_document_items(source, chunk_type=0, chunk_size=200)

        self.assertTrue(_verbatim(source, items))
        first_items = [item for item in items if '"a"' in item["text"]]
        second_items = [item for item in items if '"b"' in item["text"]]
        self.assertEqual(len(first_items), 1)
        self.assertEqual(len(second_items), 1)
        for item in (first_items[0], second_items[0]):
            self.assertEqual(item["text"].count("```"), 2)

    def test_text_without_fence_keeps_previous_behaviour(self) -> None:
        source = "长文本0123456789" * 400

        chunks = chunk_document(source, chunk_type=0, chunk_size=500)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertEqual("".join(chunks), source)
