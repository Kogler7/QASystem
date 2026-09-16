"""文本层对账（preproc_src/text_layer.py）的单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from algorithm.preproc.preproc_src.text_layer import (
    build_line_index,
    merge_split_headings,
    normalize_for_compare,
    page_text_lines,
    reconcile_markdown,
    split_markdown_line,
    tighten_number_artifacts,
)

STANDARD_PDF = (
    Path(__file__).resolve().parents[2]
    / "test_artifacts/parser/解析测试数据"
    / "GBT+48000.4-2026 标准数字化 第4部分：协同制定要求.pdf"
)
requires_standard_pdf = pytest.mark.skipif(
    not STANDARD_PDF.is_file(), reason="缺少 GB/T 48000.4—2026 测试文件"
)


def test_normalize_for_compare_strips_all_whitespace():
    assert normalize_for_compare(" GB / T1 . 1 ") == "GB/T1.1"
    assert normalize_for_compare("4 . 1\t概述") == "4.1概述"
    assert normalize_for_compare("") == ""


def test_split_markdown_line_keeps_structure_prefix():
    assert split_markdown_line("## 4 . 1 概述") == ("## ", "4 . 1 概述")
    assert split_markdown_line("- 第1部分:通用指南") == ("- ", "第1部分:通用指南")
    assert split_markdown_line("> 注:内容") == ("> ", "注:内容")
    assert split_markdown_line("  正文") == ("  ", "正文")
    assert split_markdown_line("| 查看 | 编辑 |") == ("", "| 查看 | 编辑 |")


def test_build_line_index_marks_ambiguous_keys_unusable():
    index = build_line_index(["4.1 概述", "4 . 1 概述", "唯一行"])
    assert index["4.1概述"] == ""      # 两个候选，放弃替换
    assert index["唯一行"] == "唯一行"


def test_reconcile_restores_clause_number_and_punctuation():
    engine = (
        "# 4 . 1 概述\n"
        "\n"
        "GB / T1 . 1 标准化工作导则 第 1 部分 : 标准化文件的结构和起草规则\n"
        "\n"
        "本文件规定了协同环境 、 数据管理 。\n"
    )
    lines = [
        "4.1 概述",
        "GB/T1.1 标准化工作导则 第1部分:标准化文件的结构和起草规则",
        "本文件规定了协同环境、数据管理。",
    ]
    reconciled, replaced = reconcile_markdown(engine, lines)
    assert replaced == 3
    assert "# 4.1 概述" in reconciled
    assert "GB/T1.1 标准化工作导则 第1部分:标准化文件的结构和起草规则" in reconciled
    assert "本文件规定了协同环境、数据管理。" in reconciled
    assert "4 . 1" not in reconciled


def test_reconcile_leaves_unmatched_and_structural_lines_alone():
    engine = "| 相关方角色 | 查看 、 编辑 |\n\n<!-- image -->\n\n没有对应文本层的行\n"
    reconciled, replaced = reconcile_markdown(engine, ["4.1 概述"])
    assert replaced == 0
    assert reconciled == engine


def test_reconcile_without_text_layer_is_noop():
    engine = "# 4 . 1 概述\n"
    assert reconcile_markdown(engine, []) == (engine, 0)


@requires_standard_pdf
def test_page_text_lines_reads_cover_and_body():
    cover = page_text_lines(STANDARD_PDF, 1)
    assert any("ICS" in line for line in cover), cover
    body = page_text_lines(STANDARD_PDF, 7)
    assert any(line.startswith("4.1") for line in body), body
    assert page_text_lines(STANDARD_PDF, 999) == []


@requires_standard_pdf
def test_reconcile_fixes_docling_style_spacing_with_real_text_layer():
    # Docling 会把 "4.1 概述" 导出成 "## 4 . 1 概述"
    engine = "## 4 . 1 概述\n\n## GB / T4 8 0 0 0 . 1 标准数字化 第 1 部分 : 通用指南\n"
    reconciled, replaced = reconcile_markdown(engine, page_text_lines(STANDARD_PDF, 7))
    assert replaced >= 1
    assert "## 4.1 概述" in reconciled
    assert "GB/T48000.1 标准数字化 第1部分:通用指南" in reconciled


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("## 4 . 5 . 1 应支持", "## 4.5.1 应支持"),
        ("见 5. 2)", "见 5.2)"),
        ("GB / T4 8 0 0 0 . 1 标准数字化", "GB/T48000.1 标准数字化"),
        ("2026-01-28 发布", "2026-01-28 发布"),
        ("2 0 2 6-0 1-2 8", "2026-01-28"),
        ("ISO / IEC Directives", "ISO/IEC Directives"),
        # 正常的英文与中文空格不受影响
        ("Part 4 与第 4 部分", "Part 4 与第 4 部分"),
        ("表 A. 6", "表 A.6"),
    ],
)
def test_tighten_number_artifacts(raw, expected):
    assert tighten_number_artifacts(raw) == expected


def test_merge_split_headings_merges_only_when_text_layer_confirms():
    markdown = "## 6\n\n## . 1 0 废止阶段\n\n正文"
    merged, count = merge_split_headings(markdown, ["6.10 废止阶段"])
    assert count == 1
    assert merged.startswith("## 6.10 废止阶段")
    assert ". 1 0" not in merged

    unchanged, count = merge_split_headings(markdown, ["与此无关的行"])
    assert count == 0
    assert unchanged == markdown


def test_merge_split_headings_skips_non_numeric_heading():
    markdown = "## 通用要求\n\n## 4.1 概述\n"
    merged, count = merge_split_headings(markdown, ["通用要求4.1概述"])
    assert count == 0 and merged == markdown


@requires_standard_pdf
def test_real_text_layer_confirms_split_heading_merge():
    # 第 12 页文本层包含 "6.10 废止阶段"
    lines = page_text_lines(STANDARD_PDF, 12)
    merged, count = merge_split_headings("## 6\n\n## . 1 0 废止阶段\n", lines)
    assert count == 1
    assert "## 6.10 废止阶段" in merged
