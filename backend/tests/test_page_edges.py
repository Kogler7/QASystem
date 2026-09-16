"""页眉/页脚保留（preproc_src/page_edges.py）与 PDF 版面画像的单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
from algorithm.preproc.preproc_src.page_edges import (
    extract_edge_lines,
    lines_from_blocks,
    merge_page_edges,
    missing_lines,
    normalize_keep_page_edges,
    page_is_kept,
)

STANDARD_PDF = (
    Path(__file__).resolve().parents[2]
    / "test_artifacts/parser/解析测试数据"
    / "GBT+48000.4-2026 标准数字化 第4部分：协同制定要求.pdf"
)
requires_standard_pdf = pytest.mark.skipif(
    not STANDARD_PDF.is_file(), reason="缺少 GB/T 48000.4—2026 测试文件"
)


# ---------------------------------------------------------------- 选项归一化

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (False, None),
        ("", None),
        ("off", None),
        ("OFF", None),
        (True, "first_page"),
        ("first_page", "first_page"),
        ("first", "first_page"),
        ("all", "all"),
        ([1, 3], {1, 3}),
        ((2,), {2}),
    ],
)
def test_normalize_keep_page_edges_accepts_documented_values(value, expected):
    assert normalize_keep_page_edges(value) == expected


@pytest.mark.parametrize("value", ["bogus", 7, [0], [-1], ["1"], [1, "a"], object()])
def test_normalize_keep_page_edges_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_keep_page_edges(value)


# ---------------------------------------------------------------- 页选择语义

def test_first_page_means_first_page_of_parsed_range():
    assert page_is_kept("first_page", 1, 1) is True
    assert page_is_kept("first_page", 2, 1) is False
    # 分片解析时首页不是文档第 1 页
    assert page_is_kept("first_page", 5, 5) is True
    assert page_is_kept("first_page", 6, 5) is False


def test_all_and_explicit_pages():
    assert page_is_kept("all", 4, 1) is True
    assert page_is_kept("all", 99, 1) is True
    assert page_is_kept({2, 4}, 4, 1) is True
    assert page_is_kept({2, 4}, 3, 1) is False
    assert page_is_kept(None, 1, 1) is False


# ---------------------------------------------------------------- 去重与拼装

def test_missing_lines_skips_lines_already_rendered():
    body = "# 中华人民共和国国家标准\n\n国家市场监督管理总局\n国家标准化管理委员会"
    lines = ["ICS 01.120", "国家市场监督管理总局", "CCS A 00", "国家标准化管理委员会"]
    assert missing_lines(lines, body) == ["ICS 01.120", "CCS A 00"]


def test_missing_lines_ignores_whitespace_and_punctuation_differences():
    # 引擎输出常带空格或全角/半角差异，不应因此重复补回
    assert missing_lines(["GB/T 48000.4—2026"], "## GB/T48000.4—2026") == []
    assert missing_lines(["2026-01-28发布"], "2026-01-28 发布") == []


def test_missing_lines_drops_empty_and_duplicate_candidates():
    assert missing_lines(["", "  ", "ICS", "ICS"], "正文") == ["ICS"]


def test_merge_page_edges_puts_header_first_and_footer_last():
    merged = merge_page_edges("正文", ["ICS 01.120"], ["2026-01-28发布"])
    assert merged.splitlines() == ["ICS 01.120", "", "正文", "", "2026-01-28发布"]
    # 没有补回内容时保持原样
    assert merge_page_edges("正文", [], []) == "正文"
    assert merge_page_edges("", ["ICS 01.120"], []) == "ICS 01.120"


def test_lines_from_blocks_filters_short_and_extracts_spans():
    blocks = [
        {"lines": [
            {"spans": [{"content": "ICS"}, {"content": "01.120"}]},
            {"spans": [{"content": "GB"}]},
            {"spans": [{"content": "  "}]},
            {"spans": [{"content": "2026-01-28"}, {"content": "发布"}]},
        ]}
    ]
    assert lines_from_blocks(blocks, min_chars=3) == ["ICS01.120", "2026-01-28发布"]
    assert lines_from_blocks(blocks, min_chars=1) == ["ICS01.120", "GB", "2026-01-28发布"]


# ---------------------------------------------------------------- 真实文件

@requires_standard_pdf
def test_extract_edge_lines_reads_cover_ics_and_ccs():
    edges = extract_edge_lines(STANDARD_PDF, 1)
    assert any("ICS" in line for line in edges["top"]), edges
    assert any("CCS" in line for line in edges["top"]), edges
    assert any("2026-01-28" in line for line in edges["bottom"]), edges


@requires_standard_pdf
def test_extract_edge_lines_handles_out_of_range_page():
    assert extract_edge_lines(STANDARD_PDF, 999) == {"top": [], "bottom": []}


@requires_standard_pdf
def test_cover_edges_survive_merge_when_engine_dropped_them():
    """回归：MinerU/Docling 都会丢掉封面左上角的 ICS/CCS，补回后必须可见。"""
    engine_markdown = "# 中 华 人 民 共 和 国 国 家 标 准\n\nGB/T48000.4—2026\n\n# 标准数字化 第4部分:协同制定要求"
    edges = extract_edge_lines(STANDARD_PDF, 1)
    top = missing_lines(edges["top"], engine_markdown)
    bottom = missing_lines(edges["bottom"], engine_markdown)
    merged = merge_page_edges(engine_markdown, top, bottom)
    assert "ICS" in merged and "CCS" in merged, merged
    assert "2026-01-28" in merged and "2026-08-01" in merged, merged


@requires_standard_pdf
def test_profile_ignores_decorative_watermark_images():
    processor = DocumentProcessor(output_dir="/tmp/qasystem-test-profile")
    profile = processor._profile_pdf(str(STANDARD_PDF))
    assert profile["kind"] == "text", profile
    assert profile["images"] == 0, profile
    assert profile["decorative_images_ignored"] > 0, profile


@requires_standard_pdf
def test_text_pdf_routes_to_docling_after_profile_fix():
    processor = DocumentProcessor(output_dir="/tmp/qasystem-test-profile")
    engine, selection = processor.select_engine(str(STANDARD_PDF))
    assert engine == "docling", selection


@requires_standard_pdf
def test_profile_falls_back_to_raw_image_count(monkeypatch):
    processor = DocumentProcessor(output_dir="/tmp/qasystem-test-profile")
    monkeypatch.setattr(processor, "_content_image_count", lambda *_: None)
    profile = processor._profile_pdf(str(STANDARD_PDF))
    # 退回 pypdf 的对象计数时，水印会把纯文本 PDF 判成复杂版面
    assert profile["images"] == profile["raw_images"] > 0
    assert profile["kind"] == "complex_or_scanned"


# ---------------------------------------------------------------- 参数校验

def test_process_file_rejects_invalid_keep_page_edges(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")
    result = DocumentProcessor(output_dir=str(tmp_path / "out")).process_file(
        str(sample), keep_page_edges="sometimes"
    )
    assert result["success"] is False
    assert result["error_code"] == "INVALID_KEEP_PAGE_EDGES"


def test_process_file_rejects_invalid_page_edge_min_chars(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")
    result = DocumentProcessor(output_dir=str(tmp_path / "out")).process_file(
        str(sample), page_edge_min_chars=0
    )
    assert result["success"] is False
    assert result["error_code"] == "INVALID_PAGE_EDGE_MIN_CHARS"
