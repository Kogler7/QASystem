"""切片器 JSON 保护回归测试：围栏随 JSON 同行、标量数组不保护。

背景：解析层 §10 的表格统一是 ```json 围栏内的 {"columns", "rows"}。切片器要把
JSON（连同围栏）整块保留——围栏撕成孤立小片会白占 LLM 调用，[1] 这类文献角标
被误当成 JSON 则会把参考文献条目撕碎。

运行（本机无 langchain 时自动注入最小桩，被测函数是纯文本函数）：
    cd algorithm && python3 -m unittest discover -s NL_to_cypher/tests -p 'test_*.py'
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_langchain_stubs() -> None:
    """仅为 splitter 模块导入提供 langchain 名称；被测函数不使用这些类。"""
    documents = types.ModuleType("langchain_core.documents")

    class Document:
        def __init__(self, page_content: str = "", metadata: dict | None = None) -> None:
            self.page_content = page_content
            self.metadata = metadata or {}

    documents.Document = Document

    core = types.ModuleType("langchain_core")
    core.documents = documents

    splitters = types.ModuleType("langchain_text_splitters")

    class _Unavailable:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("测试桩：未安装 langchain，真实切割器不可用")

    splitters.MarkdownTextSplitter = _Unavailable
    splitters.RecursiveCharacterTextSplitter = _Unavailable

    sys.modules.setdefault("langchain_core", core)
    sys.modules.setdefault("langchain_core.documents", documents)
    sys.modules.setdefault("langchain_text_splitters", splitters)


try:  # 装有 langchain 的环境走真实导入
    import langchain_core.documents  # noqa: F401
    import langchain_text_splitters  # noqa: F401
except ImportError:
    _install_langchain_stubs()

from langchain_core.documents import Document  # noqa: E402

from algorithm.NL_to_cypher.splitter import (  # noqa: E402
    _extract_json_blocks,
    _split_text_with_json_protection,
    split_by_characters,
)


JSON_PAYLOAD = {
    "columns": ["线路", "状态"],
    "rows": [{"线路": "2号线", "状态": "正常"}],
}
JSON_TABLE_MD = (
    "## 二号线数据\n\n```json\n"
    + json.dumps(JSON_PAYLOAD, ensure_ascii=False, indent=2)
    + "\n```\n\n下文继续。\n"
)


class ExtractJsonBlocksTest(unittest.TestCase):
    def test_object_block_extracted(self) -> None:
        text = json.dumps(JSON_PAYLOAD, ensure_ascii=False)
        self.assertEqual(_extract_json_blocks(text), [(0, len(text), text)])

    def test_list_of_objects_extracted(self) -> None:
        text = '[{"a": 1}, {"b": 2}]'
        self.assertEqual(_extract_json_blocks(text), [(0, len(text), text)])

    def test_scalar_array_not_protected(self) -> None:
        # 参考文献角标：保护它会撕出 "[1]" 这样的碎片 chunk，参考文献抽取随之失效。
        self.assertEqual(_extract_json_blocks("[1] GB/T 1.1—2020\n[2] GB/T 1.2—2020\n"), [])

    def test_string_array_not_protected(self) -> None:
        self.assertEqual(_extract_json_blocks('["参考文献", "引用"]'), [])

    def test_array_with_object_element_protected(self) -> None:
        self.assertEqual(len(_extract_json_blocks('[1, {"a": 2}]')), 1)

    def test_braces_inside_string_do_not_break_matching(self) -> None:
        text = '{"a": "}{", "b": 1}'
        self.assertEqual(_extract_json_blocks(text), [(0, len(text), text)])

    def test_unclosed_or_invalid_not_protected(self) -> None:
        self.assertEqual(_extract_json_blocks('{"a": 1'), [])
        self.assertEqual(_extract_json_blocks("{不是 JSON}"), [])

    def test_prose_around_block(self) -> None:
        text = '说明在前。\n{"a": 1}\n说明在后。'
        blocks = _extract_json_blocks(text)
        self.assertEqual(len(blocks), 1)
        start, end, block = blocks[0]
        self.assertEqual(block, '{"a": 1}')
        self.assertEqual(text[start:end], block)


class JsonFragmentShapeTest(unittest.TestCase):
    def test_fence_travels_with_json(self) -> None:
        fragments = _split_text_with_json_protection(JSON_TABLE_MD)
        self.assertEqual(len(fragments), 3)
        prefix, (json_fragment, is_json), suffix = fragments
        self.assertEqual(prefix, ("## 二号线数据\n\n", False))
        self.assertTrue(is_json)
        self.assertTrue(json_fragment.startswith("```json\n"))
        self.assertTrue(json_fragment.endswith("\n```"))
        self.assertEqual(suffix, ("\n\n下文继续。\n", False))
        self.assertEqual("".join(text for text, _ in fragments), JSON_TABLE_MD)

    def test_no_fragment_is_bare_fence(self) -> None:
        for text, _ in _split_text_with_json_protection(JSON_TABLE_MD):
            self.assertNotIn(text.strip(), ("```json", "```"))

    def test_bare_json_without_fence_unchanged(self) -> None:
        text = json.dumps(JSON_PAYLOAD, ensure_ascii=False)
        self.assertEqual(_split_text_with_json_protection(text), [(text, True)])

    def test_bare_json_keeps_surrounding_prose_split(self) -> None:
        text = '前文\n{"a": 1}\n后文'
        self.assertEqual(
            _split_text_with_json_protection(text),
            [("前文\n", False), ('{"a": 1}', True), ("\n后文", False)],
        )

    def test_reference_markers_stay_in_one_text_fragment(self) -> None:
        text = "正文。\n[1] GB/T 1.1—2020\n[2] GB/T 1.2—2020\n"
        self.assertEqual(_split_text_with_json_protection(text), [(text, False)])

    def test_pipe_table_left_to_plain_path(self) -> None:
        # .md/.txt 原文直读的管道表格不是 JSON，切片器不介入。
        text = "| 检查项 | 状态 |\n| --- | --- |\n"
        self.assertEqual(_split_text_with_json_protection(text), [(text, False)])


class ChunkLevelProtectionTest(unittest.TestCase):
    def test_long_plain_text_obeys_limit_and_preserves_all_characters(self) -> None:
        source = "长文本0123456789" * 120
        chunks = split_by_characters([Document(page_content=source)], chunk_size=500)
        self.assertTrue(all(len(item.page_content) <= 500 for item in chunks))
        self.assertEqual("".join(item.page_content for item in chunks), source)

    def test_overlap_is_inside_size_budget_for_long_plain_text(self) -> None:
        source = "".join(chr(0x4E00 + i) for i in range(1200))
        chunks = split_by_characters([Document(page_content=source)], chunk_size=500, chunk_overlap=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(item.page_content) <= 500 for item in chunks))
        reconstructed = chunks[0].page_content
        for before, after in zip(chunks, chunks[1:]):
            self.assertEqual(before.page_content[-50:], after.page_content[:50])
            reconstructed += after.page_content[50:]
        self.assertEqual(reconstructed, source)

    def test_repeated_text_ranges_follow_overlap_offsets(self) -> None:
        from algorithm.knowledge_pipeline.chunking import chunk_document_items

        source = "x" * 1200
        items = chunk_document_items(
            source,
            chunk_type=1,
            chunk_size=500,
            chunk_characters=["\n"],
            chunk_overlap=50,
        )
        self.assertEqual(
            [item["range"] for item in items],
            [[0, 450], [400, 900], [850, 1200]],
        )
        self.assertTrue(all(source[start:end] == item["text"] for item in items for start, end in [item["range"]]))

    def test_all_text_splitters_preserve_repeated_text_overlap_ranges(self) -> None:
        from algorithm.knowledge_pipeline.chunking import chunk_document_items

        source = "x" * 1200
        expected = [[0, 500], [450, 950], [900, 1200]]
        for chunk_type in (0, 2, 3):
            items = chunk_document_items(
                source,
                chunk_type=chunk_type,
                chunk_size=500,
                chunk_overlap=50,
            )
            self.assertEqual([item["range"] for item in items], expected, chunk_type)
            self.assertTrue(all(source[a:b] == item["text"] for item in items for a, b in [item["range"]]))

    def test_json_whitespace_and_multiple_documents_keep_exact_ranges(self) -> None:
        from algorithm.knowledge_pipeline.chunking import chunk_document_items

        source = '```json\n{"a": 1}\n```\n  \n```json\n{"b": 2}\n```'
        items = chunk_document_items(source, chunk_type=1, chunk_size=20)
        self.assertTrue(all(source[item["range"][0]:item["range"][1]] == item["text"] for item in items))

        first = Document(page_content="repeat " * 8, metadata={"source": "first"})
        second = Document(page_content="repeat " * 8, metadata={"source": "second"})
        chunks = split_by_characters([first, second], chunk_size=20, chunk_overlap=3)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual({item.metadata["source"] for item in chunks}, {"first", "second"})

    def test_short_documents_keep_their_own_text_and_metadata(self) -> None:
        for overlap in (0, 3):
            with self.subTest(overlap=overlap):
                documents = [
                    Document(page_content="first text", metadata={"source": "first"}),
                    Document(page_content="second text", metadata={"source": "second"}),
                ]
                chunks = split_by_characters(documents, chunk_size=20, chunk_overlap=overlap)
                self.assertEqual(
                    [(item.page_content, item.metadata["source"], item.metadata["__source_range"]) for item in chunks],
                    [("first text", "first", [0, 10]), ("second text", "second", [0, 11])],
                )

    def test_only_protected_json_can_exceed_size_limit(self) -> None:
        payload = '```json\n' + json.dumps({"description": "x" * 800}) + '\n```'
        source = "a" * 1200 + "\n" + payload + "\n" + "b" * 700
        chunks = split_by_characters([Document(page_content=source)], chunk_size=500)
        oversized = [item.page_content for item in chunks if len(item.page_content) > 500]
        self.assertEqual(oversized, [payload])
        self.assertEqual("".join(item.page_content for item in chunks), source)

    def test_no_orphan_fence_chunk_and_chunks_stay_verbatim(self) -> None:
        source = ("第一段内容。" * 40) + "\n\n" + JSON_TABLE_MD + "\n结尾。"
        chunks = [
            item.page_content
            for item in split_by_characters([Document(page_content=source)], chunk_size=200)
        ]
        self.assertGreater(len(chunks), 1)
        # chunk_document_items 靠 content.find(text) 定位字符偏移：切片必须是原文子串。
        self.assertTrue(all(text in source for text in chunks))

        payload_chunks = [text for text in chunks if "columns" in text]
        self.assertEqual(len(payload_chunks), 1)
        self.assertIn("```json", payload_chunks[0])
        self.assertIn("\n```", payload_chunks[0])

    def test_reference_markers_not_torn_into_tiny_chunks(self) -> None:
        source = "规范正文。\n[1] GB/T 1.1—2020\n[2] GB/T 1.2—2020\n[3] Q/XX 001—2025\n"
        chunks = [
            item.page_content
            for item in split_by_characters([Document(page_content=source)], chunk_size=500)
        ]
        self.assertEqual(len(chunks), 1)
        self.assertIn("[1] GB/T 1.1—2020", chunks[0])
        self.assertIn("[3] Q/XX 001—2025", chunks[0])


if __name__ == "__main__":
    unittest.main()
