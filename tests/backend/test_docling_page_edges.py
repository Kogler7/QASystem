"""Docling 路线的页眉/页脚补回逻辑测试（用假 converter，不加载模型）。"""
from __future__ import annotations

import pytest

from algorithm.preproc.preproc_src.processors import docx_processor
from algorithm.preproc.preproc_src.processors.docx_processor import DocxProcessor

PAGE_TEXT = {1: "封面正文", 2: "第二页正文", 3: "第三页正文"}


class _FakeDocument:
    def __init__(self, pages):
        self.pages = pages

    def export_to_markdown(self, page_no=None, **_kwargs):
        return PAGE_TEXT[page_no]


class _FakeResult:
    def __init__(self, pages):
        self.document = _FakeDocument(pages)


class _FakeConverter:
    def __init__(self, pages):
        self.pages = pages
        self.calls: list[dict] = []

    def convert(self, _path, **kwargs):
        self.calls.append(kwargs)
        return _FakeResult(self.pages)


@pytest.fixture()
def fake_converter(monkeypatch):
    converter = _FakeConverter({1: {}, 2: {}, 3: {}})
    processor = DocxProcessor()
    monkeypatch.setattr(processor, "_get_pdf_converter", lambda: converter)
    # 页边文本来自 PDF 文本层，这里直接给定，避免依赖真实文件。
    monkeypatch.setattr(
        docx_processor,
        "extract_edge_lines",
        lambda *_args, **_kwargs: {"top": ["ICS 01.120"], "bottom": ["2026-01-28发布"]},
    )
    return processor, converter


def test_page_range_is_omitted_when_not_bounded(fake_converter):
    """回归：docling 不接受显式 page_range=None，整篇解析不能传该参数。"""
    processor, converter = fake_converter
    processor._process_pdf("x.pdf", start_page_id=0, end_page_id=None, keep_page_edges="off")
    assert converter.calls == [{}]


def test_page_range_is_converted_to_one_based_inclusive(fake_converter):
    processor, converter = fake_converter
    processor._process_pdf("x.pdf", start_page_id=0, end_page_id=6, keep_page_edges="off")
    assert converter.calls == [{"page_range": (1, 7)}]


def test_keep_page_edges_first_page_only(fake_converter):
    processor, _ = fake_converter
    markdown, metadata = processor._process_pdf("x.pdf", keep_page_edges="first_page")

    injected = metadata["page_edges"]["injected"]
    assert [item["page"] for item in injected] == [1]
    assert injected[0]["top"] == ["ICS 01.120"]
    # 页眉在该页正文之前，页脚在其后
    assert markdown.index("ICS 01.120") < markdown.index("封面正文") < markdown.index("2026-01-28发布")
    assert markdown.count("ICS 01.120") == 1


def test_keep_page_edges_explicit_pages(fake_converter):
    processor, _ = fake_converter
    markdown, metadata = processor._process_pdf("x.pdf", keep_page_edges=[2])
    assert [item["page"] for item in metadata["page_edges"]["injected"]] == [2]
    assert markdown.count("ICS 01.120") == 1
    assert "第二页正文" in markdown


def test_keep_page_edges_all_and_off(fake_converter):
    processor, _ = fake_converter
    markdown, metadata = processor._process_pdf("x.pdf", keep_page_edges="all")
    assert [item["page"] for item in metadata["page_edges"]["injected"]] == [1, 2, 3]
    assert markdown.count("ICS 01.120") == 3

    markdown, metadata = processor._process_pdf("x.pdf", keep_page_edges="off")
    assert metadata["page_edges"] == {"mode": "off", "injected": []}
    assert "ICS 01.120" not in markdown


def test_existing_edge_text_is_not_duplicated(fake_converter, monkeypatch):
    processor, _ = fake_converter
    monkeypatch.setattr(
        docx_processor,
        "extract_edge_lines",
        lambda *_args, **_kwargs: {"top": ["封面正文"], "bottom": []},
    )
    markdown, metadata = processor._process_pdf("x.pdf", keep_page_edges="first_page")
    assert metadata["page_edges"]["injected"] == []
    assert markdown.count("封面正文") == 1
