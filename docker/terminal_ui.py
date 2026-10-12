"""Dependency-free terminal layout, including CJK cell widths."""
from __future__ import annotations

import re
import unicodedata

SECTIONS = ("模型", "服务", "抽取", "裁判", "队列", "其他", "全部")
RESET = "\x1b[0m"
STYLES = {"title": "\x1b[1;36m", "muted": "\x1b[90m", "selected": "\x1b[1;30;46m",
          "good": "\x1b[32m", "warning": "\x1b[33m", "normal": ""}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean(value: str) -> str:
    # User-entered settings must not execute terminal control sequences.
    return "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(value))


def width(value: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
               for c in ANSI.sub("", value))


def fit(value: str, cells: int) -> str:
    value = clean(value)
    if cells <= 0: return ""
    if width(value) <= cells: return value + " " * (cells - width(value))
    result, used = "", 0
    for char in value:
        n = width(char)
        if used + n > cells - 1: break
        result += char; used += n
    return result + "…" + " " * (cells - used - 1)


def wrap(value: str, cells: int) -> list[str]:
    if cells <= 0: return []
    result, line, used = [], "", 0
    for char in clean(value):
        n = width(char)
        if used + n > cells:
            result.append(line); line, used = "", 0
        line += char; used += n
    if line: result.append(line)
    return result or [""]


def section(row: dict) -> str:
    if row["kind"] == "model" or row["key"].startswith("LLM_"): return "模型"
    key = row["key"]
    if key.startswith("QASYSTEM_JUDGE_"): return "裁判"
    if key.startswith("KG_EXTRACT_"): return "抽取"
    if key.startswith(("EXECUTION_", "MCP_EXECUTION_")): return "队列"
    if key.startswith(("BACKEND_", "AGENT_", "QASYSTEM_", "HOST_", "GPU_", "COMPOSE_", "CLEF_", "NANOJEV_")): return "服务"
    return "其他"


def filter_rows(rows: list[dict], selected: str) -> list[dict]:
    return rows if selected == "全部" else [r for r in rows if section(r) == selected]


def page_size(columns: int, lines: int) -> int:
    return max(1, lines - (12 if columns >= 110 else 17))


def detail(row: dict | None, config) -> list[str]:
    if row is None: return ["没有匹配的配置项", "按 / 清空搜索，或切换分区。"]
    if row["kind"] == "model":
        model = config.models[row["repo"]]
        return [row["repo"], "", "计划状态: " + ("启用" if model["enabled"] else "关闭"),
                f"GPU: {model['gpu'] or '未选择'}    端口: {model['port']}",
                f"上下文: {model['context']}    显存比例: {model['utilization']}", "",
                "空格勾选模型，回车编辑参数。", "保存只生成配置，不启动模型。",
                "校验权重按 v；实际部署按 a。", "启动参数变化需重建对应模型容器。"]
    value = config.values.get(row["key"], "")
    default = row["default"]
    if row["secret"]:
        value = "******（已隐藏）" if value else "(空)"
        default = "******（已隐藏）" if default else "(空)"
    return [row["key"], "", "当前: " + (value or "(空)"), "默认: " + (default or "(空)"), "",
            "生效: " + ("热更新 / 新请求" if row["hot"] else "启动配置 / 需相应服务重启"), "",
            row["description"], "", "Enter 编辑  ·  d 恢复默认", "凭据不会回显。" if row["secret"] else ""]


def render(config, rows: list[dict], index: int, selected: str, query: str, notice: str,
           columns: int, lines: int, *, dirty: bool = False, color: bool = True) -> str:
    cells = max(1, columns - 1)  # Avoid wrapping at the terminal's last column.
    def paint(value, style="normal"):
        return STYLES[style] + value + RESET if color else value
    def full(value, style="normal"): return paint(fit(value, cells), style)
    if columns < 56 or lines < 18:
        return "\n".join([full("QASystem · 配置"), full("终端过小，请扩大至至少 56 列 × 18 行。"), full("q 退出；扩大窗口后按任意键刷新。")])
    current = rows[index] if rows else None
    count = sum(bool(m["enabled"]) for m in config.models.values())
    output = [full(" QASystem  /  系统配置", "title"),
              full(f" {count} 个模型计划启用    {'● 有未保存修改' if dirty else '○ 当前配置'}    打开界面不会启动服务", "warning" if dirty else "muted")]
    tabs = "  ".join(f"[{s}]" if s == selected else s for s in SECTIONS)
    if width(tabs) > cells - 2: tabs = f"分区: {selected}    ← → 切换 / Tab 下一分区"
    output += [full(" " + tabs, "title"), full(f" 搜索: {query or '全部配置'}    / 搜索    ← → 切换分区", "muted")]
    height = page_size(columns, lines)
    start = min(max(0, index - height + 1), max(0, len(rows) - height))
    details = detail(current, config)
    if columns >= 110:
        left = int((cells - 3) * .59); right = cells - left - 3
        output.append(paint("┌" + "─" * left + "┬" + "─" * right + "┐", "muted"))
        output.append("│" + paint(fit(" 配置项 / 当前值", left), "title") + "│" + paint(fit(" 参数说明", right), "title") + "│")
        detail_lines = [part for line in details for part in wrap(line, right - 2)]
        for i in range(height):
            pos = start + i
            label = (" › " if pos == index else "   ") + rows[pos]["label"] if pos < len(rows) else ""
            style = "selected" if pos == index and rows else "good" if pos < len(rows) and rows[pos]["label"].startswith("[x]") else "normal"
            description = " " + detail_lines[i] if i < len(detail_lines) else ""
            output.append("│" + paint(fit(label, left), style) + "│" + fit(description, right) + "│")
        output.append(paint("└" + "─" * left + "┴" + "─" * right + "┘", "muted"))
    else:
        inner = cells - 2
        output.append(paint("┌" + "─" * inner + "┐", "muted"))
        for i in range(height):
            pos = start + i
            label = (" › " if pos == index else "   ") + rows[pos]["label"] if pos < len(rows) else ""
            output.append("│" + paint(fit(label, inner), "selected" if pos == index and rows else "normal") + "│")
        output.append(paint("└" + "─" * inner + "┘", "muted"))
        compact = details if current and current["kind"] == "model" else (
            [details[0], details[2], details[3], details[5], details[7]] if current else details)
        text = [part for item in compact for part in wrap(item, cells - 2)]
        output += [full(" " + (text[i] if i < len(text) else ""), "muted") for i in range(6)]
    output += [full(f" {index + 1 if rows else 0}/{len(rows)} 项    备份: {config.backup.name}", "muted"),
               full(" " + notice, "warning"), full(" ↑↓ 移动  Space 开关  Enter 编辑  d 默认  PgUp/PgDn 翻页", "muted"),
               full(" s 保存  v 校验权重  a 部署  q 退出", "title")]
    return "\n".join(output[:lines])
