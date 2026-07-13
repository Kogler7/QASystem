from algorithm.preproc.preproc_src.main_processor import DocumentProcessor


def test_csv_always_uses_pandas(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("a,b\n1,2", encoding="utf-8")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    engine, details = processor.select_engine(str(path))
    assert engine == "pandas"
    assert details["mode"] == "auto"


def test_incompatible_engine_is_rejected_before_loading(tmp_path):
    path = tmp_path / "sample.html"
    path.write_text("<p>test</p>", encoding="utf-8")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    result = processor.process_file(str(path), model_name="mineru")
    assert result["success"] is False
    assert result["error_code"] == "ENGINE_NOT_SUPPORTED"


def test_text_pdf_prefers_docling(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_pdf", lambda _path: {"kind": "text", "reason": "test"})
    engine, _ = processor.select_engine(str(path))
    assert engine == "docling"


def test_scanned_pdf_prefers_mineru(tmp_path, monkeypatch):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(processor, "_profile_pdf", lambda _path: {"kind": "complex_or_scanned", "reason": "test"})
    engine, _ = processor.select_engine(str(path))
    assert engine == "mineru"
