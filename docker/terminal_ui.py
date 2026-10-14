"""Dependency-free terminal layout, including CJK cell widths."""
from __future__ import annotations

import re
import unicodedata

SECTIONS = ("运行", "模型", "服务", "抽取", "裁判", "队列", "其他", "全部")
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


def pair(name: str, value: str, cells: int, *, name_cells: int | None = None) -> str:
    """Fixed name column; values share a right edge, including CJK text."""
    if cells < 4: return fit(name, cells)
    left = min(cells - 3, name_cells if name_cells is not None else int((cells - 2) * .64))
    right = cells - left - 2
    clipped = fit(value, right).rstrip()
    return fit(name, left) + "  " + " " * (right - width(clipped)) + clipped


def item_line(row: dict, cells: int, selected: bool = False) -> str:
    prefix = " › " if selected else "   "
    return prefix + pair(row["display_name"], row["display_value"], cells - width(prefix) - 1) + " "


def section(row: dict) -> str:
    if row['kind'] == 'runtime': return '运行'
    if row['kind'] in {'registry', 'api_registry', 'migration_review'}: return '服务'
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
    if row['kind'] == 'runtime':
        c = row['container']
        return [c['name'], '', '实际状态: ' + row['display_value'], '镜像: ' + c['image'],
                '模型: ' + (c['model'] or '应用服务'), 'GPU: ' + c['gpu'], '宿主端口: ' + c['host_ports'],
                '上下文: ' + (c['context'] or '未声明'), '', '只读展示 Docker 实际配置。',
                '按 r 刷新；这里不修改外部服务。', '参数以实际运行容器为准。']
    if row['kind'] in {'api_registry', 'migration_review'}:
        return [row['display_name'], '', row.get('description', ''), '', '配置计划: ' + row['display_value'], '', 'Enter 编辑单个字段；保存需逐页审核。', '服务登记不启动本地模型；功能只引用服务。']
    if row['kind'] == 'registry': return [row['display_name'], '', row['description'], '', row['display_value'], '', '回车编辑JSON；保存需要逐页审核。', '', 'API登记不会启动本地进程。']
    if row["kind"] == "model":
        model = config.models[row["repo"]]
        running = [c for c in config.live if c['model'] == row['repo'] and c['status'] == 'running']
        return [row["repo"], row.get('description', ''),
                "配置: " + str(model[row['key']]), "默认: " + str(row.get('default', False)),
                "实际状态: " + (f"{len(running)} 个服务运行中" if running else '未发现运行服务' if not config.live_error else '未确认'),
                "计划状态: " + ("启用" if model["enabled"] else "关闭"),
                f"设备: {model['device'] or '未选择'}", f"端口: {model['port']}",
                f"上下文: {model['context']}", f"显存比例: {model['utilization']}", "",
                "空格勾选模型，回车编辑参数。", "保存只生成配置，不启动模型。",
                "校验权重按 v；实际部署按 a。", "启动参数变化需重建对应模型容器。"]
    value = config.values.get(row["key"], "")
    default = row["default"]
    if row["secret"]:
        value = "******（已隐藏）" if value else "(空)"
        default = "******（已隐藏）" if default else "(空)"
    return [row["key"], "", "实际: " + row.get('actual_value', '未确认'),
            "配置: " + (value or "(空)"), "默认: " + (default or "(空)"), "",
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
    running = sum(c['status'] == 'running' for c in config.live)
    output = [full(" QASystem  /  系统配置", "title"),
              full(f" 实际运行服务 {running}    计划启用模型 {count}    {'● 未保存修改' if dirty else '○ 配置未修改'}", "warning" if dirty else "muted")]
    tabs = "  ".join(f"[{s}]" if s == selected else s for s in SECTIONS)
    if width(tabs) > cells - 2: tabs = f"分区: {selected}    ← → 切换 / Tab 下一分区"
    output += [full(" " + tabs, "title"), full(f" 搜索: {query or '全部配置'}   [启动]需重启 / [热更]新请求   r 刷新实际状态", "muted")]
    height = page_size(columns, lines)
    start = min(max(0, index - height + 1), max(0, len(rows) - height))
    details = detail(current, config)
    if columns >= 110:
        left = int((cells - 3) * .59); right = cells - left - 3
        output.append(paint("┌" + "─" * left + "┬" + "─" * right + "┐", "muted"))
        output.append("│" + paint("   " + pair("服务 / 配置项", "实际状态" if selected == '运行' else "配置值", left - 4) + " ", "title") + "│" + paint(fit(" 参数说明", right), "title") + "│")
        detail_lines = []
        for line in details:
            name, separator, value = line.partition(": ")
            if separator and name in {"实际", "配置", "默认", "生效", "实际状态", "计划状态", "GPU", "端口", "宿主端口", "镜像", "模型", "上下文", "显存比例"} and width(line) <= right - 2:
                detail_lines.append(pair(name, value, right - 2, name_cells=width(name)))
            else: detail_lines.extend(wrap(line, right - 2))
        for i in range(height):
            pos = start + i
            label = item_line(rows[pos], left, pos == index) if pos < len(rows) else ""
            style = "selected" if pos == index and rows else "good" if pos < len(rows) and rows[pos]["label"].startswith("[x]") else "normal"
            description = " " + detail_lines[i] if i < len(detail_lines) else ""
            output.append("│" + paint(fit(label, left), style) + "│" + fit(description, right) + "│")
        output.append(paint("└" + "─" * left + "┴" + "─" * right + "┘", "muted"))
    else:
        inner = cells - 2
        output.append(paint("┌" + "─" * inner + "┐", "muted"))
        for i in range(height):
            pos = start + i
            label = item_line(rows[pos], inner, pos == index) if pos < len(rows) else ""
            output.append("│" + paint(fit(label, inner), "selected" if pos == index and rows else "normal") + "│")
        output.append(paint("└" + "─" * inner + "┘", "muted"))
        if current and current['kind'] == 'runtime':
            c = current['container']
            compact = [c['name'], '实际状态: ' + current['display_value'], '模型: ' + (c['model'] or '应用服务'),
                       f"GPU: {c['gpu']}    宿主端口: {c['host_ports']}",
                       '上下文: ' + (c['context'] or '未声明'), '镜像: ' + c['image']]
        else:
            compact = details if current and current["kind"] in {'model', 'api_registry', 'migration_review'} else (
                [details[i] for i in (0, 2, 3, 4, 6, 8) if i < len(details)] if current else details)
        text = [part for item in compact for part in wrap(item, cells - 2)]
        output += [full(" " + (text[i] if i < len(text) else ""), "muted") for i in range(6)]
    output += [full(f" {index + 1 if rows else 0}/{len(rows)} 项    备份: {config.backup.name}", "muted"),
               full(" " + (config.live_error if selected == '运行' and config.live_error else notice), "warning"), full(" ↑↓ 移动 Space 开关 Enter 编辑 n 新模型 d 默认  PgUp/PgDn 翻页", "muted"),
               full(" s 保存  v 校验权重  a 部署  q 退出", "title")]
    return "\n".join(output[:lines])
