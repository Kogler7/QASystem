"""Phase 1 Step 3 单元测试：ChunkContext + Evidence。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v2/tests/test_chunk_evidence.py

覆盖：
- §5 输入契约：ExtractionRequest 只携带 chunks 与 target_types（text/ontology
  已在重构中移出请求对象）；
- §6 ChunkContextManager：索引、邻接、同结构上下文、有界拼接、span 定位；
- §14.3 Evidence：模型、from_dict/to_dict、evidence_type 校验；
- §16 L1/L2 验证与 AC-4 覆盖统计。
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    # pytest 全量运行时多个测试文件共享 sys.modules：已加载的模块直接复用，
    # 避免同名模块被重复 exec 出多个类对象（isinstance/类型一致性污染）。
    parent = sys.modules.get(package_name)
    if parent is None:
        parent = types.ModuleType(package_name)
        parent.__path__ = [str(directory)]
        parent.__package__ = package_name
        sys.modules[package_name] = parent
    loaded: dict[str, types.ModuleType] = {}
    for stem in stems:
        module_name = f"{package_name}.{stem}"
        if module_name in sys.modules:
            loaded[stem] = sys.modules[module_name]
            continue
        spec = importlib.util.spec_from_file_location(module_name, directory / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded[stem] = module
    return loaded


MODULES = _load_package_modules(
    "algorithm.knowledge_extract.v2",
    PACKAGE_DIR,
    ["graph_utils", "chunk_context", "evidence"],
)
CC = MODULES["chunk_context"]
EV = MODULES["evidence"]


def make_chunks() -> list[CC.ExtractionChunk]:
    return [
        CC.ExtractionChunk(
            chunk_id="c1",
            text="第一条。\n第二条 关于开展OpenClaw安全风险排查的通知。",
            section_id="s1",
            document_id="doc-1",
        ),
        CC.ExtractionChunk(
            chunk_id="c2",
            text="第三条 各企业于2026年4月10日前\n完成排查。",
            section_id="s1",
            document_id="doc-1",
            previous_chunk_id="c1",
        ),
        CC.ExtractionChunk(
            chunk_id="c3",
            text="另一章节的内容，与其他段落无关。",
            section_id="s2",
            document_id="doc-1",
            previous_chunk_id="c2",
        ),
    ]


class ExtractionRequestTest(unittest.TestCase):
    def test_single_chunk_request(self) -> None:
        request = CC.ExtractionRequest(
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text="正文内容")]
        )
        self.assertEqual(len(request.chunks), 1)
        self.assertEqual(request.chunks[0].chunk_id, "chunk-0")
        self.assertEqual(request.chunks[0].text, "正文内容")
        self.assertIsNone(request.target_types)

    def test_request_carries_chunks_and_target_types(self) -> None:
        request = CC.ExtractionRequest(
            chunks=make_chunks(),
            target_types=["OperationalActivity"],
        )
        self.assertEqual([chunk.chunk_id for chunk in request.chunks], ["c1", "c2", "c3"])
        self.assertEqual(request.target_types, ["OperationalActivity"])

    def test_empty_request(self) -> None:
        request = CC.ExtractionRequest(chunks=[])
        self.assertEqual(request.chunks, [])

    def test_chunk_roundtrip_dict(self) -> None:
        chunk = make_chunks()[0]
        restored = CC.ExtractionChunk.from_dict(chunk.to_dict())
        self.assertEqual(restored, chunk)


class ChunkContextManagerTest(unittest.TestCase):
    def test_duplicate_chunk_id_renamed(self) -> None:
        chunks = [
            CC.ExtractionChunk(chunk_id="c1", text="a"),
            CC.ExtractionChunk(chunk_id="c1", text="b"),
        ]
        warnings: list[dict] = []
        manager = CC.ChunkContextManager(chunks, warnings=warnings)
        self.assertEqual(chunks[0].chunk_id, "c1")
        self.assertEqual(chunks[1].chunk_id, "c1#dup2")
        self.assertTrue(manager.has_chunk("c1#dup2"))
        self.assertEqual([row["code"] for row in warnings], ["DUPLICATE_CHUNK_ID"])

    def test_get_chunk_unknown_raises(self) -> None:
        manager = CC.ChunkContextManager(make_chunks())
        with self.assertRaises(KeyError):
            manager.get_chunk("missing")

    def test_local_context_uses_explicit_links(self) -> None:
        manager = CC.ChunkContextManager(make_chunks())
        context = manager.local_context("c2", previous=1, next=1, same_section=False)
        self.assertEqual([chunk.chunk_id for chunk in context], ["c1", "c2", "c3"])

    def test_local_context_bounded(self) -> None:
        manager = CC.ChunkContextManager(make_chunks())
        # 默认 same_section=True：异章节邻居被过滤，仍是有界上下文
        self.assertEqual([chunk.chunk_id for chunk in manager.local_context("c2")], ["c1", "c2"])
        self.assertEqual(
            [chunk.chunk_id for chunk in manager.local_context("c1", previous=5, next=5, same_section=False)],
            ["c1", "c2", "c3"],
        )
        # previous=0 / next=0 只返回自身
        self.assertEqual(
            [chunk.chunk_id for chunk in manager.local_context("c3", previous=0, next=0, same_section=False)],
            ["c3"],
        )

    def test_same_section_filter(self) -> None:
        manager = CC.ChunkContextManager(make_chunks())
        context = manager.local_context("c2", previous=2, next=1, same_section=True)
        self.assertEqual([chunk.chunk_id for chunk in context], ["c1", "c2"])
        # same_section=False 时不按章节过滤
        context_all = manager.local_context("c2", previous=2, next=1, same_section=False)
        self.assertEqual([chunk.chunk_id for chunk in context_all], ["c1", "c2", "c3"])

    def test_context_text_joins_bounded(self) -> None:
        manager = CC.ChunkContextManager(make_chunks())
        text = manager.context_text("c2", previous=1, next=1, same_section=True)
        self.assertIn("第一条", text)
        self.assertIn("完成排查", text)
        self.assertNotIn("另一章节", text)

    def test_find_span_whitespace_insensitive(self) -> None:
        manager = CC.ChunkContextManager(make_chunks())
        start, end = manager.find_span("c2", "2026年4月10日前 \n 完成排查")
        self.assertIsNotNone(start)
        slice_text = manager.evidence_text("c2", start, end)
        self.assertIn("2026年4月10日前", slice_text)
        self.assertIn("完成排查", slice_text)
        self.assertIsNone(manager.find_span("c2", "不存在的文本"))

    def test_evidence_text_clamps_offsets(self) -> None:
        manager = CC.ChunkContextManager(make_chunks())
        self.assertEqual(manager.evidence_text("c1", 0, 3), "第一条")
        self.assertEqual(manager.evidence_text("c1", -5, 999), "第一条。\n第二条 关于开展OpenClaw安全风险排查的通知。")
        self.assertEqual(manager.evidence_text("c1", 10, 5), "")


class EvidenceModelTest(unittest.TestCase):
    def test_literal_evidence_preserves_boundary_whitespace(self) -> None:
        text = "\n附件原文\n\n"
        chunk = CC.ExtractionChunk("attachment", text)
        evidence = EV.Evidence("attachment:0:8", ["attachment"], text, 0, len(text), "structural")
        payload = evidence.to_dict()
        payload["span_id"] = f"attachment:0:{len(text)}"
        restored = EV.Evidence.from_dict(payload)
        self.assertEqual(restored.quote, text)
        verdicts = EV.verify_evidence({"entities": [{"evidence": payload}], "relations": []}, [chunk])
        self.assertTrue(verdicts)
        self.assertTrue(all(item.status == "ok" for item in verdicts))

    def test_invalid_evidence_type_rejected(self) -> None:
        with self.assertRaises(ValueError):
            EV.Evidence(span_id="c1:0:3", source_block_ids=["c1"], quote="第一条", evidence_type="guessed")

    def test_from_dict_tolerates_benchmark_style(self) -> None:
        evidence = EV.Evidence.from_dict({"quote": "第一条。"})
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.quote, "第一条。")
        self.assertEqual(evidence.evidence_type, "explicit")
        self.assertEqual(evidence.source_block_ids, [])
        self.assertIsNone(EV.Evidence.from_dict({}))

    def test_to_dict_roundtrip(self) -> None:
        evidence = EV.Evidence(
            span_id="c1:0:3",
            source_block_ids=["c1"],
            quote="第一条",
            start_char=0,
            end_char=3,
            evidence_type="explicit",
        )
        restored = EV.Evidence.from_dict(evidence.to_dict())
        self.assertEqual(restored, evidence)


class EvidenceBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = CC.ChunkContextManager(make_chunks())

    def test_locate_quote_across_chunks(self) -> None:
        evidence = EV.locate_quote(self.manager, "完成排查")
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.source_block_ids, ["c2"])
        self.assertEqual(evidence.span_id, f"c2:{evidence.start_char}:{evidence.end_char}")
        self.assertEqual(evidence.quote, "完成排查")

    def test_bind_existing_quote_fills_span(self) -> None:
        raw = {
            "entities": [
                {
                    "type": "OperationalActivity",
                    "name": "完成排查",
                    "properties": {},
                    "evidence": {"quote": "2026年4月10日前 完成排查"},
                }
            ],
            "relations": [],
        }
        bound = EV.bind_evidence(raw, self.manager)
        evidence = bound["entities"][0]["evidence"]
        self.assertEqual(evidence["source_block_ids"], ["c2"])
        self.assertIn("c2:", evidence["span_id"])
        self.assertGreater(evidence["end_char"], evidence["start_char"])

    def test_bind_fallback_by_name(self) -> None:
        raw = {
            "entities": [{"type": "OperationalActivity", "name": "开展OpenClaw安全风险排查", "properties": {}}],
            "relations": [],
        }
        bound = EV.bind_evidence(raw, self.manager)
        self.assertEqual(bound["entities"][0]["evidence"]["source_block_ids"], ["c1"])

    def test_bind_relations_fallback_endpoints(self) -> None:
        raw = {
            "entities": [],
            "relations": [
                {
                    "from_type": "OperationalActivity",
                    "from_name": "完成排查",
                    "rel_type": "PRECEDES",
                    "to_type": "OperationalActivity",
                    "to_name": "开展OpenClaw安全风险排查",
                }
            ],
        }
        bound = EV.bind_evidence(raw, self.manager)
        self.assertEqual(bound["relations"][0]["evidence"]["source_block_ids"], ["c2"])

    def test_bind_unlocatable_quote_preserved_without_fake_span(self) -> None:
        raw = {
            "entities": [
                {"type": "Terminology", "name": "X", "properties": {}, "evidence": {"quote": "原文不存在"}}
            ],
            "relations": [],
        }
        bound = EV.bind_evidence(raw, self.manager)
        self.assertEqual(bound["entities"][0]["evidence"], {"quote": "原文不存在"})


class EvidenceVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = CC.ChunkContextManager(make_chunks())

    def test_verify_ok(self) -> None:
        raw = EV.bind_evidence(
            {
                "entities": [{"type": "OperationalActivity", "name": "完成排查", "properties": {}}],
                "relations": [],
            },
            self.manager,
        )
        verdicts = EV.verify_evidence(raw, self.manager)
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].status, "ok")
        self.assertEqual(verdicts[0].level, "L2")

    def test_verify_missing_evidence(self) -> None:
        raw = {"entities": [{"type": "X", "name": "n", "properties": {}}], "relations": []}
        verdicts = EV.verify_evidence(raw, self.manager)
        self.assertEqual(verdicts[0].status, "fail")
        self.assertEqual(verdicts[0].level, "L1")
        self.assertIn("缺少 evidence", verdicts[0].message)

    def test_verify_unknown_source_block(self) -> None:
        raw = {
            "entities": [
                {
                    "type": "X",
                    "name": "n",
                    "properties": {},
                    "evidence": {"span_id": "missing:0:2", "source_block_ids": ["missing"], "quote": "第一条"},
                }
            ],
            "relations": [],
        }
        verdicts = EV.verify_evidence(raw, self.manager)
        self.assertEqual(verdicts[0].status, "fail")
        self.assertEqual(verdicts[0].level, "L1")

    def test_verify_unlocatable_quote(self) -> None:
        raw = {
            "entities": [
                {
                    "type": "X",
                    "name": "n",
                    "properties": {},
                    "evidence": {
                        "span_id": "c1:0:2",
                        "source_block_ids": ["c1"],
                        "quote": "完全不存在",
                        "start_char": 0,
                        "end_char": 2,
                    },
                }
            ],
            "relations": [],
        }
        verdicts = EV.verify_evidence(raw, self.manager)
        self.assertEqual(verdicts[0].status, "fail")
        self.assertEqual(verdicts[0].level, "L2")

    def test_verify_rejects_quote_at_different_span(self) -> None:
        raw = {
            "entities": [{
                "type": "X",
                "name": "n",
                "properties": {},
                "evidence": {
                    "span_id": "c1:0:3",
                    "source_block_ids": ["c1"],
                    "quote": "第二条",
                    "start_char": 0,
                    "end_char": 3,
                },
            }],
            "relations": [],
        }
        verdict = EV.verify_evidence(raw, self.manager)[0]
        self.assertEqual(verdict.status, "fail")
        self.assertIn("声明 span 不一致", verdict.message)

    def test_verify_rejects_span_id_offset_mismatch(self) -> None:
        raw = EV.bind_evidence(
            {"entities": [{"type": "X", "name": "第一条", "properties": {}}], "relations": []},
            self.manager,
        )
        raw["entities"][0]["evidence"]["start_char"] = 1
        verdict = EV.verify_evidence(raw, self.manager)[0]
        self.assertEqual(verdict.status, "fail")
        self.assertIn("span_id 与 offset 不一致", verdict.message)

    def test_verify_uses_span_block_not_first_source_block(self) -> None:
        raw = EV.bind_evidence(
            {"entities": [{"type": "X", "name": "完成排查", "properties": {}}], "relations": []},
            self.manager,
            chunk_ids=("c2",),
        )
        raw["entities"][0]["evidence"]["source_block_ids"] = ["missing", "c2"]
        verdict = EV.verify_evidence(raw, self.manager)[0]
        self.assertEqual(verdict.status, "ok")

    def test_verify_checks_every_evidence_not_only_first(self) -> None:
        first = EV.locate_quote(self.manager, "第一条")
        self.assertIsNotNone(first)
        raw = {
            "entities": [{
                "type": "X",
                "name": "n",
                "properties": {},
                "evidence": first.to_dict(),
                "evidences": [
                    first.to_dict(),
                    {
                        "span_id": "c1:0:3",
                        "source_block_ids": ["c1"],
                        "quote": "第二条",
                        "start_char": 0,
                        "end_char": 3,
                    },
                ],
            }],
            "relations": [],
        }
        verdicts = EV.verify_evidence(raw, self.manager)
        self.assertEqual([item.status for item in verdicts], ["ok", "fail"])
        self.assertEqual(verdicts[1].ref, "entities[0].evidences[1]")


class EvidenceCoverageTest(unittest.TestCase):
    def test_coverage_counts_and_by_type(self) -> None:
        raw = {
            "entities": [
                {"type": "A", "name": "a", "properties": {}, "evidence": {"quote": "x"}},
                {"type": "A", "name": "b", "properties": {}},
                {"type": "B", "name": "c", "properties": {}, "evidence": {"quote": "y"}},
            ],
            "relations": [{"from_name": "a", "to_name": "c", "rel_type": "R"}],
        }
        coverage = EV.evidence_coverage(raw)
        self.assertEqual(coverage["entities"], {"total": 3, "covered": 2, "rate": 2 / 3})
        self.assertEqual(coverage["relations"], {"total": 1, "covered": 0, "rate": 0.0})
        self.assertEqual(coverage["by_entity_type"]["A"]["covered"], 1)
        self.assertEqual(coverage["by_entity_type"]["B"]["rate"], 1.0)
        self.assertEqual(coverage["overall"]["total"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
