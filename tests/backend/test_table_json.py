"""表格转 JSON（preproc_src/table_json.py）与各解析路线接线测试。"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from algorithm.preproc.preproc_src.table_json import (
    convert_tables,
    convert_tables_to_json,
    grid_from_html_table,
    html_tables_to_json,
    is_form_source,
    markdown_tables_to_json,
    merge_grouped_header,
    render_form_text,
    render_json_block,
    table_from_columns_and_rows,
    table_from_grid,
)
from algorithm.preproc.preproc_src.processors import docx_processor, ocr_processor

REPO_ROOT = Path(__file__).resolve().parents[2]
MULTI_SHEET_XLSX = REPO_ROOT / "test_data/parse/解析测试数据/故障数据样例（规则抽取）.xlsx"


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


# ------------------------------------------------------------- 表单类文件

# MinerU 4.x 对「收文处理单」的真实输出：整张表单是一张 HTML 表，没有表头。
FORM_HTML = (
    "<table>"
    "<tr><td>收文号:</td><td colspan=3>信息公司收(2026)209号</td></tr>"
    "<tr><td>文件标题:*</td><td colspan=3>关于开展OpenClaw等开源智能体安全风险排查工作的通知</td></tr>"
    "<tr><td>来文单位:</td><td>中国中车集团有限公司</td><td>来文字号:</td><td>中车集团办科信(2026)18号</td></tr>"
    "<tr><td>密级:</td><td colspan=2>缓急:</td><td>收文日期: 2026-03-27</td></tr>"
    "<tr><td colspan=4>主办部门:</td></tr>"
    "<tr><td>办公室主任拟办意见:</td><td colspan=3>请刘总阅批。白雪峰 2026-03-27 16:36</td></tr>"
    "<tr><td>领导批示:</td><td colspan=3>请公司领导阅，各中心和各事业部阅。</td></tr>"
    "<tr><td rowspan=2></td><td colspan=3>已阅。李鹏 2026-03-30 09:25</td></tr>"
    "<tr><td colspan=3>已阅。请部门员工阅。王元元 2026-03-30 09:26</td></tr>"
    "</table>"
)

FORM_SOURCE = "test_docs/表单/表单.pdf"


def test_is_form_source_matches_only_form_filenames():
    assert is_form_source("表单.pdf")
    assert is_form_source("/data/upload/表单.pdf")
    assert is_form_source(r"C:\upload\表单.docx")
    assert is_form_source("附件2表单.xlsx")
    # 目录名不参与判定：test_docs/表单/ 下的普通文件不是表单文件
    assert not is_form_source("test_docs/表单/工作簿1.pdf")
    assert not is_form_source("通知.pdf")
    assert not is_form_source(None)
    assert not is_form_source("")


def test_form_file_tables_become_reading_order_text():
    converted, stats = convert_tables(FORM_HTML, source_name=FORM_SOURCE)

    assert stats == {"tables": 0, "forms": 1}
    assert "```json" not in converted
    assert '"columns"' not in converted
    # 标签与值保持一一对应
    assert "收文号: 信息公司收(2026)209号" in converted
    assert "来文单位: 中国中车集团有限公司" in converted
    assert "来文字号: 中车集团办科信(2026)18号" in converted
    assert "办公室主任拟办意见: 请刘总阅批。白雪峰 2026-03-27 16:36" in converted
    assert "领导批示: 请公司领导阅，各中心和各事业部阅。" in converted
    # colspan 展开不再把同一个值复制成多列
    assert converted.count("信息公司收(2026)209号") == 1
    # 合并单元格挤在一起的连续标签各自成行，不与后面的字段粘连
    assert "密级:\n\n缓急:\n\n收文日期: 2026-03-27" in converted
    # 没有标签的行（传阅签字）按阅读顺序保留
    assert "已阅。李鹏 2026-03-30 09:25" in converted


def test_form_file_treats_every_table_as_text():
    """表单文件里的表不区分形态，一律按阅读顺序还原。"""
    converted, stats = convert_tables(HTML_GROUPED, source_name=FORM_SOURCE)
    assert stats == {"tables": 0, "forms": 1}
    assert '"columns"' not in converted
    assert "建议方" in converted and "查看、编辑" in converted


def test_form_shaped_table_in_ordinary_file_stays_json():
    """普通文件不做内容形态猜测：即使长得像表单，也仍按数据表出 columns/rows。"""
    converted, stats = convert_tables(FORM_HTML, source_name="通知.pdf")

    assert stats == {"tables": 1, "forms": 0}
    assert _json_blocks(converted)


def test_headerless_signature_table_keeps_its_first_row_as_data():
    html = (
        "<table><tr><td></td><td>李明2026-03-30 09:35</td></tr>"
        "<tr><td>已阅。</td><td>刘云松 2026-03-30 09:36</td></tr>"
        "<tr><td>已阅。</td><td>陈东兰 2026-03-30 09:40</td></tr></table>"
    )
    converted, stats = convert_tables(html, source_name=FORM_SOURCE)

    assert stats == {"tables": 0, "forms": 1}
    # 首行其实是签名数据，必须保留，不能变成列名
    assert "李明2026-03-30 09:35" in converted
    assert "刘云松 2026-03-30 09:36" in converted
    assert '"columns"' not in converted


def test_label_with_colon_split_into_next_cell_is_rejoined():
    """MinerU 4.x 有时把冒号切进下一个单元格：`来文字号` + `:中车`。"""
    html = (
        "<table><tr><td>来文单位:</td><td>中车</td><td>来文字号</td><td>:中车</td></tr>"
        "<tr><td>联系电话:</td><td>1234</td><td>密级:</td><td>测试</td></tr></table>"
    )
    converted, stats = convert_tables(html, source_name=FORM_SOURCE)
    assert stats == {"tables": 0, "forms": 1}
    assert "来文字号:中车" in converted


def test_single_column_table_is_still_a_data_table():
    converted, count = convert_tables_to_json("| a |\n| --- |\n| 1 |")
    assert count == 1
    assert _json_blocks(converted)[0] == {"columns": ["a"], "rows": [{"a": "1"}]}


def test_form_note_row_stays_text():
    html = FORM_HTML[:-len("</table>")] + "<tr><td colspan=4>注:本表由收文部门填写。</td></tr></table>"
    converted, stats = convert_tables(html, source_name=FORM_SOURCE)
    assert stats["forms"] == 1
    assert "注:本表由收文部门填写。" in converted


# ------------------------------------------------------------------ 解析接线

def test_ocr_batch_passes_original_path_into_table_conversion(monkeypatch, tmp_path):
    """回归：_process_batch 里曾引用未定义的 file_path，导致 mineru 路线整体失败。

    批量解析时文件名来自 `path.stem`，而表单类文件判定需要原始文件名（含 `表单`），
    因此必须按索引带上原始路径。
    """
    from algorithm.preproc.preproc_src.processors import ocr_processor

    pdf = tmp_path / "表单.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")

    processor = ocr_processor.OCRProcessor.__new__(ocr_processor.OCRProcessor)
    captured: list[dict] = []
    processor._MakeMode = types.SimpleNamespace(MM_MD="mm", CONTENT_LIST="cl")
    processor._read_fn = lambda _path: b"%PDF-1.4 test"
    processor._convert_pdf_bytes = lambda data, _start, _end: data
    processor._prepare_env = lambda _out, _name, _method: (
        str(tmp_path / "images"), str(tmp_path / "md"),
    )
    processor._FileBasedDataWriter = lambda *_a, **_k: object()
    processor._pipeline_doc_analyze = lambda *_a, **_k: ([[]], [[]], [[]], ["ch"], [True])
    processor._pipeline_result_to_middle_json = lambda *_a, **_k: {"pdf_info": []}
    processor._process_output = lambda **kwargs: captured.append(kwargs)

    processor._process_batch(
        [str(pdf)],
        str(tmp_path / "out"),
        backend="pipeline",
        draw_layout_bbox=False,
        draw_span_bbox=False,
        dump_md=False,
        dump_middle_json=False,
        dump_model_output=False,
        dump_orig_pdf=False,
        dump_content_list=False,
    )

    assert captured and captured[0]["source_name"] == str(pdf)


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
    assert result["metadata"]["sheets"] == [
        {
            "sheet_name": "sample",
            "row_count": 2,
            "column_count": 2,
            "columns": ["术语", "定义"],
        }
    ]
    assert result["metadata"]["table_semantics"]["first_data_row"] == 2


def test_csv_preserves_identifier_lexemes_and_empty_values(tmp_path):
    from algorithm.preproc.preproc_src.processors.excel_processor import ExcelProcessor

    path = tmp_path / "lexical.csv"
    path.write_text(
        "编号,数量,备注\n00123,9007199254740993,\n00456,NA,nan\n",
        encoding="utf-8",
    )

    result = ExcelProcessor().process(str(path))
    blocks = _json_blocks(result["content"])

    assert blocks[0]["rows"] == [
        {"编号": "00123", "数量": "9007199254740993", "备注": ""},
        {"编号": "00456", "数量": "NA", "备注": "nan"},
    ]


def test_legacy_xls_failure_is_explicit_and_does_not_use_openpyxl(monkeypatch, tmp_path):
    from algorithm.preproc.preproc_src.processors import excel_processor

    path = tmp_path / "legacy.xls"
    path.write_bytes(b"not-a-real-workbook")
    processor = excel_processor.ExcelProcessor()
    monkeypatch.setattr(
        excel_processor.pd,
        "ExcelFile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ImportError("Missing xlrd")),
    )
    monkeypatch.setattr(
        processor,
        "_fallback_excel_process",
        lambda *_args, **_kwargs: pytest.fail(".xls must not be passed to openpyxl"),
    )

    result = processor.process(str(path))

    assert result["content"] == ""
    assert "xlrd" in result["metadata"]["error"]
    assert ".xlsx" in result["metadata"]["error"]


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
