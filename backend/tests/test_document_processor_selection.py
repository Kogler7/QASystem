import zipfile

from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
from algorithm.preproc.preproc_src.processors.ocr_processor import OCRProcessor


def test_csv_always_uses_pandas(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("a,b\n1,2", encoding="utf-8")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    engine, details = processor.select_engine(str(path))
    assert engine == "pandas"
    assert details["mode"] == "auto"


def test_incompatible_engine_is_ignored(tmp_path):
    path = tmp_path / "sample.html"
    path.write_text("<p>test</p>", encoding="utf-8")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    engine, details = processor.select_engine(str(path), requested="mineru")
    assert engine == "beautifulsoup"
    assert details["preferred_engine"] == "beautifulsoup"
    assert details["request_compatible"] is False
    assert details["request_role"] == "ignored_incompatible"


def test_text_pdf_prefers_docling(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_pdf", lambda _path: {"kind": "text", "reason": "test"})
    engine, _ = processor.select_engine(str(path))
    assert engine == "docling"


def test_scanned_pdf_prefers_vlm(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_pdf", lambda _path: {"kind": "complex_or_scanned", "reason": "test"})
    engine, _ = processor.select_engine(str(path))
    assert engine == "vlm"


def test_force_selects_requested_engine_even_when_profile_disagrees(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_pdf", lambda _path: {"kind": "text", "reason": "test"})

    engine, details = processor.select_engine(str(path), requested="vlm", force=True)

    assert engine == "vlm"
    assert details["mode"] == "forced"
    assert details["force"] is True
    assert details["request_role"] == "forced"


def test_force_rejects_incompatible_engine(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("a,b\n1,2", encoding="utf-8")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))

    try:
        processor.select_engine(str(path), requested="vlm", force=True)
    except ValueError as exc:
        assert "不支持强制解析引擎" in str(exc)
    else:
        raise AssertionError("incompatible forced engine should be rejected")


def test_ofd_is_converted_then_uses_pdf_selection(tmp_path, monkeypatch):
    path = tmp_path / "sample.ofd"
    path.write_bytes(b"ofd-test")
    conversion_dir = tmp_path / "conversion"
    conversion_dir.mkdir()
    converted = conversion_dir / "converted.pdf"
    converted.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_pdf", lambda _path: {"kind": "text", "reason": "test"})

    class FakeConverter:
        last_conversion_error = None

        def convert_to_pdf(self, _path):
            return str(converted)

    monkeypatch.setattr(processor, "_ofd_converter", lambda: FakeConverter())
    monkeypatch.setattr(
        processor,
        "_run_processor",
        lambda engine, file_path, suffix, *_args, **_kwargs: {
            "success": True,
            "content": "parsed",
            "metadata": {"processed_path": file_path, "processed_suffix": suffix, "engine": engine},
        },
    )

    result = processor.process_file(str(path))

    assert result["success"] is True
    assert result["metadata"]["model_used"] == "docling"
    assert result["metadata"]["engine_selection"]["source_file_type"] == ".ofd"
    assert result["metadata"]["processed_suffix"] == ".pdf"


def test_ofd_conversion_failure_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "sample.ofd"
    path.write_bytes(b"ofd-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))

    class FakeConverter:
        last_conversion_error = "OFD 转换为 PDF 失败：invalid OFD"

        def convert_to_pdf(self, _path):
            return None

    monkeypatch.setattr(processor, "_ofd_converter", lambda: FakeConverter())

    result = processor.process_file(str(path))

    assert result["success"] is False
    assert result["error_code"] == "DOCUMENT_CONVERT_FAILED"
    assert "invalid OFD" in result["error"]


def test_requested_mineru_is_only_a_hint_for_text_docx(tmp_path, monkeypatch):
    path = tmp_path / "sample.docx"
    path.write_bytes(b"docx-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_docx", lambda _path: {"kind": "text", "reason": "test"})

    engine, details = processor.select_engine(str(path), requested="mineru")

    assert engine == "docling"
    assert details["mode"] == "auto"
    assert details["requested_engine"] == "mineru"
    assert details["request_role"] == "fallback_candidate"
    assert details["request_matched_selection"] is False


def test_requested_docling_does_not_override_scanned_pdf_profile(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_pdf", lambda _path: {"kind": "complex_or_scanned", "reason": "test"})

    engine, details = processor.select_engine(str(path), requested="docling")

    assert engine == "vlm"
    assert details["preferred_engine"] == "vlm"
    assert details["requested_engine"] == "docling"
    assert details["request_role"] == "fallback_candidate"
    assert details["request_matched_selection"] is False


def test_complex_docx_honors_requested_engine(tmp_path, monkeypatch):
    path = tmp_path / "sample.docx"
    path.write_bytes(b"docx-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_docx", lambda _path: {"kind": "complex", "reason": "test"})

    engine, details = processor.select_engine(str(path), requested="docling")

    assert engine == "docling"
    assert details["preferred_engine"] == "vlm"
    assert details["request_role"] == "selected_primary"
    assert details["request_matched_selection"] is True


def test_complex_docx_defaults_to_vlm_without_request(tmp_path, monkeypatch):
    path = tmp_path / "sample.docx"
    path.write_bytes(b"docx-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_docx", lambda _path: {"kind": "complex", "reason": "test"})

    engine, details = processor.select_engine(str(path))

    assert engine == "vlm"
    assert "requested_engine" not in details


def test_requested_engine_is_preferred_when_primary_engine_fails(tmp_path, monkeypatch):
    path = tmp_path / "sample.docx"
    path.write_bytes(b"docx-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_docx", lambda _path: {"kind": "text", "reason": "test"})
    calls = []

    def fake_run(engine, *_args, **_kwargs):
        calls.append(engine)
        if engine == "docling":
            return {"success": False, "error": "docling failed"}
        return {"success": True, "content": "parsed", "metadata": {}}

    monkeypatch.setattr(processor, "_run_processor", fake_run)

    result = processor.process_file(str(path), model_name="mineru")

    assert result["success"] is True
    assert calls == ["docling", "mineru"]
    assert result["metadata"]["model_used"] == "mineru"
    assert result["metadata"]["fallback_from"]["engine"] == "docling"


def test_text_pdf_falls_back_to_requested_mineru_when_docling_fails(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_pdf", lambda _path: {"kind": "text", "reason": "test"})
    calls = []

    def fake_run(engine, *_args, **_kwargs):
        calls.append(engine)
        if engine == "docling":
            return {"success": False, "error": "docling model unavailable"}
        return {"success": True, "content": "parsed", "metadata": {}}

    monkeypatch.setattr(processor, "_run_processor", fake_run)

    result = processor.process_file(str(path), model_name="mineru")

    assert result["success"] is True
    assert calls == ["docling", "mineru"]
    assert result["metadata"]["model_used"] == "mineru"
    assert result["metadata"]["fallback_from"] == {
        "engine": "docling",
        "error": "docling model unavailable",
    }


def test_no_fallback_is_attempted_without_a_compatible_user_request(tmp_path, monkeypatch):
    path = tmp_path / "sample.docx"
    path.write_bytes(b"docx-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_docx", lambda _path: {"kind": "text", "reason": "test"})
    calls = []

    def fake_run(engine, *_args, **_kwargs):
        calls.append(engine)
        return {"success": False, "error": "failed"}

    monkeypatch.setattr(processor, "_run_processor", fake_run)

    result = processor.process_file(str(path))

    assert result["success"] is False
    assert calls == ["docling"]


def test_markdown_is_read_directly_and_ignores_requested_engine(tmp_path):
    path = tmp_path / "sample.md"
    path.write_text("# 标题\n正文", encoding="utf-8")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))

    result = processor.process_file(str(path), model_name="mineru")

    assert result["success"] is True
    assert result["content"].splitlines() == ["# 标题", "正文"]
    assert result["metadata"]["model_used"] == "text"
    assert result["metadata"]["engine_selection"]["request_role"] == "ignored_incompatible"


def test_plain_text_is_read_by_text_engine(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("第一行\n第二行", encoding="utf-8")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))

    result = processor.process_file(str(path), model_name="vlm")

    assert result["success"] is True
    assert result["content"].splitlines() == ["第一行", "第二行"]
    assert result["metadata"]["model_used"] == "text"
    assert result["metadata"]["engine_selection"]["request_role"] == "ignored_incompatible"

    engine, details = processor.select_engine(str(path), requested="text")
    assert engine == "text"
    assert details["preferred_engine"] == "text"
    assert details["request_role"] == "matched_default"


def test_new_image_and_presentation_formats_have_expected_routes(tmp_path):
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    expected_defaults = {
        ".png": "vlm",
        ".jpg": "vlm",
        ".jpeg": "vlm",
        ".ppt": "docling",
        ".pptx": "docling",
    }

    for suffix, expected_engine in expected_defaults.items():
        path = tmp_path / f"sample{suffix}"
        path.write_bytes(b"test")
        engine, details = processor.select_engine(str(path))
        assert engine == expected_engine
        assert details["allowed_engines"] == list(processor.ENGINE_MATRIX[suffix])


def test_user_engine_is_one_time_fallback_for_image_and_presentation(tmp_path):
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    cases = [(".png", "docling", "vlm"), (".pptx", "mineru", "docling")]

    for suffix, requested, expected_primary in cases:
        path = tmp_path / f"sample{suffix}"
        path.write_bytes(b"test")
        engine, details = processor.select_engine(str(path), requested=requested)
        assert engine == expected_primary
        assert details["fallback_engine"] == requested
        assert details["request_role"] == "fallback_candidate"


def test_mineru_and_vlm_names_select_distinct_backends(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    calls = []

    class FakeMinerURuntime:
        def process(self, _path, **kwargs):
            calls.append(kwargs)
            return {"content": "parsed", "metadata": {"backend": kwargs["backend"]}}

    monkeypatch.setattr(processor, "_processor", lambda _engine: FakeMinerURuntime())

    pipeline_result = processor._run_processor(
        "mineru", str(path), ".pdf", "ch", "on", False, False
    )
    vlm_result = processor._run_processor(
        "vlm", str(path), ".pdf", "ch", "off", False, False
    )

    assert calls[0]["backend"] == "pipeline"
    assert calls[0]["method"] == "ocr"
    assert calls[0]["table_enable"] is False
    assert calls[0]["formula_enable"] is False
    assert pipeline_result["metadata"]["mineru_options"] == {
        "language": "ch",
        "ocr_mode": "on",
        "table_enable": False,
        "formula_enable": False,
    }
    assert calls[1]["backend"] == "vlm-http-client"
    assert "mineru_options" not in vlm_result["metadata"]


def test_bounded_page_range_accepts_empty_processor_content(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))

    class EmptyPageRuntime:
        def process(self, _path, **_kwargs):
            return {"content": "", "metadata": {"backend": "vlm-http-client"}}

    monkeypatch.setattr(processor, "_processor", lambda _engine: EmptyPageRuntime())

    complete_document = processor._run_processor("vlm", str(path), ".pdf", "ch")
    page_shard = processor._run_processor(
        "vlm",
        str(path),
        ".pdf",
        "ch",
        start_page_id=45,
        end_page_id=46,
        allow_empty_content=True,
    )

    assert complete_document["success"] is False
    assert page_shard == {
        "success": True,
        "content": "",
        "metadata": {"backend": "vlm-http-client", "empty_content": True},
    }


def test_invalid_ocr_mode_is_rejected(tmp_path):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))

    result = processor.process_file(str(path), ocr_mode="sometimes")

    assert result["success"] is False
    assert result["error_code"] == "INVALID_OCR_MODE"


def test_mineru_model_validation_only_checks_selected_route(tmp_path):
    pipeline_path = tmp_path / "pipeline"
    pipeline_path.mkdir()
    config = {"pipeline": str(pipeline_path), "vlm": str(tmp_path / "missing-vlm")}

    OCRProcessor._validate_local_models_config(config, "pipeline")

    try:
        OCRProcessor._validate_local_models_config(config, "vlm")
    except ValueError as exc:
        assert "vlm=" in str(exc)
    else:
        raise AssertionError("missing VLM model path should be rejected for VLM route")


def test_docx_profile_counts_actual_elements_instead_of_tag_prefixes(tmp_path):
    path = tmp_path / "sample.docx"
    document_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:tbl><w:tblPr/><w:tblGrid/></w:tbl>
      </w:body>
    </w:document>
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))

    profile = processor._profile_docx(str(path))

    assert profile["tables"] == 1
    assert profile["kind"] == "text"
