from types import SimpleNamespace

from algorithm.knowledge_extract.latest.official_attachments import (
    add_official_attachment_entities,
    build_official_attachment_entities,
)


def _prop(identifier, display=None):
    return SimpleNamespace(identifier=identifier, display_name=display or identifier)


def _plan(with_type=True, with_text=True, allowed=None):
    props = [_prop("informationName", "信息名称"), _prop("informationType", "信息类型")]
    if with_text:
        props.append(_prop("sourceText", "来源文本"))
    props.append(_prop("sourceFile", "来源文件"))
    specs = {}
    if with_type:
        specs["OperationalInformation"] = SimpleNamespace(
            class_id="OperationalInformation",
            display_name="操作信息",
            stereotypes=(),
            properties=tuple(props),
        )
    return SimpleNamespace(class_specs=specs, allowed_types=frozenset(specs if allowed is None else allowed))


def _chunk(chunk_id, text, source, document, start, end, role="attachment"):
    return SimpleNamespace(
        chunk_id=chunk_id,
        text=text,
        document_id=document,
        start_offset=start,
        end_offset=end,
        metadata={"source_file": source, "official_document_role": role},
    )


def test_attachment_chunks_merge_overlap_and_keep_exact_evidence():
    chunks = [
        _chunk("a1", "附件原文第一段", "附件1.docx", "doc-a", 0, 7),
        _chunk("a2", "第一段第二段", "附件1.docx", "doc-a", 4, 10),
    ]
    pool = SimpleNamespace(entity_candidate_count=0, allowed_types={"OperationalInformation"})
    warnings = []
    entities = build_official_attachment_entities(_plan(), pool, chunks, warnings=warnings)
    assert len(entities) == 1
    entity = entities[0]
    assert entity.name == "附件1.docx"
    assert entity.properties["sourceText"] == "附件原文第一段第二段"
    assert [item.quote for item in entity.evidence] == ["附件原文第一段", "第一段第二段"]
    assert entity.source_files == [{"source_file": "附件1.docx", "document_id": "doc-a"}]
    assert '"document_id":"doc-a"' in entity.source_identity
    assert not warnings


def test_same_filename_different_documents_do_not_merge():
    chunks = [
        _chunk("a", "甲", "附件.docx", "doc-a", 0, 1),
        _chunk("b", "乙", "附件.docx", "doc-b", 0, 1),
    ]
    pool = SimpleNamespace(entity_candidate_count=0, allowed_types={"OperationalInformation"})
    entities = build_official_attachment_entities(_plan(), pool, chunks)
    assert len(entities) == 2
    assert {entity.properties["sourceText"] for entity in entities} == {"甲", "乙"}


def test_addition_respects_target_scope_and_reports_missing_type():
    chunk = _chunk("a", "附件原文", "附件.txt", "doc-a", 0, 4)
    pool = SimpleNamespace(entity_candidate_count=0, allowed_types=set())
    warnings = []
    assert add_official_attachment_entities(_plan(with_type=False), pool, [chunk], warnings=warnings) == 0
    assert warnings[0]["code"] == "ATTACHMENT_TYPE_UNAVAILABLE"

    warnings = []
    assert add_official_attachment_entities(_plan(allowed=[]), pool, [chunk], warnings=warnings) == 0
    assert warnings[0]["code"] == "ATTACHMENT_TYPE_OUT_OF_SCOPE"


def test_source_text_limitation_is_explicit_when_property_missing():
    chunk = _chunk("a", "附件原文", "附件.txt", "doc-a", 0, 4)
    pool = SimpleNamespace(entity_candidate_count=0, allowed_types={"OperationalInformation"})
    warnings = []
    entities = build_official_attachment_entities(_plan(with_text=False), pool, [chunk], warnings=warnings)
    assert len(entities) == 1
    assert "ATTACHMENT_SOURCE_TEXT_UNMODELED" in {item["code"] for item in warnings}
    assert entities[0].evidence[0].quote == "附件原文"


def test_overlap_mismatch_warns_and_keeps_both_chunks():
    chunks = [
        _chunk("a", "abcdef", "附件.txt", "doc-a", 0, 6),
        _chunk("b", "XYZghi", "附件.txt", "doc-a", 3, 9),
    ]
    warnings = []
    pool = SimpleNamespace(entity_candidate_count=0, allowed_types={"OperationalInformation"})
    entity = build_official_attachment_entities(_plan(), pool, chunks, warnings=warnings)[0]
    assert entity.properties["sourceText"] == "abcdefXYZghi"
    assert any(item["code"] == "ATTACHMENT_OVERLAP_TEXT_MISMATCH" for item in warnings)
