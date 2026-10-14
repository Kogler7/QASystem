"""公文结构单元（DocumentUnit）切分与命名：对齐 test_data/eval/official 金标。

金标口径（也是本体 `单元标题` 的属性规则）：
  ①有编号且有题名 → 名称取该题名（去编号前缀）
  ②章（只有标题、标题内无句读）→ 章题名
  ③章下无题名的条目 → 「<章题名>第N条」
  ④无编号自然段 → 「第N段」
  ⑤附件段 → 「附件说明」（无论附件项是否带编号）

另覆盖两处解析产物的现实约束：正文行带 markdown 标题前缀（`# 一、工作目标`）；
正文与「收文处理单 / 领导批示」可能拼在同一 chunk 里。
"""
from algorithm.knowledge_extract.v3.official_structure import (
    _cn_number,
    _golden_document_units,
)

TITLE = "关于开展开源智能体安全风险排查工作的通知"


def _names(text: str, title: str = TITLE):
    return [unit["name"] for unit in _golden_document_units(text, title)]


def _by_name(text: str, title: str = TITLE):
    return {unit["name"]: unit for unit in _golden_document_units(text, title)}


def test_numbered_item_takes_its_own_title():
    text = (
        f"# {TITLE}\n\n"
        "各一级子公司，总部各部门：\n\n"
        "为做好风险防范，现将有关要求通知如下。\n\n"
        "一、全面组织开展排查，梳理本企业使用开源智能体情况。\n\n"
        "二、各企业要严格执行内部办公网络与互联网有效隔离。\n"
    )
    assert _names(text) == ["第1段", "全面组织开展排查", "各企业要严格执行内部办公网络与互联网有效隔离"]


def test_chapter_and_unnamed_items_use_template_name():
    text = (
        f"# {TITLE}\n\n"
        "各一级子公司：\n\n"
        "为落实监管要求，现予印发。\n\n"
        "# 一、工作目标\n\n"
        "1.2025年，完成第一批系统建设和完善工作。\n\n"
        "2.2026年，完成第二批系统建设和完善工作。\n\n"
        "# 二、工作要求\n\n"
        "1.各领域负责部门全面启动本领域升级工作任务。\n"
    )
    units = _by_name(text)
    assert list(units) == [
        "第1段",
        "工作目标",
        "工作目标第一条",
        "工作目标第二条",
        "工作要求",
        "工作要求第一条",
    ]
    assert units["工作目标"]["unit_type"] == "章"
    assert units["工作目标第一条"]["unit_type"] == "条"
    assert units["工作目标第一条"]["unit_order"] == "1"
    assert units["工作目标"]["unit_order"] == "一"


def test_paragraphs_are_numbered_and_typed():
    text = (
        f"# {TITLE}\n\n"
        "各一级子公司：\n\n"
        "第一段正文内容，说明缘由。\n\n"
        "第二段正文内容，提出要求。\n"
    )
    units = _by_name(text)
    assert list(units) == ["第1段", "第2段"]
    assert units["第1段"]["unit_type"] == "段落"
    assert units["第1段"]["unit_order"] == "1"
    assert units["第2段"]["unit_order"] == "2"


def test_attachment_is_always_named_attachment_note():
    numbered = (
        f"# {TITLE}\n\n各一级子公司：\n\n正文说明，提出要求。\n\n"
        "附件：1.风险防范建议\n\n2．风险排查反馈表\n"
    )
    plain = f"# {TITLE}\n\n各一级子公司：\n\n正文说明，提出要求。\n\n附件：风险排查反馈表\n"
    for text in (numbered, plain):
        units = _by_name(text)
        assert units["附件说明"]["unit_type"] == "附件"
        assert units["附件说明"]["unit_order"] == ""


def test_markdown_heading_prefix_is_stripped():
    text = (
        f"# {TITLE}\n\n"
        "各一级子公司：\n\n"
        "为落实要求，现予印发。\n\n"
        "## 一、工作目标\n\n"
        "1.完成系统建设工作。\n"
    )
    units = _by_name(text)
    # `## 一、工作目标` 必须被识别为章，而不是自然段
    assert "工作目标" in units
    assert "第2段" not in units
    assert units["工作目标第一条"]["unit_type"] == "条"


def test_header_and_footer_noise_is_not_a_unit():
    text = (
        f"# {TITLE}\n\n"
        "各一级子公司：\n\n"
        "正文说明，提出要求。\n\n"
        "丨11\n\n"
        "中国中车股份有限公司办公室\n\n"
        "2025年8月13日印发\n\n"
        "联系人及联系电话：某某，13800000000。\n\n"
        "抄送：公司领导，总师级领导。\n"
    )
    assert _names(text) == ["第1段"]


def test_form_section_truncates_the_body():
    text = (
        f"# {TITLE}\n\n"
        "各一级子公司：\n\n"
        "正文说明，提出要求。\n\n"
        "## 收文处理单\n\n"
        "收文号：\n\n"
        "请赵总阅批。\n\n"
        "已阅。\n\n"
        "领导批示：\n\n"
        "已阅。\n"
    )
    assert _names(text) == ["第1段"]


def test_body_mentions_of_form_labels_do_not_truncate_following_paragraphs():
    text = (f"# {TITLE}\n\n"
            "各部门依据领导批示调整任务，按来文单位要求办理。\n\n"
            "后续工作继续推进，按期完成。\n")
    assert _names(text) == ["第1段", "第2段"]


def test_table_form_label_ends_the_body():
    text = (f"# {TITLE}\n\n正文说明，提出要求。\n\n"
            "| 领导批示 | 已阅。 |\n\n请有关部门阅办。\n")
    assert _names(text) == ["第1段"]


def test_large_item_number_preserves_number_instead_of_crashing():
    text = f"# {TITLE}\n\n一、工作目标\n\n100.完成后续工作。\n"
    assert _names(text) == ["工作目标", "工作目标第100条"]
    assert _cn_number(100) == "100"


def test_prompt_audit_version_tracks_updated_official_rules():
    from algorithm.knowledge_extract.v3.prompt_router import PROMPT_VERSIONS, OFFICIAL_DOCUMENT_PROMPT_EXTENSION
    assert PROMPT_VERSIONS["official_document"] == "v11"
    assert OFFICIAL_DOCUMENT_PROMPT_EXTENSION.startswith("OFFICIAL_DOCUMENT_PROMPT_EXTENSION v11")


def test_chinese_number_helper():
    assert [_cn_number(n) for n in (1, 2, 10, 11, 20, 21)] == [
        "一",
        "二",
        "十",
        "十一",
        "二十",
        "二十一",
    ]
