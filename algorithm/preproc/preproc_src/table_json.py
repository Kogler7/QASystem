"""把解析结果里的表格统一转换成 JSON 代码块。

约定（与调用方确认过）：每张表输出为一个 ```` ```json ```` 代码块，结构固定为::

    {"columns": ["相关方角色", "提案登记", ...],
     "rows": [{"相关方角色": "建议方", "提案登记": "查看", ...}, ...]}

处理规则：

* 首行作为表头；表头重名按 `分阶段`、`分阶段_2` 去重，空表头按 `列1`、`列2` 命名。
* 合并单元格（HTML `rowspan`/`colspan`，以及 Markdown 里重复的分组表头）按
  "分组/子列" 展开成单层表头，例如 `分阶段/提案登记`。
* 数据行按表头宽度补齐或补齐表头，不丢单元格。
* 只替换表格本身，正文、标题、图片与其它 Markdown 结构保持不变。
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
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


def _render_table_block(rows: Sequence[Sequence[str]]) -> str | None:
    """把表格矩阵渲染成 JSON 代码块，表格末尾的注释按普通文本接在后面。"""
    grid, notes = split_note_rows(normalize_grid(rows))
    table = table_from_grid(grid)
    if not table["columns"] and not table["rows"]:
        return None
    block = render_json_block(table)
    if notes:
        block = block + "\n\n" + "\n".join(notes)
    return block


def html_tables_to_json(markdown: str) -> tuple[str, int]:
    """把 Markdown 里的 HTML 表格替换成 JSON 代码块（MinerU 输出形态）。"""
    count = 0

    def _replace(match: re.Match) -> str:
        nonlocal count
        block = _render_table_block(grid_from_html_table(match.group(0)))
        if block is None:
            return match.group(0)
        count += 1
        return block

    return _HTML_TABLE.sub(_replace, str(markdown or "")), count


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


def markdown_tables_to_json(markdown: str) -> tuple[str, int]:
    """把 Markdown 管道表格替换成 JSON 代码块（Docling/其它输出形态）。"""
    lines = str(markdown or "").split("\n")
    output: list[str] = []
    count = 0
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
            block = _render_table_block(rows)
            if block is not None:
                output.append(block)
                count += 1
            continue
        output.append(line)
        index += 1
    return "\n".join(output), count


def convert_tables_to_json(
    markdown: str,
    *,
    html: bool = True,
    pipe: bool = True,
) -> tuple[str, int]:
    """统一入口：先处理 HTML 表格，再处理 Markdown 管道表格。"""
    text = str(markdown or "")
    total = 0
    if html:
        text, count = html_tables_to_json(text)
        total += count
    if pipe:
        text, count = markdown_tables_to_json(text)
        total += count
    return text, total
