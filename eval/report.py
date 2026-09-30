#!/usr/bin/env python3
"""评测批次报告：summary.json 内容 → report.md Markdown。

纯函数模块（stdlib only），不 import eval.run，方便单测与独立渲染历史批次：
  python3 -c "import json; from eval.report import render_report; \
print(render_report(json.load(open('tmp/eval/runs/<ts>/summary.json', encoding='utf-8'))))"
"""
from __future__ import annotations

from typing import Any

MODULE_LABELS = {
    "backend_extract": "后台抽取",
    "backend_system": "沉淀 knowledge-ingestion",
    "plugin_extract": "插件 kg_extract_v2",
}
MODULE_ORDER = ("backend_extract", "backend_system", "plugin_extract")
STAGE_ORDER = ("parse", "chunk", "extract")
SIDE_LABELS = (("entities", "实体"), ("relations", "关系"))

EXIT_CODE_MEANINGS = {
    0: "全部通过",
    2: "有 case 失败（含重试后跳过、空抽取判失败）",
    4: "无失败 case 但门禁未过（实体 F1 低于阈值或耗时超预算）",
}

ERROR_TEXT_LIMIT = 200


def exit_code_for(summary: dict[str, Any]) -> int:
    """与 eval/run.py 主流程一致：2（有失败 case）优先于 4（门禁未过），否则 0。

    配置错误（退出码 1）发生在批次目录建立之前，不会生成报告。
    """
    if int(summary.get("failed") or 0) > 0:
        return 2
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


def _fmt_prf(side: dict[str, Any] | None) -> str:
    item = side or {}
    return f"{_fmt_metric(item.get('precision'))} / {_fmt_metric(item.get('recall'))} / {_fmt_metric(item.get('f1'))}"


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


def _primary_metrics(record: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """逐 case 表用的主指标：后台抽取优先，其次沉淀，再次插件。"""
    for label, block in _case_blocks(record):
        if isinstance(block, dict) and isinstance(block.get("metrics"), dict):
            return label, block["metrics"]
    return None, None


def _lowest_entity_f1(record: dict[str, Any]) -> float | None:
    values: list[float] = []
    for _label, block in _case_blocks(record):
        metrics = block.get("metrics") if isinstance(block, dict) else None
        entities = (metrics or {}).get("entities") if isinstance(metrics, dict) else None
        if isinstance(entities, dict) and entities.get("f1") is not None:
            values.append(float(entities.get("f1")))
    return min(values) if values else None


def _stage_elapsed(record: dict[str, Any], stage: str) -> Any:
    modules = ((record.get("backend") or {}).get("modules")) or {}
    block = modules.get(stage)
    if isinstance(block, dict):
        return block.get("elapsed_seconds")
    return None


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


def _case_note(record: dict[str, Any], metrics_label: str | None, has_metrics: bool) -> str:
    notes: list[str] = []
    if record.get("status") == "failed":
        notes.append(_one_line(record.get("error") or "失败", 120))
    if record.get("skipped_after_retries"):
        notes.append(f"重试 {int(record.get('attempts') or 0)} 次仍失败，已跳过")
    elif int(record.get("attempts") or 1) > 1:
        notes.append(f"第 {int(record.get('attempts') or 1)} 次尝试成功" if record.get("status") == "succeeded" else f"尝试 {int(record.get('attempts') or 1)} 次")
    if record.get("empty_extraction") and record.get("error") != "empty_extraction":
        notes.append("空抽取（0 节点）")
    if not record.get("scored"):
        notes.append(f"未评分：{_one_line(record.get('unscored_reason') or '无 golden', 80)}")
    if has_metrics and metrics_label and metrics_label != "backend_extract":
        notes.append(f"指标来自 {MODULE_LABELS.get(metrics_label, metrics_label)}")
    for _label, block in _case_blocks(record):
        metrics = block.get("metrics") if isinstance(block, dict) else None
        if isinstance(metrics, dict) and metrics.get("match_mode") == "identity_fallback":
            notes.append("judge 失败降级 identity")
            break
    return "；".join(notes) or "-"


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


def _render_gate(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("## 门禁")
    lines.append("")
    if summary.get("interrupted"):
        lines.append("- 退出码：130（用户中断，进度已保存）")
    else:
        code = exit_code_for(summary)
        lines.append(f"- 退出码：{code}（{EXIT_CODE_MEANINGS.get(code, '未知')}）")
    gate = summary.get("gate")
    if not isinstance(gate, dict):
        lines.append("- 门禁：未配置（summary 中无 gate 块）")
        lines.append("")
        return
    passed = gate.get("passed")
    if passed is None:
        lines.append("- 门禁判定：未判定（无评分 case）")
    else:
        lines.append(f"- 门禁判定：{'通过' if passed else '未过'}")

    empty_failures = [str(item) for item in (gate.get("empty_failures") or [])]
    lines.append(f"- 空抽取（empty_extraction）：{len(empty_failures)} 个")
    for label in empty_failures:
        lines.append(f"  - `{label}`")

    min_f1 = gate.get("min_f1") or {}
    threshold = float(min_f1.get("threshold") or 0.0)
    below = list(min_f1.get("below") or [])
    cases = list(summary.get("cases") or [])
    scored_f1 = [value for value in (_lowest_entity_f1(item) for item in cases if item.get("scored")) if value is not None]
    actual = f"{min(scored_f1):.3f}" if scored_f1 else "-"
    if threshold > 0:
        lines.append(f"- 实体 F1 阈值：{threshold:g}（已评分 case 实际最低 {actual}）；低于阈值 {len(below)} 个")
        for item in below:
            lines.append(f"  - `{item.get('case')}` F1={_fmt_metric(item.get('f1'))}")
    else:
        lines.append(f"- 实体 F1 阈值：未设置（已评分 case 实际最低 {actual}）")

    budgets = gate.get("budgets") or {}
    case_budget = budgets.get("case_seconds")
    stage_budgets = budgets.get("stage_seconds") or {}
    budget_parts = []
    if case_budget is not None:
        budget_parts.append(f"case≤{float(case_budget):g}s")
    budget_parts.extend(f"{name}≤{float(value):g}s" for name, value in stage_budgets.items())
    violations = list(gate.get("budget_violations") or [])
    if budget_parts:
        lines.append(f"- 耗时预算：{'，'.join(budget_parts)}；违规 {len(violations)} 项")
    else:
        lines.append(f"- 耗时预算：未设置；违规 {len(violations)} 项")
    for item in violations:
        lines.append(
            f"  - `{item.get('case')}` {item.get('stage')} "
            f"{_fmt_seconds(item.get('elapsed'))} > 预算 {_fmt_seconds(item.get('budget'))}"
        )
    lines.append("")


def _render_overview(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("## 总览")
    lines.append("")
    overall = summary.get("overall") or {}
    included = [
        key
        for key in MODULE_ORDER
        if key == "backend_extract" or _module_has_data(overall.get(key))
    ]
    if not overall:
        lines.append("（无总览数据）")
        lines.append("")
        return
    lines.append("| 模块 | 类别 | golden | predicted | P | R | F1 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for key in included:
        metrics = overall.get(key) or {}
        for side, side_label in SIDE_LABELS:
            block = metrics.get(side) or {}
            lines.append(
                f"| {MODULE_LABELS.get(key, key)} | {side_label} | {_fmt_count(block.get('golden'))} | "
                f"{_fmt_count(block.get('predicted'))} | {_fmt_metric(block.get('precision'))} | "
                f"{_fmt_metric(block.get('recall'))} | {_fmt_metric(block.get('f1'))} |"
            )
    lines.append("")
    identity = summary.get("overall_identity") or {}
    if any(_module_has_data(identity.get(key)) for key in MODULE_ORDER):
        lines.append("**identity 精确匹配对照**")
        lines.append("")
        lines.append("| 模块 | 类别 | golden | predicted | P | R | F1 |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
        for key in included:
            metrics = identity.get(key) or {}
            if not _module_has_data(metrics):
                continue
            for side, side_label in SIDE_LABELS:
                block = metrics.get(side) or {}
                lines.append(
                    f"| {MODULE_LABELS.get(key, key)} | {side_label} | {_fmt_count(block.get('golden'))} | "
                    f"{_fmt_count(block.get('predicted'))} | {_fmt_metric(block.get('precision'))} | "
                    f"{_fmt_metric(block.get('recall'))} | {_fmt_metric(block.get('f1'))} |"
                )
        lines.append("")


def _render_cases(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("## 逐 case 明细")
    lines.append("")
    cases = list(summary.get("cases") or [])
    if not cases:
        lines.append("（无 case）")
        lines.append("")
        return
    lines.append(
        "| case | 状态 | 实体 P/R/F1 | 关系 P/R/F1 | parse | chunk | extract | 总耗时 | 抽取版本 | 备注 |"
    )
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |")
    for record in cases:
        metrics_label, metrics = _primary_metrics(record)
        entities = (metrics or {}).get("entities") if metrics else None
        relations = (metrics or {}).get("relations") if metrics else None
        row = [
            f"`{_case_label(record)}`",
            _status_text(record),
            _fmt_prf(entities) if entities else "-",
            _fmt_prf(relations) if relations else "-",
            _fmt_seconds(_stage_elapsed(record, "parse")),
            _fmt_seconds(_stage_elapsed(record, "chunk")),
            _fmt_seconds(_stage_elapsed(record, "extract")),
            _fmt_seconds(record.get("elapsed_seconds")),
            str(record.get("extract_version") or "-"),
            _case_note(record, metrics_label, metrics is not None),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")


def _render_issues(lines: list[str], summary: dict[str, Any]) -> None:
    lines.append("## 问题清单")
    lines.append("")
    cases = list(summary.get("cases") or [])
    gate = summary.get("gate") or {}

    failed = [item for item in cases if item.get("status") == "failed"]
    lines.append(f"**失败 case（{len(failed)}）**")
    if failed:
        for item in failed:
            suffix = "（重试耗尽已跳过）" if item.get("skipped_after_retries") else ""
            lines.append(f"- `{_case_label(item)}`：{_one_line(item.get('error') or '失败')}{suffix}")
    else:
        lines.append("- 无")
    lines.append("")

    empty_labels = [str(item) for item in (gate.get("empty_failures") or [])]
    if not empty_labels:
        empty_labels = [_case_label(item) for item in cases if item.get("empty_extraction")]
    lines.append(f"**空抽取（{len(empty_labels)}）**")
    if empty_labels:
        for label in empty_labels:
            lines.append(f"- `{label}`")
    else:
        lines.append("- 无")
    lines.append("")

    fallbacks = _judge_fallbacks(cases)
    lines.append(f"**judge 降级 / 错误（{len(fallbacks)}）**")
    if fallbacks:
        for label, error in fallbacks:
            lines.append(f"- `{label}`：{error}")
    else:
        lines.append("- 无")
    lines.append("")

    violations = list(gate.get("budget_violations") or [])
    lines.append(f"**耗时超预算（{len(violations)}）**")
    if violations:
        for item in violations:
            lines.append(
                f"- `{item.get('case')}` {item.get('stage')} "
                f"{_fmt_seconds(item.get('elapsed'))} > 预算 {_fmt_seconds(item.get('budget'))}"
            )
    else:
        lines.append("- 无")
    lines.append("")

    below = list((gate.get("min_f1") or {}).get("below") or [])
    threshold = float((gate.get("min_f1") or {}).get("threshold") or 0.0)
    title = f"**实体 F1 低于阈值 {threshold:g}（{len(below)}）**" if threshold > 0 else f"**实体 F1 低于阈值（{len(below)}）**"
    lines.append(title)
    if below:
        for item in below:
            lines.append(f"- `{item.get('case')}` F1={_fmt_metric(item.get('f1'))}")
    else:
        lines.append("- 无")
    lines.append("")


def _render_slow_cases(lines: list[str], summary: dict[str, Any], slow_top: int) -> None:
    lines.append(f"## 最慢 case Top {slow_top}")
    lines.append("")
    cases = [
        item
        for item in (summary.get("cases") or [])
        if item.get("elapsed_seconds") is not None
    ]
    cases.sort(key=lambda item: float(item.get("elapsed_seconds") or 0), reverse=True)
    top = cases[: max(1, int(slow_top))]
    if not top:
        lines.append("（无 case）")
        lines.append("")
        return
    lines.append("| # | case | 总耗时 | parse | chunk | extract | 状态 |")
    lines.append("| ---: | --- | ---: | ---: | ---: | ---: | --- |")
    for index, record in enumerate(top, 1):
        lines.append(
            f"| {index} | `{_case_label(record)}` | {_fmt_seconds(record.get('elapsed_seconds'))} | "
            f"{_fmt_seconds(_stage_elapsed(record, 'parse'))} | {_fmt_seconds(_stage_elapsed(record, 'chunk'))} | "
            f"{_fmt_seconds(_stage_elapsed(record, 'extract'))} | {_status_text(record)} |"
        )
    lines.append("")


def render_report(summary: dict[str, Any], *, slow_top: int = 5) -> str:
    """summary.json 内容 → Markdown 报告。对缺字段/中断中的 summary 容错。"""
    if not isinstance(summary, dict):
        raise TypeError("summary 必须是 dict")
    lines: list[str] = [f"# Benchmark 评测报告 {summary.get('run_id') or ''}".rstrip(), ""]
    _render_header(lines, summary)
    _render_gate(lines, summary)
    _render_overview(lines, summary)
    _render_cases(lines, summary)
    _render_issues(lines, summary)
    _render_slow_cases(lines, summary, slow_top)
    return "\n".join(lines).rstrip() + "\n"
