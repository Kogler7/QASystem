"""Offline compatibility and exact provenance tests for line-ID citations."""
from copy import deepcopy

import pytest

from algorithm.knowledge_extract.v2.chunk_context import ChunkContextManager, ExtractionChunk
from algorithm.knowledge_extract.v3_1 import citations, extract_aggregate
from algorithm.knowledge_extract.v3_1.evidence import locate
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _catalog_id, _empty,
)


@pytest.mark.parametrize("text", ["", "单行没有换行", "一\n二\n", "一\r\n\r\n二\r三",
    "😀e\u0301\u2028中文 空格\u2029不允许\n", "  \t\n\n", "长" * 30000],
    ids=["empty", "single-line", "lf", "mixed-newlines", "unicode", "whitespace", "long-line"])
def test_source_rendering_reconstructs_every_character_and_utf8_byte(text):
    sources = [{"chunk_id": "doc:chunk", "text": text, "section_title": "标题", "metadata": {"x": [1]}}]
    before = deepcopy(sources)
    rendered = citations.render_sources(sources)
    assert sources == before
    assert "text" not in rendered[0]
    assert [s["span_id"] for s in rendered[0]["spans"]] == [
        f"doc:chunk:L{i}" for i in range(1, len(text.splitlines(keepends=True)) + 1)]
    restored = "".join(span["text"] for span in rendered[0]["spans"])
    assert restored == text
    assert restored.encode("utf-8") == text.encode("utf-8")
    assert rendered[0]["section_title"] == "标题"
    rendered[0]["metadata"]["x"].append(2)
    assert sources == before


def fixture_context():
    return ChunkContextManager([
        ExtractionChunk("chunk-0000", "😀甲\r\n从技术和管理两方面着手，严格杜 绝此类现象发生。\n不得安装。\n \t\n",
                        document_id="doc-a", section_id="sec", section_title="标题", page_no=2),
        ExtractionChunk("chunk:other:Lpart", '重复\n重复\n{"动作": "组织", "对象": "设备"}\n')])


def test_span_resolves_original_unicode_offsets_spaces_negation_and_metadata():
    context = fixture_context()
    result = locate([{"span_id": "chunk-0000:L2"}, {"span_id": "chunk-0000:L3"}], context, "wrong-default")
    assert len(result) == 2
    assert result[0] == {"quote": "从技术和管理两方面着手，严格杜 绝此类现象发生。\n",
                         "source_block_ids": ["chunk-0000"], "start_char": 4, "end_char": 29,
                         "document_id": "doc-a", "section_id": "sec", "section_title": "标题", "page_no": 2}
    assert result[1]["quote"] == "不得安装。\n"
    assert all(context.get_chunk("chunk-0000").text[x["start_char"]:x["end_char"]] == x["quote"] for x in result)
    assert all("span_id" not in x for x in result)


def test_multiline_json_is_multiple_original_spans_never_fabricated_joined_quote():
    text = '{\n  "a": "组织公司自建自用信息系统和员工",\n  "b": "电脑终端安装使用开源智能体风险排查工作"\n}\n'
    context = ChunkContextManager([ExtractionChunk("json", text)])
    evidence = locate([{"span_id": "json:L2"}, {"span_id": "json:L3"}], context, "json")
    assert len(evidence) == 2
    assert evidence[0]["quote"] == text.splitlines(keepends=True)[1]
    assert evidence[1]["quote"] == text.splitlines(keepends=True)[2]
    assert not locate({"chunk_id": "json", "quote": "组织公司自建自用信息系统和员工电脑终端安装使用开源智能体风险排查工作"}, context, "json")
    rendered = citations.render_sources([{"chunk_id": "json", "text": text}])[0]
    assert "text" not in rendered and "".join(s["text"] for s in rendered["spans"]) == text


def test_duplicate_lines_use_selected_position_and_cross_chunk_source():
    context = fixture_context()
    row = locate({"span_id": "chunk:other:Lpart:L2"}, context, "chunk-0000")[0]
    assert row == {"quote": "重复\n", "source_block_ids": ["chunk:other:Lpart"], "start_char": 3, "end_char": 6}
    assert locate([{"span_id": "chunk:other:Lpart:L2"}] * 2, context, "chunk-0000") == [row]


@pytest.mark.parametrize("span_id", [None, 1, "", "missing:L1", "chunk-0000:L0", "chunk-0000:L01",
    "chunk-0000:L-1", "chunk-0000:L١", "chunk-0000:L99", "chunk-0000:L4", "chunk-0000:L1:L2"])
def test_unknown_malformed_and_blank_span_ids_are_rejected(span_id):
    assert locate({"span_id": span_id}, fixture_context(), "chunk-0000") == []


@pytest.mark.parametrize("extra", [
    {"quote": "从技术和管理方面着手，严格杜 绝此类现象发生。\n"},
    {"quote": "从技术和管理两方面着手，严格杜绝此类现象发生。\n"},
    {"quote": "从技术和管理两方面着手，严格杜 绝此类现象发生。"},
    {"quote": None}, {"start_char": 0}, {"end_char": 9999}, {"start_char": True},
    {"source_block_ids": ["other"]}, {"source_block_ids": []}, {"chunk_id": "other"},
    {"source_chunk_id": "other"}, {"chunk_id": None}, {"document_id": "forged"},
])
def test_span_id_cannot_hide_forged_quote_offsets_or_source(extra):
    assert locate({"span_id": "chunk-0000:L2", **extra}, fixture_context(), "chunk-0000") == []


def test_redundant_consistent_fields_and_legacy_evidence_can_mix():
    context = fixture_context()
    anchor = locate({"span_id": "chunk-0000:L2"}, context, "chunk-0000")[0]
    assert locate({"span_id": "chunk-0000:L2", "chunk_id": "chunk-0000", "source_chunk_id": "chunk-0000", **anchor}, context, "x") == [anchor]
    assert locate([{"span_id": "chunk-0000:L2"}, {"quote": "不得安装。"}], context, "chunk-0000")[1]["quote"] == "不得安装。"
    assert locate([{"span_id": "chunk-0000:L2"}, {"span_id": "unknown:L1"}], context, "chunk-0000") == [anchor]


def test_one_unlocatable_citation_never_discards_its_valid_siblings():
    """Each citation is verified on its own; only unresolvable ones disappear."""
    context = fixture_context()
    anchor = locate({"span_id": "chunk-0000:L2"}, context, "chunk-0000")[0]
    assert locate([{"span_id": "chunk-0000:L2"}, {"span_id": "chunk-0000:L99"}], context, "chunk-0000") == [anchor]
    assert locate([{"span_id": "unknown:L1"}, {"span_id": "chunk-0000:L2"}], context, "chunk-0000") == [anchor]
    assert locate([{"span_id": "chunk-0000:L2"}, {"quote": "原文未出现的证据"},
                   {"span_id": "chunk-0000:L3"}, 1, None], context, "chunk-0000") == [
        anchor, locate({"span_id": "chunk-0000:L3"}, context, "chunk-0000")[0]]
    assert locate([{"span_id": "chunk-0000:L2", "quote": "伪造引用"}, {"span_id": "chunk-0000:L3"}],
                  context, "chunk-0000") == [locate({"span_id": "chunk-0000:L3"}, context, "chunk-0000")[0]]
    assert locate([{"span_id": "unknown:L1"}, {"span_id": "chunk-0000:L99"}, {"quote": " "}], context, "chunk-0000") == []


_SOURCE_METADATA = {"document_id": "doc-a", "section_id": "sec", "section_title": "标题", "page_no": 2}


@pytest.mark.parametrize("value,expected", [
    ({"quote": "严格杜 绝此类现象发生。"}, [{"quote": "严格杜 绝此类现象发生。", "source_block_ids": ["chunk-0000"], "start_char": 16, "end_char": 28, **_SOURCE_METADATA}]),
    ({"quote": "严格杜绝此类现象发生。"}, []),
    ({"quote": "重复", "source_chunk_id": "chunk:other:Lpart"}, [{"quote": "重复", "source_block_ids": ["chunk:other:Lpart"], "start_char": 0, "end_char": 2}]),
    ({"quote": "重复", "source_block_ids": ["chunk:other:Lpart"], "start_char": 3, "end_char": 5}, [{"quote": "重复", "source_block_ids": ["chunk:other:Lpart"], "start_char": 3, "end_char": 5}]),
    ({"quote": "重复", "chunk_id": "chunk:other:Lpart", "source_block_ids": ["chunk-0000"]}, []),
    ({"quote": "重复", "chunk_id": "chunk:other:Lpart", "start_char": 0, "end_char": 999}, []),
    ({"quote": "重复", "chunk_id": "chunk:other:Lpart", "start_char": True, "end_char": 2}, []),
    ({"quote": "重复", "chunk_id": "missing"}, []),
    ({"quote": " "}, []),
    ({"quote": "😀甲"}, [{"quote": "😀甲", "source_block_ids": ["chunk-0000"], "start_char": 0, "end_char": 2, **_SOURCE_METADATA}]),
    ([], []), (None, []), ([1], []),
    ([{"quote": "不得安装。"}, {"quote": "不得安装。"}], [{"quote": "不得安装。", "source_block_ids": ["chunk-0000"], "start_char": 29, "end_char": 34, **_SOURCE_METADATA}]),
])
def test_old_quote_formats_keep_their_public_anchors_and_failure_contract(value, expected):
    assert locate(value, fixture_context(), "chunk-0000") == expected


def test_extract_aggregate_restores_span_evidence_for_models_and_public_graph():
    """Use the real prompts/controller/harness path, with deterministic model peers."""
    first_line = "🧭 原文标题\r\n"
    fact_line = "甲负责乙，从技术和管理两方面着手，严格杜 绝此类现象发生。\n"
    text = first_line + fact_line
    ontology = "@startuml\nclass Actor\nclass Work\nActor --> Work : responsible\n@enduml"
    chunk = ExtractionChunk("source:Lalpha", text, document_id="doc-1", section_title="原文标题")

    def model(stage, payload, _context):
        source = next(item for item in payload["sources"] if item["chunk_id"] == chunk.chunk_id)
        assert "text" not in source
        assert "".join(item["text"] for item in source["spans"]) == text
        selected = next(item for item in source["spans"] if item["text"] == fact_line)
        assert selected["span_id"] == "source:Lalpha:L2"
        evidence = [{"span_id": selected["span_id"]}]
        if stage == "entities":
            return {"entities": [
                {"candidate_id": "actor", "type": "Actor", "name": "甲", "properties": {}, "evidence": evidence},
                {"candidate_id": "work", "type": "Work", "name": "乙", "properties": {}, "evidence": evidence},
            ], "alias_pairs": [], "issues": []}
        if stage == "relations":
            return {"relations": [{"from_id": _catalog_id(payload, "甲"), "rel_type": "responsible",
                                   "to_id": _catalog_id(payload, "乙"), "fact": "甲负责乙。", "evidence": evidence,
                                   "condition_status": "unconditional", "condition_entity_ids": []}],
                    "context_requests": [], "issues": []}
        return _empty(stage)

    decider = SemanticDecider()
    result = extract_aggregate(ontology, [chunk], llm=ScriptedLLM(model), decider=decider,
                               use_scheduler=False, max_repair_rounds=0)
    assert result["status"] == "succeeded"
    assert len(result["graph"]["nodes"]) == 2
    assert len(result["graph"]["relationships"]) == 1
    expected = {"quote": fact_line, "source_block_ids": [chunk.chunk_id], "start_char": len(first_line),
                "end_char": len(text), "document_id": "doc-1", "section_title": "原文标题"}
    for obj in result["intermediate_model"]["objects"]:
        assert obj["evidence"] == [expected]
    assert result["intermediate_model"]["relations"][0]["evidence"] == [expected]
    assert all(entity["evidence"] == expected for entity in result["raw"]["entities"])
    assert result["raw"]["relations"][0]["evidence"] == expected
    assert result["graph"]["relationships"][0]["evidences"] == [expected]
    assert {task.kind for task in decider.tasks} >= {"entity_support", "relation_support"}
    for task in decider.tasks:
        assert task.state["evidence_sets"][task.task_id] == [expected]
        assert "两方面" in task.state["evidence_sets"][task.task_id][0]["quote"]
        assert "杜 绝" in task.state["evidence_sets"][task.task_id][0]["quote"]
