from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence

_HTML_TABLE = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_SEPARATOR_ROW = re.compile(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*$")
_ESCAPED_PIPE = re.compile(r"\\\|")

# 单元格文本里不适合出现在 JSON/Markdown 行内的字符
_CELL_WHITESPACE = re.compile(r"[\s\u3000]+")
# 导出器会在中文标点两侧补空格（"查看 、 编辑"），中文标点旁的空格没有语义，收拢掉。
_CJK_PUNCT_SPACING = (
    (re.compile(r"[ \t]*([、。，；：！？])[ \t]*"), r"\1"),
    # 括号内侧不留空格（正文里的中文也用半角括号）
    (re.compile(r"([（(【\[《「『])[ \t]+"), r"\1"),
    (re.compile(r"[ \t]+([）)】\]》」』])"), r"\1"),
)
# 表格末尾的整行注释（"注:"/"注1:"），不应作为数据行。
_NOTE_PREFIX = re.compile(r"^注\s*\d*\s*[:：]")

# ---------------------------------------------------------------- 表格形状判定

#: 表单类文件的文件名特征（生产约定：表单文件一定叫 `表单.pdf`）。当前只用于
#: :func:`is_form_source`，该判定对应的输出分支暂时停用。
_FORM_FILENAME = re.compile(r"表单")

#: 带 rowspan/colspan 的单元格占比达到它就按稀疏表处理（只对 HTML 表可用）。
SPARSE_MERGE_RATIO = 0.6
#: 合并密度只在小表上不可靠（4 个单元格的表很容易到 0.5），低于这个单元格数不采信。
SPARSE_MERGE_MIN_CELLS = 8
#: 首列是 `标签:` 的行占比达到它就按稀疏表处理。
SPARSE_LABEL_RATIO = 0.5
#: 首行的值在数据行首列重复达到这个次数，说明首行也是数据（表头是伪造的）。
SPARSE_HEADER_REPEAT = 2

# 标签正文：不含句读、冒号和数字。排除数字是为了不把时间戳当标签
# （`李明2026-03-30 09:35` 里的 `09:` 曾被误判成 `标签: 值`）。
_LABEL_BODY = r"[^，。；;、:：0-9]"
# 表单字段标签：以冒号结尾的短文本（允许 `必填 *` 后缀），中间不含句读。
_LABEL = re.compile(rf"^{_LABEL_BODY}{{1,18}}[:：]\s*\*?$")
# 单元格开头是否就是 `标签:`（后面还可能跟着值，例如 `收文日期: 2026-03-27`）。
_LABEL_PREFIX = re.compile(rf"^{_LABEL_BODY}{{1,18}}[:：]")
# 短文本且不含冒号/数字；MinerU 有时把冒号切到下一个单元格（`来文字号` + `:中车`）。
_BARE_LABEL = re.compile(r"^[^，。；;、:：\s0-9][^，。；;、:：0-9]{0,18}$")
# 完整日期（含日）或时分：表头里出现它，说明那一行是数据。
# 刻意不匹配 `2026年6月` 这种只有年月的列名。
_DATE_TIME = re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|\d{1,2}\s*[:：]\s*\d{2}")
# 表头单元格里不该出现的句读。
_SENTENCE_PUNCT = re.compile(r"[。！？；，]")
# 比较"表头值是否也是数据"时剥掉的行尾句读（"已阅。" 与 "已阅" 视为同一个值）。
_TRAILING_PUNCT = re.compile(r"[\s。.．]+$")
# 统计 HTML 表格的合并密度：带 rowspan/colspan 的单元格占全部单元格的比例。
_TD_TAG = re.compile(r"<t[dh]\b([^>]*)>", re.IGNORECASE)
_SPAN_ATTR = re.compile(r"(?:rowspan|colspan)\s*=\s*[\"']?\s*([2-9]\d*)", re.IGNORECASE)

# 签名：日期（兼容 `2026-03` 这种简写），人名取日期前紧邻的 2~3 个汉字。
_DATE = re.compile(
    r"\d{4}\s*[-/.]\s*\d{1,2}"
    r"(?:\s*[-/.]\s*\d{1,2})?"
    r"(?:\s*\d{1,2}\s*[:：]\s*\d{1,2})?"
)
_NAME_BEFORE_DATE = re.compile(r"([\u4e00-\u9fa5]{2,3})$")
# 与姓名粘连的签阅状态词（表单里常写成 `已阅小黄`）
_STATUS_WORDS = ("已阅。", "已阅", "己阅。", "己阅", "已阋。", "已阋", "阅。", "阅", "同意")
# Markdown 标题，用于给表单 JSON 取 title。
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*$")


def is_form_source(source_name: str | None) -> bool:
    """来源文件是不是表单类文件（只看文件名，不含目录）。

    约定表单文件一定含 `表单`（如 `表单.pdf`、`附件2表单.docx`）。目录名不参与判定，
    避免把 `test_docs/表单/` 下的普通文件也当成表单。

    注：「表单类文件里的表不转 JSON」的旧行为当前**未启用**，本函数只为恢复保留；
    启用方式见 :func:`_render_table_block`。
    """
    if not source_name:
        return False
    basename = str(source_name).replace("\\", "/").rsplit("/", 1)[-1]
    return bool(_FORM_FILENAME.search(basename))


def _clean_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    text = _ESCAPED_PIPE.sub("|", text)
    text = _CELL_WHITESPACE.sub(" ", text).strip()
    for pattern, replacement in _CJK_PUNCT_SPACING:
        text = pattern.sub(replacement, text)
    return text.strip()


def _dedupe_columns(names: Sequence[str]) -> list[str]:
    """表头去重并为空表头补名。"""
    columns: list[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(names, start=1):
        name = _clean_cell(raw) or f"列{index}"
        if name in seen:
            seen[name] += 1
            candidate = f"{name}_{seen[name]}"
            while candidate in seen:
                seen[name] += 1
                candidate = f"{name}_{seen[name]}"
            seen[candidate] = 1
            name = candidate
        else:
            seen[name] = 1
        columns.append(name)
    return columns


def normalize_grid(rows: Iterable[Sequence[Any]]) -> list[list[str]]:
    """把表格规整为等宽字符串矩阵（短行补空、长行补表头列）。"""
    grid = [[_clean_cell(cell) for cell in row] for row in rows if row is not None]
    grid = [row for row in grid if any(cell for cell in row)] or []
    if not grid:
        return []
    width = max(len(row) for row in grid)
    return [row + [""] * (width - len(row)) for row in grid]


def _has_duplicates(cells: Sequence[str]) -> bool:
    values = [cell for cell in cells if cell]
    return len(values) != len(set(values))


# ------------------------------------------------------------- 表单文本还原

def _condense_row(row: Sequence[str]) -> list[str]:
    """折叠 colspan/rowspan 展开产生的相邻重复值与空单元格，保持原顺序。

    `_HtmlTableParser` 会把合并单元格的值复制进它覆盖的每一列，这里只做还原，
    不改变真正的多列数据。
    """
    cells: list[str] = []
    for cell in row:
        if cell and (not cells or cells[-1] != cell):
            cells.append(cell)
    return cells


def render_form_text(grid: Sequence[Sequence[str]]) -> str:
    """把表单矩阵按阅读顺序还原成文本：`标签: 值` 逐条，其余单元格各占一行。

    只有"标签后面确实是值"时才配对；后续单元格本身以标签开头时（`密级:`、`缓急:`
    被合并单元格或 OCR 挤到一起），各自单独成行，避免粘成一条假记录。
    MinerU 有时把冒号切进下一个单元格（`来文字号` + `:中车`），这里也拼回去。
    """
    lines: list[str] = []
    for row in grid:
        cells = _condense_row(row)
        index = 0
        while index < len(cells):
            cell = cells[index]
            next_cell = cells[index + 1] if index + 1 < len(cells) else ""
            if (
                _LABEL.fullmatch(cell)
                and next_cell
                and not _LABEL_PREFIX.match(next_cell)
            ):
                lines.append(f"{cell} {next_cell}")
                index += 2
            elif _BARE_LABEL.fullmatch(cell) and next_cell.startswith((":", "：")):
                lines.append(f"{cell}{next_cell}")
                index += 2
            else:
                lines.append(cell)
                index += 1
    return "\n\n".join(lines)


# ------------------------------------------------- 稀疏表判定与键值对 JSON

def span_stats(html: str) -> tuple[int, int]:
    """统计 HTML 表格的 ``(单元格总数, 带 rowspan/colspan 的单元格数)``。

    只对 HTML 表格有意义：管道表格没有合并信息，调用方传 ``None``。
    """
    total = merged = 0
    for attrs in _TD_TAG.findall(str(html or "")):
        total += 1
        if _SPAN_ATTR.search(attrs):
            merged += 1
    return total, merged


def _looks_like_header(row: Sequence[str]) -> bool:
    """首行是否像表头：无 `标签:`、无完整日期/时间、无句读。"""
    cells = _condense_row(row)
    if not cells:
        return False
    if any(_LABEL_PREFIX.match(cell) for cell in cells):
        return False
    if any(_DATE_TIME.search(cell) for cell in cells):
        return False
    if any(_SENTENCE_PUNCT.search(cell) for cell in cells):
        return False
    return True


def _label_ratio(grid: Sequence[Sequence[str]]) -> float:
    """首列（折叠合并单元格后第一个非空值）是 `标签:` 的行占比。"""
    rows = [row for row in grid if any(cell for cell in row)]
    if not rows:
        return 0.0
    hits = 0
    for row in rows:
        cells = _condense_row(row)
        if cells and _LABEL.fullmatch(cells[0]):
            hits += 1
    return hits / len(rows)


def _header_row_is_data(grid: Sequence[Sequence[str]]) -> bool:
    """首行首个单元格的值在数据行首列反复出现 —— 首行其实也是数据。

    传阅签字栏被切碎后，MinerU 会把某条 `已阅` 记录当成表头：表头只有一个短值，
    这个值又在下面多行首列重复。真正的表头（`序号`、`相关方角色`）不会这样。
    分组表头的第二行会让首列值重复 1 次，因此门槛取 ``SPARSE_HEADER_REPEAT``。
    """
    if len(grid) < 3:
        return False
    header = _condense_row(grid[0])
    if not header:
        return False
    key = _TRAILING_PUNCT.sub("", header[0])
    if not key or len(key) > 8:
        return False
    first_column = [
        _TRAILING_PUNCT.sub("", _condense_row(row)[0])
        for row in grid[1:]
        if _condense_row(row)
    ]
    return first_column.count(key) >= SPARSE_HEADER_REPEAT


def is_sparse_table(
    grid: Sequence[Sequence[str]],
    *,
    spans: tuple[int, int] | None = None,
) -> bool:
    """这张表是不是"稀疏表"（表单/版式表），而不是行列式数据表。

    三个信号取"或"，任一命中即按稀疏表处理：

    * 合并密度达标（大量 rowspan/colspan；仅 HTML 表可得，且单元格太少时不采信）；
    * 首列过半是 `标签:`（键值表单）；
    * 首行不像表头（含完整日期/时间、句读，或本身是 `标签:`）；
    * 首行的值在数据行首列反复出现（首行本身是数据）。

    **只有表头、没有数据行的表一律按数据表处理**：全空的数据行会被 ``normalize_grid``
    丢掉，剩下的一行就是表头本身。此时"首列是标签"之类的信号只是在描述表头
    （`项目号：/跟踪号：` 这类模板表头会把标签率顶到 1.0），并没有"标签→值"版式可还原；
    判成表单只会得到字段全空的键值对 JSON。空表头 + 空数据的表同样不输出内容。

    刻意**不用"填充率"**：实测真实记录表 `故障数据样例（规则抽取）.xlsx` 的填充率只有
    0.64，而多张表单反而在 0.69~0.94，两者完全重叠——用它会把 17 列×10 行的故障记录表
    误判成表单。合并密度 + 标签率 + 表头可信度这三条在语料上分得开。
    """
    if not grid:
        return False
    rows, cols = len(grid), max(len(row) for row in grid)
    # 列数不足 2 或没有数据行（只剩表头）时不可能是"表单/版式表"
    if cols < 2 or rows < 2:
        return False
    if spans and spans[0] >= SPARSE_MERGE_MIN_CELLS:
        total, merged = spans
        if merged / total >= SPARSE_MERGE_RATIO:
            return True
    if _label_ratio(grid) >= SPARSE_LABEL_RATIO:
        return True
    if _header_row_is_data(grid):
        return True
    return not _looks_like_header(grid[0])


def _extract_signatures(text: str) -> list[dict[str, str]]:
    """从一段文本里抽出"姓名 + 时间"签名，附上它前面的状态/批示原文。"""
    found: list[dict[str, str]] = []
    for match in _DATE.finditer(text):
        before = text[: match.start()].rstrip()
        name = _NAME_BEFORE_DATE.search(before)
        if name is None:
            continue
        signer = name.group(1)
        status = before[: name.start()].strip()
        for word in _STATUS_WORDS:
            if signer.startswith(word):
                status = f"{status}{signer[:len(word)]}".strip()
                signer = signer[len(word):]
                break
        if signer:
            found.append({
                "signer": signer,
                "time": match.group(0).strip(),
                "status": status,
                "raw": text.strip(),
            })
    return found


def _form_entries(grid: Sequence[Sequence[str]]) -> Iterable[tuple[str, str, str]]:
    """把表单矩阵按阅读顺序拆成 ``("pair", 标签, 值)`` / ``("text", 文本, "")``。

    兼容 `标签: 值` 同格、`标签:` 与值分格、以及纯文本行；冒号被切进下一格的
    （`来文字号` + `:中车`）也拼回去。docling 常把"姓名"和"时间"放进相邻单元格，
    这里把紧跟其后的纯日期格并回前一个值，签名才抽得出来。
    """
    for row in grid:
        cells = _condense_row(row)
        index = 0
        while index < len(cells):
            cell = cells[index]
            next_cell = cells[index + 1] if index + 1 < len(cells) else ""
            prefix = _LABEL_PREFIX.match(cell)
            if prefix:
                label = prefix.group(0).rstrip(":：").rstrip("*").strip()
                value = cell[prefix.end():].strip().lstrip("*").strip()
                consumed = 1
                if not value and next_cell and not _LABEL_PREFIX.match(next_cell):
                    value, consumed = next_cell, 2
                after = cells[index + consumed] if index + consumed < len(cells) else ""
                if value and _DATE.fullmatch(after):
                    value, consumed = f"{value} {after}", consumed + 1
                yield ("pair", label, value)
                index += consumed
            elif _BARE_LABEL.fullmatch(cell) and next_cell.startswith((":", "：")):
                yield ("pair", cell, next_cell.lstrip(":：").strip())
                index += 2
            elif next_cell and _DATE.fullmatch(next_cell):
                yield ("text", f"{cell} {next_cell}", "")
                index += 2
            else:
                yield ("text", cell, "")
                index += 1


def _add_field(fields: dict[str, Any], label: str, value: str) -> None:
    """同名字段重复出现时收成数组，不丢内容。"""
    if label not in fields:
        fields[label] = value
    elif isinstance(fields[label], list):
        fields[label].append(value)
    else:
        fields[label] = [fields[label], value]


def form_document_from_grid(
    grid: Sequence[Sequence[str]],
    *,
    title: str = "",
    fallback_title: str = "",
) -> dict[str, Any]:
    """把稀疏表矩阵转成键值对 JSON。

    结构：``{"doc_type": "form", "title": ..., "fields": {...}, "signatures": [...]}``；
    没被识别成字段或签名的文本进 ``notes``。标题优先用传入的 ``title``，其次用表格首行
    横跨整行的短文本（例如 `收文处理单`），都没有才退回 ``fallback_title``。
    """
    rows = [list(row) for row in grid]
    if rows:
        head = _condense_row(rows[0])
        # 横跨整行的短文本当标题；带日期/时间的单元格是数据（传阅签字首行），不能当标题
        if (
            len(head) == 1
            and not title
            and ":" not in head[0]
            and "：" not in head[0]
            and len(head[0]) <= 30
            and not _DATE_TIME.search(head[0])
        ):
            title = head[0]
            rows = rows[1:]

    fields: dict[str, Any] = {}
    signatures: list[dict[str, str]] = []
    notes: list[str] = []
    for kind, first, second in _form_entries(rows):
        if kind == "pair":
            _add_field(fields, first, second)
            signatures.extend(_extract_signatures(second))
        else:
            found = _extract_signatures(first)
            if found:
                signatures.extend(found)
            else:
                notes.append(first)

    document: dict[str, Any] = {
        "doc_type": "form",
        "title": title or fallback_title,
        "fields": fields,
        "signatures": signatures,
    }
    if notes:
        document["notes"] = notes
    return document


def document_title(markdown: str, *, fallback: str = "") -> str:
    """取文档标题，用于表单 JSON 的 ``title``。

    优先 Markdown 标题；否则取第一行"不像字段"的短文本（表单文件常见形态：
    正文第一行就是 `收文处理单`）。都取不到时返回 ``fallback``。
    """
    prose = _HTML_TABLE.sub("\n", str(markdown or ""))
    for raw in prose.splitlines():
        text = raw.strip()
        if not text:
            continue
        heading = _HEADING.match(text)
        candidate = heading.group("text") if heading else text
        if ":" in candidate or "：" in candidate or len(candidate) > 30:
            continue
        return candidate
    return fallback


def _is_grouped_header(first: Sequence[str], second: Sequence[str]) -> bool:
    """判断前两行是否构成"分组表头 + 子表头"。"""
    if not first or not second or len(first) != len(second):
        return False
    if not _has_duplicates(first):
        return False
    if any(not cell for cell in second):
        return False
    return first[0] == second[0] or not first[0]


def merge_grouped_header(first: Sequence[str], second: Sequence[str]) -> list[str]:
    """把两行表头压成单层：`分阶段` + `提案登记` -> `分阶段/提案登记`。"""
    merged: list[str] = []
    for group, sub in zip(first, second):
        group, sub = _clean_cell(group), _clean_cell(sub)
        if not group or group == sub:
            merged.append(sub or group)
        elif not sub:
            merged.append(group)
        else:
            merged.append(f"{group}/{sub}")
    return merged


def split_note_rows(grid: Sequence[Sequence[str]]) -> tuple[list[list[str]], list[str]]:
    """把表格末尾的"注：…"整行拆出来，避免被当成数据记录。

    兼容两种情况：注释独占一行（其余单元格为空），以及注释跨列（合并单元格被填成同一值）。
    """
    rows = [list(row) for row in grid]
    notes: list[str] = []
    while rows:
        filled = [cell for cell in rows[-1] if cell]
        if filled and _NOTE_PREFIX.match(filled[0]) and len(set(filled)) == 1:
            notes.insert(0, filled[0])
            rows.pop()
            continue
        break
    return rows, notes


def table_from_grid(rows: Iterable[Sequence[Any]]) -> dict[str, Any]:
    """由表格矩阵生成 {"columns": [...], "rows": [{...}]}。"""
    grid = normalize_grid(rows)
    if not grid:
        return {"columns": [], "rows": []}

    header, data = grid[0], grid[1:]
    if data and _is_grouped_header(header, data[0]):
        header, data = merge_grouped_header(header, data[0]), data[1:]

    columns = _dedupe_columns(header)
    records = []
    for row in data:
        if len(row) > len(columns):  # 数据比表头宽，补齐列名后仍保留数据
            columns.extend(_dedupe_columns([
                f"列{i}" for i in range(len(columns) + 1, len(row) + 1)
            ])[: len(row) - len(columns)])
        records.append({column: row[index] if index < len(row) else ""
                        for index, column in enumerate(columns)})
    return {"columns": columns, "rows": records}


def table_from_columns_and_rows(columns: Sequence[Any], rows: Iterable[Sequence[Any]]) -> dict[str, Any]:
    """已知列名与数据行时直接构造（Excel/CSV 走这条路）。"""
    names = _dedupe_columns(columns)
    records = []
    for row in rows or []:
        cells = [_clean_cell(cell) for cell in row]
        records.append({name: cells[index] if index < len(cells) else ""
                        for index, name in enumerate(names)})
    return {"columns": names, "rows": records}


def render_json_block(table: dict[str, Any]) -> str:
    """把表对象渲染成 ```json 代码块。"""
    body = json.dumps(table, ensure_ascii=False, indent=2)
    return f"```json\n{body}\n```"


# ------------------------------------------------------------------ HTML 表格

class _HtmlTableParser(HTMLParser):
    """解析 HTML 表格，按 rowspan/colspan 把合并单元格填成规则矩阵。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._grid: list[list[str]] | None = None
        self._occupied: set[tuple[int, int]] = set()
        self._row_index = -1
        self._cell: tuple[int, int] | None = None
        self._buffer: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._grid = []
                self._occupied = set()
                self._row_index = -1
            return
        if self._depth != 1 or self._grid is None:
            return
        if tag == "tr":
            self._row_index += 1
            return
        if tag in {"td", "th"}:
            attributes = {key.casefold(): (value or "") for key, value in attrs}
            try:
                rowspan = max(1, int(attributes.get("rowspan") or 1))
            except ValueError:
                rowspan = 1
            try:
                colspan = max(1, int(attributes.get("colspan") or 1))
            except ValueError:
                colspan = 1
            self._cell = (rowspan, colspan)
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"} and self._cell is not None and self._grid is not None:
            value = _CELL_WHITESPACE.sub(" ", "".join(self._buffer)).strip()
            self._place(value, *self._cell)
            self._cell = None
            self._buffer = []
            return
        if tag == "table":
            if self._depth == 1 and self._grid is not None:
                self.tables.append(self._grid)
                self._grid = None
            self._depth = max(0, self._depth - 1)

    def _place(self, value: str, rowspan: int, colspan: int) -> None:
        assert self._grid is not None
        row = max(self._row_index, 0)
        column = 0
        while (row, column) in self._occupied:
            column += 1
        for target_row in range(row, row + rowspan):
            while len(self._grid) <= target_row:
                self._grid.append([])
            cells = self._grid[target_row]
            for target_column in range(column, column + colspan):
                while len(cells) <= target_column:
                    cells.append("")
                cells[target_column] = value
                self._occupied.add((target_row, target_column))


def grid_from_html_table(html: str) -> list[list[str]]:
    parser = _HtmlTableParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:
        return []
    return normalize_grid(parser.tables[0]) if parser.tables else []


def _render_table_block(
    rows: Sequence[Sequence[str]],
    *,
    form: bool = False,
    title: str = "",
    fallback_title: str = "",
    spans: tuple[int, int] | None = None,
) -> tuple[str | None, str]:
    """渲染一张表，返回 ``(文本, 形态)``；形态为 ``data``（行列式 JSON）或 ``form``（键值对 JSON）。

    稀疏表出键值对 JSON，行列式数据表出 ``columns/rows`` JSON；表格末尾的注释都按普通
    文本接在后面。渲染不出内容时返回 ``(None, "data")``。
    """
    grid, notes = split_note_rows(normalize_grid(rows))
    if not grid:
        return None, "data"
    # ------------------------------------------------------------------
    # 【暂时停用】按文件名判定：表单类文件（文件名含"表单"）里的表不转 JSON，
    # 改按阅读顺序还原为文本。恢复时取消下面这段注释即可（相关函数与常量都还在）。
    # ------------------------------------------------------------------
    # if form:
    #     text = render_form_text(grid)
    #     if not text:
    #         return None, "data"
    #     if notes:
    #         text = text + "\n\n" + "\n".join(notes)
    #     return text, "form"
    if is_sparse_table(grid, spans=spans):
        document = form_document_from_grid(
            grid, title=title, fallback_title=fallback_title
        )
        block = render_json_block(document)
        if notes:
            block = block + "\n\n" + "\n".join(notes)
        return block, "form"
    table = table_from_grid(grid)
    if not table["columns"] and not table["rows"]:
        return None, "data"
    block = render_json_block(table)
    if notes:
        block = block + "\n\n" + "\n".join(notes)
    return block, "data"


def render_grid_block(
    rows: Sequence[Sequence[Any]],
    *,
    title: str = "",
    fallback_title: str = "",
    columns: Sequence[Any] | None = None,
) -> tuple[str | None, str]:
    """渲染一个"已知行列"的表格矩阵（Excel/CSV 等），返回 ``(文本, 形态)``。

    与 HTML/管道表格走同一套形状判定：稀疏表（收文处理单、审批单这类版式）出键值对
    JSON，行列式数据表出 ``columns/rows`` JSON。电子表格没有 `rowspan`/`colspan`
    信息，合并密度传 ``None``。

    ``columns`` 给定时，行列式分支用它当列名（Excel 的 pandas 列名，保持既有输出），
    否则用矩阵首行；形状判定始终基于 ``rows`` 本身。
    """
    grid, notes = split_note_rows(normalize_grid(rows))
    if not grid:
        return None, "data"
    if is_sparse_table(grid):
        document = form_document_from_grid(
            grid, title=title, fallback_title=fallback_title
        )
        block = render_json_block(document)
        kind = "form"
    else:
        if columns is not None:
            table = table_from_columns_and_rows(columns, grid[1:])
        else:
            table = table_from_grid(grid)
        if not table["columns"] and not table["rows"]:
            return None, "data"
        block = render_json_block(table)
        kind = "data"
    if notes:
        block = block + "\n\n" + "\n".join(notes)
    return block, kind


def _replace_html_tables(
    markdown: str,
    *,
    form: bool = False,
    title: str = "",
    fallback_title: str = "",
) -> tuple[str, int, int]:
    """替换 HTML 表格，返回 ``(文本, 行列式 JSON 表数, 键值对 JSON 表数)``。"""
    tables = forms = 0

    def _replace(match: re.Match) -> str:
        nonlocal tables, forms
        html = match.group(0)
        block, kind = _render_table_block(
            grid_from_html_table(html),
            form=form,
            title=title,
            fallback_title=fallback_title,
            spans=span_stats(html),
        )
        if block is None:
            return html
        if kind == "form":
            forms += 1
        else:
            tables += 1
        return block

    return _HTML_TABLE.sub(_replace, str(markdown or "")), tables, forms


def html_tables_to_json(
    markdown: str,
    *,
    source_name: str | None = None,
    title: str = "",
) -> tuple[str, int]:
    """把 Markdown 里的 HTML 表格替换成 JSON 代码块（MinerU 输出形态）。

    返回的计数只统计行列式数据表；稀疏表的键值对 JSON 不计入。
    """
    text, tables, _forms = _replace_html_tables(
        markdown, form=is_form_source(source_name), title=title
    )
    return text, tables


# -------------------------------------------------------------- Markdown 表格

def _split_pipe_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in stripped:
        if character == "|" and not escaped:
            cells.append(_clean_cell("".join(current)))
            current = []
            continue
        current.append(character)
        escaped = character == "\\" and not escaped
        if character != "\\":
            escaped = False
    cells.append(_clean_cell("".join(current)))
    return cells


def _is_pipe_row(line: str) -> bool:
    return line.strip().startswith("|")


def _replace_markdown_tables(
    markdown: str,
    *,
    form: bool = False,
    title: str = "",
    fallback_title: str = "",
) -> tuple[str, int, int]:
    """替换 Markdown 管道表格，返回 ``(文本, 行列式 JSON 表数, 键值对 JSON 表数)``。"""
    lines = str(markdown or "").split("\n")
    output: list[str] = []
    tables = forms = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            _is_pipe_row(line)
            and index + 1 < len(lines)
            and _is_pipe_row(lines[index + 1])
            and _SEPARATOR_ROW.match(lines[index + 1])
        ):
            rows = [_split_pipe_row(line)]
            index += 2  # 跳过表头与分隔行
            while index < len(lines) and _is_pipe_row(lines[index]):
                rows.append(_split_pipe_row(lines[index]))
                index += 1
            # 管道表格没有 rowspan/colspan 信息，合并密度传 None
            block, kind = _render_table_block(
                rows,
                form=form,
                title=title,
                fallback_title=fallback_title,
                spans=None,
            )
            if block is not None:
                output.append(block)
                if kind == "form":
                    forms += 1
                else:
                    tables += 1
            continue
        output.append(line)
        index += 1
    return "\n".join(output), tables, forms


def markdown_tables_to_json(
    markdown: str,
    *,
    source_name: str | None = None,
    title: str = "",
) -> tuple[str, int]:
    """把 Markdown 管道表格替换成 JSON 代码块（Docling/其它输出形态）。

    返回的计数只统计行列式数据表；稀疏表的键值对 JSON 不计入。
    """
    text, tables, _forms = _replace_markdown_tables(
        markdown, form=is_form_source(source_name), title=title
    )
    return text, tables


def convert_tables(
    markdown: str,
    *,
    html: bool = True,
    pipe: bool = True,
    source_name: str | None = None,
) -> tuple[str, dict[str, int]]:
    """统一入口：返回 ``(文本, {"tables": 行列式 JSON 表数, "forms": 键值对 JSON 表数})``。

    每张表按 :func:`is_sparse_table` 单独判定：稀疏表出键值对 JSON，否则出
    ``columns/rows`` JSON。``source_name``（文件名）只用于表单 JSON 的 ``title`` 兜底
    和（当前停用的）表单类文件判定。
    """
    text = str(markdown or "")
    title = document_title(text)
    fallback_title = Path(str(source_name)).stem if source_name else ""
    tables = forms = 0
    if html:
        text, count, form_count = _replace_html_tables(
            text, title=title, fallback_title=fallback_title
        )
        tables += count
        forms += form_count
    if pipe:
        text, count, form_count = _replace_markdown_tables(
            text, title=title, fallback_title=fallback_title
        )
        tables += count
        forms += form_count
    return text, {"tables": tables, "forms": forms}


def convert_tables_to_json(
    markdown: str,
    *,
    html: bool = True,
    pipe: bool = True,
    source_name: str | None = None,
) -> tuple[str, int]:
    """统一入口：先处理 HTML 表格，再处理 Markdown 管道表格。

    返回的计数只统计行列式数据表（与函数名一致）；需要区分键值对 JSON 时用
    :func:`convert_tables`。
    """
    text, stats = convert_tables(markdown, html=html, pipe=pipe, source_name=source_name)
    return text, stats["tables"]
