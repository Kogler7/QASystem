#!/usr/bin/env python3
"""评测批次报告：summary.json 内容 → report.md Markdown。

纯函数模块（stdlib only），不 import eval.run，方便单测与独立渲染历史批次：
  python3 -c "import json; from eval.report import render_report; \
print(render_report(json.load(open('tmp/eval/runs/<ts>/summary.json', encoding='utf-8'))))"
"""
from __future__ import annotations

from typing import Any

try:  # python3 -m eval.report / pytest
    from eval.quality import build_quality_summary
except ImportError:  # python3 eval/report.py
    from quality import build_quality_summary

MODULE_LABELS = {
    "backend_extract": "后台抽取",
    "backend_system": "沉淀 knowledge-ingestion",
    "plugin_extract": "插件 kg_extract_v2",
}
MODULE_ORDER = ("backend_extract", "backend_system", "plugin_extract")
SIDE_LABELS = (("entities", "实体"), ("relations", "关系"))

ERROR_TEXT_LIMIT = 200


def exit_code_for(summary: dict[str, Any]) -> int:
    """与 eval/run.py 主流程一致：2（有失败 case）优先于 4（门禁未过），否则 0。

    配置错误（退出码 1）发生在批次目录建立之前，不会生成报告。
    """
    if int(summary.get("failed") or 0) > 0:
        return 2
    if summary.get("interrupted"):
        return 130
    if (summary.get("gate") or {}).get("passed") is False:
        return 4
    return 0


def _fmt_metric(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_count(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "-"


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "-"
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{rest:04.1f}s"
    hours, rest_m = divmod(int(minutes), 60)
    return f"{hours}h{rest_m}m{rest:.0f}s"


def _one_line(text: Any, limit: int = ERROR_TEXT_LIMIT) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat or "-"


def _case_label(record: dict[str, Any]) -> str:
    return f"{record.get('group')}/{record.get('case')}"


def _case_blocks(record: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    backend = record.get("backend") or {}
    modules = backend.get("modules") or {}
    return [
        ("backend_extract", modules.get("extract")),
        ("backend_system", backend.get("system")),
        ("plugin_extract", record.get("plugin")),
    ]


def _judge_fallbacks(cases: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """judge 失败降级 identity 的 case：(label, judge_error)。"""
    items: list[tuple[str, str]] = []
    for record in cases:
        for _label, block in _case_blocks(record):
            metrics = block.get("metrics") if isinstance(block, dict) else None
            if isinstance(metrics, dict) and metrics.get("match_mode") == "identity_fallback":
                items.append((_case_label(record), _one_line(metrics.get("judge_error"))))
                break
    return items


def _module_has_data(metrics: dict[str, Any] | None) -> bool:
    for side, _label in SIDE_LABELS:
        block = (metrics or {}).get(side) or {}
        if int(block.get("golden") or 0) or int(block.get("predicted") or 0):
            return True
    return False


def _status_text(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "-")
    return {"succeeded": "成功", "failed": "失败"}.get(status, status)


def _render_header(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("## 运行信息")
    lines.append("")
    lines.append(f"- 运行 ID：`{summary.get('run_id') or '-'}`")
    lines.append(f"- 开始：{summary.get('started_at') or '-'}")
    lines.append(f"- 结束：{summary.get('finished_at') or '-'}")
    lines.append(f"- 总耗时：{_fmt_seconds(summary.get('elapsed_seconds'))}")
    lines.append(f"- Backend：{summary.get('backend_url') or '-'}")
    lines.append(f"- 阶段：{summary.get('stage') or '-'}；模式：{', '.join(summary.get('modes') or []) or '-'}")
    match_mode = str(summary.get("match_mode") or "-")
    cases = list(summary.get("cases") or [])
    fallbacks = _judge_fallbacks(cases)
    if fallbacks:
        match_mode += f"（{len(fallbacks)} 个 case judge 失败降级 identity_fallback）"
    lines.append(f"- 评分口径：{match_mode}")
    versions = summary.get("extract_versions") or []
    lines.append(f"- 抽取版本：{', '.join(str(item) for item in versions) if versions else '-'}")
    lines.append(
        f"- case：成功 {int(summary.get('succeeded') or 0)} / 失败 {int(summary.get('failed') or 0)} / "
        f"跳过 {int(summary.get('skipped') or 0)} / 未评分 {int(summary.get('unscored') or 0)}"
    )
    if summary.get("interrupted"):
        lines.append("- ⚠ 本批次被 Ctrl+C 中断，以下为中断时已保存的进度")
    lines.append("")


def _included_modules(summary: dict[str, Any]) -> list[str]:
    overall = summary.get("overall") or {}
    return [
        key
        for key in MODULE_ORDER
        if key == "backend_extract" or _module_has_data(overall.get(key))
    ]


def _append_prf_table(lines: list[str], rows: list[tuple[str, dict[str, Any]]], *, lead: str) -> None:
    """lead 是表头里类别列之前的列名，用「 | 」分隔。"""
    lines.append(f"| {lead} | 类别 | golden | predicted | P | R | F1 |")
    separators = " | ".join(["---"] * (lead.count("|") + 1))
    lines.append(f"| {separators} | --- | ---: | ---: | ---: | ---: | ---: |")
    for prefix, metrics in rows:
        for side, side_label in SIDE_LABELS:
            block = (metrics or {}).get(side) or {}
            lines.append(
                f"| {prefix} | {side_label} | {_fmt_count(block.get('golden'))} | "
                f"{_fmt_count(block.get('predicted'))} | {_fmt_metric(block.get('precision'))} | "
                f"{_fmt_metric(block.get('recall'))} | {_fmt_metric(block.get('f1'))} |"
            )


def _quality_view(summary: dict[str, Any]) -> dict[str, Any]:
    quality = summary.get("quality")
    if isinstance(quality, dict) and quality.get("modules"):
        return quality
    if not summary.get("cases"):
        return {}
    return build_quality_summary(summary)


def _render_overall(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("### Average：LLM-as-Judge")
    lines.append("")
    lines.append("全部有效评分按 TP/FP/FN 累加（micro）。")
    lines.append("")
    overall = summary.get("overall") or {}
    included = _included_modules(summary)
    if not overall:
        lines.append("（无总览数据）")
        lines.append("")
        return
    _append_prf_table(
        lines,
        [(MODULE_LABELS.get(key, key), overall.get(key) or {}) for key in included],
        lead="模块",
    )
    lines.append("")


def _render_by_type(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("### 分类型：LLM-as-Judge")
    lines.append("")
    lines.append("同一类型内的有效评分按 TP/FP/FN 累加。类型名是评测集目录，如 `official`、`rule`、`simple`。")
    lines.append("")
    quality = _quality_view(summary)
    included = set(_included_modules(summary))
    rows: list[tuple[str, dict[str, Any]]] = []
    for module, item in (quality.get("modules") or {}).items():
        if module not in included:
            continue
        label = MODULE_LABELS.get(module, module)
        for group in sorted((item.get("by_group") or {})):
            stats = item["by_group"][group]
            if not int((stats.get("coverage") or {}).get("scored") or 0):
                continue
            rows.append((f"`{group}` | {label}", stats.get("micro") or {}))
    if not rows:
        lines.append("（无分类型数据）")
        lines.append("")
        return
    _append_prf_table(lines, rows, lead="类型 | 模块")
    lines.append("")


def _render_per_file(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("### 单个文件：LLM-as-Judge")
    lines.append("")
    lines.append("每个 case 单独列出实体、关系的 P、R、F1。一个 case 对应评测集里的一份文档（可含多个附件），分数对照该 case 的 golden。")
    lines.append("")
    cases = list(summary.get("cases") or [])
    if not cases:
        lines.append("（无文件）")
        lines.append("")
        return
    lines.append("| 文件 | 模块 | 状态 | 实体 P | 实体 R | 实体 F1 | 关系 P | 关系 R | 关系 F1 |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for record in cases:
        blocks = [
            (label, block["metrics"])
            for label, block in _case_blocks(record)
            if isinstance(block, dict) and isinstance(block.get("metrics"), dict)
        ]
        if not blocks:
            lines.append(
                f"| `{_case_label(record)}` | - | {_status_text(record)} | - | - | - | - | - | - |"
            )
            continue
        for label, metrics in blocks:
            entities = metrics.get("entities") or {}
            relations = metrics.get("relations") or {}
            lines.append(
                f"| `{_case_label(record)}` | {MODULE_LABELS.get(label, label)} | {_status_text(record)} | "
                f"{_fmt_metric(entities.get('precision'))} | {_fmt_metric(entities.get('recall'))} | {_fmt_metric(entities.get('f1'))} | "
                f"{_fmt_metric(relations.get('precision'))} | {_fmt_metric(relations.get('recall'))} | {_fmt_metric(relations.get('f1'))} |"
            )
    lines.append("")


def _render_conclusion(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("## 指标结论")
    lines.append("")
    lines.append("P、R、F1 按 LLM-as-Judge 口径分三层：Average、分类型、单个文件。实体与关系分开计算。")
    lines.append("")
    _render_overall(lines, summary)
    _render_by_type(lines, summary)
    _render_per_file(lines, summary)


def render_report(summary: dict[str, Any]) -> str:
    """summary.json 内容 → Markdown 报告。对缺字段/中断中的 summary 容错。"""
    if not isinstance(summary, dict):
        raise TypeError("summary 必须是 dict")
    lines: list[str] = [f"# Benchmark 评测报告 {summary.get('run_id') or ''}".rstrip(), ""]
    _render_header(lines, summary)
    _render_conclusion(lines, summary)
    return "\n".join(lines).rstrip() + "\n"
