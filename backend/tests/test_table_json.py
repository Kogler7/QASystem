"""表格转 JSON（preproc_src/table_json.py）与各解析路线接线测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from algorithm.preproc.preproc_src.table_json import (
    convert_tables_to_json,
    grid_from_html_table,
    html_tables_to_json,
    markdown_tables_to_json,
    merge_grouped_header,
    render_json_block,
    table_from_columns_and_rows,
    table_from_grid,
)
from algorithm.preproc.preproc_src.processors import docx_processor, ocr_processor

REPO_ROOT = Path(__file__).resolve().parents[2]
MULTI_SHEET_XLSX = REPO_ROOT / "test_artifacts/parser/解析测试数据/故障数据样例（规则抽取）.xlsx"


def _json_blocks(markdown: str) -> list[dict]:
    blocks: list[dict] = []
    current: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if line.strip() == "```json":
            inside, current = True, []
            continue
        if inside and line.strip() == "```":
            blocks.append(json.loads("\n".join(current)))
            inside = False
            continue
        if inside:
            current.append(line)
    return blocks


# ------------------------------------------------------------------ 基本结构

def test_table_from_columns_and_rows_builds_records_with_dedup():
    table = table_from_columns_and_rows(
        ["名称", "值", "值", ""],
        [["甲", "1", "2", "3"], ["乙"]],
    )
    assert table["columns"] == ["名称", "值", "值_2", "列4"]
    assert table["rows"] == [
        {"名称": "甲", "值": "1", "值_2": "2", "列4": "3"},
        {"名称": "乙", "值": "", "值_2": "", "列4": ""},
    ]


def test_render_json_block_is_valid_json_code_fence():
    block = render_json_block({"columns": ["a"], "rows": [{"a": "值"}]})
    assert block.startswith("```json\n") and block.endswith("\n```")
    assert json.loads(block[len("```json\n"):-len("\n```")])["rows"] == [{"a": "值"}]


def test_grouped_header_is_flattened_to_single_level():
    assert merge_grouped_header(
        ["相关方角色", "分阶段", "分阶段"],
        ["相关方角色", "提案登记", "决定"],
    ) == ["相关方角色", "分阶段/提案登记", "分阶段/决定"]

    table = table_from_grid([
        ["相关方角色", "分阶段", "分阶段"],
        ["相关方角色", "提案登记", "决定"],
        ["建议方", "查看", "查看、编辑"],
    ])
    assert table["columns"] == ["相关方角色", "分阶段/提案登记", "分阶段/决定"]
    assert table["rows"] == [{"相关方角色": "建议方", "分阶段/提案登记": "查看", "分阶段/决定": "查看、编辑"}]


def test_single_header_row_is_kept_as_is():
    table = table_from_grid([["名称", "数量"], ["甲", "3"]])
    assert table["columns"] == ["名称", "数量"]
    assert table["rows"] == [{"名称": "甲", "数量": "3"}]


# --------------------------------------------------------------- HTML 表格

HTML_GROUPED = (
    "<table><tr><td rowspan=2 colspan=1>相关方角色</td><td rowspan=1 colspan=2>分阶段</td></tr>"
    "<tr><td rowspan=1 colspan=1>提案登记</td><td>决定</td></tr>"
    "<tr><td>建议方</td><td>查看</td><td>查看、编辑</td></tr></table>"
)


def test_grid_from_html_table_expands_rowspan_and_colspan():
    grid = grid_from_html_table(HTML_GROUPED)
    assert grid == [
        ["相关方角色", "分阶段", "分阶段"],
        ["相关方角色", "提案登记", "决定"],
        ["建议方", "查看", "查看、编辑"],
    ]


def test_html_tables_to_json_replaces_only_the_table():
    markdown = f"# 标题\n\n{HTML_GROUPED}\n\n正文段落"
    converted, count = html_tables_to_json(markdown)
    assert count == 1
    assert "<table" not in converted
    assert "# 标题" in converted and "正文段落" in converted
    table = _json_blocks(converted)[0]
    assert table["rows"] == [{"相关方角色": "建议方", "分阶段/提案登记": "查看", "分阶段/决定": "查看、编辑"}]


def test_broken_html_table_is_left_untouched():
    markdown = "<table><tr><td>没有闭合"
    converted, count = html_tables_to_json(markdown)
    assert count == 0 and converted == markdown


# ------------------------------------------------------------ Markdown 表格

def test_markdown_tables_to_json_replaces_pipe_table():
    markdown = "说明\n\n| 名称 | 值 |\n| --- | --- |\n| 甲 | 1 \\| 2 |\n\n结尾"
    converted, count = markdown_tables_to_json(markdown)
    assert count == 1
    assert "| 名称 |" not in converted
    assert "说明" in converted and "结尾" in converted
    table = _json_blocks(converted)[0]
    assert table["rows"] == [{"名称": "甲", "值": "1 | 2"}]


def test_pipe_table_without_separator_is_not_a_table():
    markdown = "| 只是 | 文本 |\n| 继续 | 文本 |"
    converted, count = markdown_tables_to_json(markdown)
    assert count == 0 and converted == markdown


def test_multiple_tables_in_one_document(tmp_path):
    markdown = "| a |\n| --- |\n| 1 |\n\ntext\n\n" + HTML_GROUPED
    converted, count = convert_tables_to_json(markdown)
    assert count == 2
    assert len(_json_blocks(converted)) == 2


# ------------------------------------------------------------------ 解析接线

def test_excel_engine_outputs_json_per_sheet(tmp_path):
    from algorithm.preproc.preproc_src.processors.excel_processor import ExcelProcessor

    path = tmp_path / "sample.csv"
    path.write_text("术语,定义\n甲,说明1\n乙,说明2\n", encoding="utf-8")
    result = ExcelProcessor().process(str(path))

    assert "|" not in result["content"]
    blocks = _json_blocks(result["content"])
    assert blocks == [
        {
            "columns": ["术语", "定义"],
            "rows": [{"术语": "甲", "定义": "说明1"}, {"术语": "乙", "定义": "说明2"}],
        }
    ]
    assert result["content"].startswith("## ")


def test_mineru_path_converts_html_tables(monkeypatch, tmp_path):
    """MinerU 以 HTML 输出表格，落盘前必须转成 JSON 代码块。"""
    captured: dict[str, str] = {}

    class _Writer:
        def write_string(self, name, content):
            captured[name] = content

    processor = ocr_processor.OCRProcessor.__new__(ocr_processor.OCRProcessor)
    processor._pipeline_union_make = lambda *_a, **_k: f"# 标题\n\n{HTML_GROUPED}"
    processor._MakeMode = type("M", (), {"MM_MD": "mm", "CONTENT_LIST": "cl"})
    processor._render_markdown_with_page_edges = lambda **_k: ("不应走到这里", [])

    processor._process_output(
        pdf_info=[],
        pdf_bytes=b"",
        pdf_file_name="x",
        local_md_dir=str(tmp_path),
        local_image_dir=str(tmp_path),
        md_writer=_Writer(),
        f_draw_layout_bbox=False,
        f_draw_span_bbox=False,
        f_dump_orig_pdf=False,
        f_dump_md=True,
        f_dump_content_list=False,
        f_dump_middle_json=False,
        f_dump_model_output=False,
        f_make_md_mode="mm",
        middle_json={},
        is_pipeline=True,
        keep_page_edges="off",
    )
    content = captured["x.md"]
    assert "<table" not in content
    assert _json_blocks(content)[0]["columns"] == ["相关方角色", "分阶段/提案登记", "分阶段/决定"]


def test_docling_docx_path_converts_pipe_tables(monkeypatch):
    class _Document:
        def export_to_markdown(self, **_kwargs):
            return "## 标题\n\n| 名称 | 值 |\n| --- | --- |\n| 甲 | 1 |\n"

    class _Result:
        document = _Document()

    class _Converter:
        def convert(self, _path):
            return _Result()

    processor = docx_processor.DocxProcessor()
    monkeypatch.setattr(processor, "_get_converter", lambda: _Converter())
    content = processor._process_docx("x.docx")
    assert "| 名称 |" not in content
    assert _json_blocks(content)[0]["rows"] == [{"名称": "甲", "值": "1"}]


@pytest.mark.skipif(not MULTI_SHEET_XLSX.is_file(), reason="缺少 xlsx 测试文件")
def test_excel_engine_on_real_workbook():
    from algorithm.preproc.preproc_src.processors.excel_processor import ExcelProcessor

    result = ExcelProcessor().process(str(MULTI_SHEET_XLSX))
    blocks = _json_blocks(result["content"])
    assert blocks, result["content"][:200]
    assert all("columns" in table and "rows" in table for table in blocks)
    assert result["content"].count("## ") == len(blocks)


def test_cjk_punctuation_spacing_is_collapsed_in_cells():
    table = table_from_columns_and_rows(
        ["权限", "备注"],
        [["查看 、 编辑 、 导入", "( 说明 )"]],
    )
    assert table["rows"] == [{"权限": "查看、编辑、导入", "备注": "(说明)"}]
    # 英文空格不受影响
    assert table_from_columns_and_rows(["a"], [["Standard digitalization"]])["rows"] == [
        {"a": "Standard digitalization"}
    ]


def test_trailing_note_row_becomes_text_not_a_record():
    markdown = "| 角色 | 权限 |\n| --- | --- |\n| 甲 | 查看 |\n| 注:“×”表示没有权限。 |  |"
    converted, count = markdown_tables_to_json(markdown)
    assert count == 1
    table = _json_blocks(converted)[0]
    assert table["rows"] == [{"角色": "甲", "权限": "查看"}]
    assert "注:“×”表示没有权限。" in converted
    assert converted.index("```") < converted.index("注:")


def test_colspan_note_row_is_also_recognised():
    html = (
        "<table><tr><td>角色</td><td>权限</td></tr>"
        "<tr><td>甲</td><td>查看</td></tr>"
        "<tr><td colspan=2>注1:专家指评审专家。注2:没有权限时为×。</td></tr></table>"
    )
    converted, count = html_tables_to_json(html)
    assert count == 1
    table = _json_blocks(converted)[0]
    assert table["rows"] == [{"角色": "甲", "权限": "查看"}]
    assert "注1:专家指评审专家。注2:没有权限时为×。" in converted
