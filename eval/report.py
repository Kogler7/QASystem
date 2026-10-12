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
        return "—"


def _fmt_count(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "—"


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "—"
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return "—"
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
    return flat or "—"


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


def _status_text(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "-")
    return {"succeeded": "成功", "failed": "失败"}.get(status, status)


GROUP_LABELS = {"official": "公文数据集", "rule": "规则数据集", "simple": "知识单元数据集", "trouble": "故障数据集"}


def _cell(value: Any) -> str:
    """Keep artifact names and notes inside one Markdown table cell."""
    return _one_line(value).replace("&", "&amp;").replace("|", "&#124;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "&#96;")


def _prf(metrics: dict[str, Any], side: str) -> str:
    block = metrics.get(side) or {}
    if not block or (block.get("golden") == 0 and block.get("predicted") == 0):
        return "— | — | —"
    return " | ".join(_fmt_metric(block.get(key)) for key in ("precision", "recall", "f1"))


def _seconds(value: Any) -> str:
    """Numeric seconds for pasting into the reference report's 耗时(s) column."""
    try:
        return f"{max(0.0, float(value)):.1f}" if value is not None else "—"
    except (TypeError, ValueError):
        return "—"


def _total_seconds(records: list[dict[str, Any]]) -> str:
    if not records or any(record.get("elapsed_seconds") is None for record in records):
        return "—"
    return _seconds(sum(float(record["elapsed_seconds"]) for record in records))


def _table(lines: list[str], header: str, rows: list[str]) -> None:
    lines.extend([f"| {header} |", "| " + " | ".join("---" for _ in header.split("|")) + " |"])
    lines.extend(f"| {row} |" for row in rows)
    lines.append("")


def _coverage_note(stats: dict[str, Any]) -> str:
    coverage = stats.get("coverage") or {}
    return f"有效评分 {int(coverage.get('scored') or 0)}/{int(coverage.get('expected') or 0)}"


def _case_view(summary: dict[str, Any], record: dict[str, Any], module: str) -> tuple[dict[str, Any], str]:
    # Reuse the production scoring contract; fallback/invalid scores cannot look
    # like formal LLM results even in the per-case copy/paste tables.
    stats = build_quality_summary({**summary, "cases": [record]})["modules"].get(module) or {}
    coverage = stats.get("coverage") or {}
    notes = []
    if coverage.get("fallback"):
        notes.append("judge 降级 identity_fallback，不计入正式汇总")
    elif coverage.get("mode_mismatch"):
        notes.append("评分口径不一致，不计入汇总")
    elif coverage.get("invalid"):
        notes.append("评分字段无效，不计入汇总")
    elif not coverage.get("scored"):
        notes.append("未评分")
    if record.get("empty_extraction"):
        notes.append("空抽取")
    if record.get("skipped_after_retries"):
        notes.append("重试耗尽")
    if record.get("error"):
        notes.append(_one_line(record["error"]))
    block = dict(_case_blocks(record)).get(module) or {}
    return (block.get("metrics") or {}) if coverage.get("scored") else {}, _cell("；".join(notes))


def _golden_counts(record: dict[str, Any]) -> dict[str, Any]:
    # Old summaries only carry gold counts in scorer outputs. Use a single
    # module per case so multi-mode/plugin runs never double-count the dataset.
    if isinstance(record.get("golden_counts"), dict):
        return record["golden_counts"]
    for _, block in _case_blocks(record):
        metrics = (block or {}).get("metrics") or {}
        if all((metrics.get(side) or {}).get("golden") is not None for side, _ in SIDE_LABELS):
            return {side: metrics[side]["golden"] for side, _ in SIDE_LABELS}
    return {}


def render_report(summary: dict[str, Any]) -> str:
    """Render the numbered evaluation-report template from saved run evidence."""
    if not isinstance(summary, dict):
        raise TypeError("summary 必须是 dict")
    cases = list(summary.get("cases") or [])
    quality = build_quality_summary(summary) if cases else summary.get("quality") or {}
    modules = [key for key in MODULE_ORDER if key in (quality.get("modules") or {})]
    groups = sorted({str(record.get("group") or "?") for record in cases})
    lines = ["# 知识抽取评测报告（eval 数据集）", "", "## 0. 报告信息", ""]
    versions = summary.get("extract_versions") or []
    _table(lines, "项 | 内容", [
        f"评测日期 | {_cell(str(summary.get('started_at') or '待补充')[:10])}",
        f"后端环境 | {_cell(summary.get('backend_url'))}",
        f"抽取算法版本 | {_cell(', '.join(str(v) for v in versions) or '待补充')}",
        f"运行 ID | {_cell(summary.get('run_id'))}",
        f"开始 / 结束 | {_cell(summary.get('started_at'))} / {_cell(summary.get('finished_at'))}",
        f"总耗时 | {_fmt_seconds(summary.get('elapsed_seconds'))}",
        "结论摘要 | 待补充（结合下方指标与问题分析填写）",
    ])
    if summary.get("interrupted"):
        lines.extend(["⚠ 本批次被 Ctrl+C 中断，以下为中断时已保存的进度。", ""])
    lines.extend(["## 1. 评测目的与范围", "",
                  f"本报告统计本批次已记录的 **{len(groups)} 类数据集、{len(cases)} 个 case**；各 case 可含多个附件。", ""])
    _table(lines, "项 | 说明", [
        f"数据目录 | {_cell(summary.get('workspace'))}",
        f"评测阶段 | {_cell(summary.get('stage'))}",
        f"是否含插件链路 | {'是' if summary.get('stage') in ('plugin', 'all') else '否'}",
        f"是否含全流程沉淀 | {'是' if 'backend_system' in modules else '否'}",
    ])
    lines.extend(["## 2. 评测口径", "", "### 2.1 模式（`--mode`）", ""])
    _table(lines, "模式 | 含义 | 评分模块", [
        "extract | 仅知识抽取（复用切片） | backend_extract",
        "chunk-extract | 切片 + 抽取（解析原文件或复用 Markdown） | backend_extract",
        "pipeline | 全流程知识沉淀 | backend_system",
    ])
    modes = summary.get("modes") or []
    lines.extend([f"本批次模式：{_cell(', '.join(modes))}。", "", "### 2.2 指标计算", "",
                  f"本批次评分口径：**{_cell(summary.get('match_mode') or 'identity')}**。", "",
                  "`identity` 按实体 type + name / 关系五元组精确匹配；`llm` 使用 LLM-as-Judge 语义一对一匹配。两种分数不可直接比较。", ""])
    fallbacks = _judge_fallbacks(cases)
    if fallbacks:
        lines.extend([f"⚠ {len(fallbacks)} 个 case judge 失败降级 identity_fallback；降级分数不计入正式汇总，明细表标注原因。", ""])
    lines.extend(["### 2.3 指标定义", "",
                  "实体与关系分别统计：P = TP/(TP+FP)，R = TP/(TP+FN)，F1 = 2PR/(P+R)，Jaccard = TP/(TP+FP+FN)。", "",
                  "整体与分数据集均按有效同口径评分的 TP/FP/FN 累加（micro），不填补未评分结果。P/R/F1 使用 0–1 小数，保留三位。双方均无实体或关系时显示 `—`；golden 为零但有预测时仍计分。", "",
                  "单 case 耗时(s)取记录的 case 总耗时（含评分及重试）；复用批次沿用 summary 中的复用耗时口径。小计为各 case 耗时之和，有缺失则显示 `—`，不等于批次墙钟耗时。", "",
                  "## 3. 数据集总览", ""])
    overview = []
    for i, group in enumerate(groups, 1):
        records = [r for r in cases if str(r.get('group') or '?') == group]
        counts = [_golden_counts(r) for r in records]
        gold = [str(sum(c[side] for c in counts)) if all(side in c for c in counts) else '待补充' for side, _ in SIDE_LABELS]
        formats = sorted({str(name).rsplit('.', 1)[-1].upper() for r in records for name in r.get('files') or [] if '.' in str(name)})
        overview.append(f"{_cell(group)} | {GROUP_LABELS.get(group, '待补充')} | {len(records)} | {_cell(', '.join(formats))} | 待补充 | {gold[0]} | {gold[1]} | §4.{i}")
    if cases:
        counts = [_golden_counts(r) for r in cases]
        gold = [str(sum(c[side] for c in counts)) if all(side in c for c in counts) else '待补充' for side, _ in SIDE_LABELS]
        overview.append(f"**合计** | — | **{len(cases)}** | — | — | {gold[0]} | {gold[1]} | —")
    _table(lines, "数据集（`--groups`） | 业务含义 | case 数 | 文档格式 | 本体（类 / 关系） | golden 实体 | golden 关系 | 章节", overview)
    lines.extend(["golden 数来自本批次记录，每个 case 只计一次；缺失计数时标为待补充，不从报告模板沿用。", ""])
    for module in modules:
        lines.extend([f"**各数据集结果汇总：{MODULE_LABELS[module]}**", ""])
        stats = quality['modules'][module]
        _table(lines, "数据集 | 实体 P | 实体 R | 实体 F1 | 关系 P | 关系 R | 关系 F1 | 备注", [
            f"{_cell(group)} | {_prf((stats.get('by_group', {}).get(group) or {}).get('micro') or {}, 'entities')} | {_prf((stats.get('by_group', {}).get(group) or {}).get('micro') or {}, 'relations')} | {_coverage_note(stats.get('by_group', {}).get(group) or {})}"
            for group in groups
        ] + [f"**整体（micro）** | {_prf(stats.get('micro') or {}, 'entities')} | {_prf(stats.get('micro') or {}, 'relations')} | {_coverage_note(stats)}"])
    lines.extend(["## 4. 分数据集说明与评测数据", ""])
    if not cases:
        lines.extend(["（无文件）", ""])
    for i, group in enumerate(groups, 1):
        records = [r for r in cases if str(r.get('group') or '?') == group]
        lines.extend([f"### 4.{i} {_cell(group)} —— {GROUP_LABELS.get(group, '数据集')}", "",
                      f"#### 4.{i}.1 数据集说明", "", "业务场景、本体类型与属性说明：待补充。", ""])
        _table(lines, "case | 文档 | golden 实体 | golden 关系", [
            f"{_cell(r.get('case'))} | {_cell('、'.join(r.get('files') or []))} | {_fmt_count(_golden_counts(r).get('entities'))} | {_fmt_count(_golden_counts(r).get('relations'))}" for r in records
        ])
        lines.extend([f"#### 4.{i}.2 评测数据", ""])
        for module in modules:
            stats = quality['modules'][module].get('by_group', {}).get(group) or {}
            views = [(r, *_case_view(summary, r, module)) for r in records]
            for side, label in SIDE_LABELS:
                lines.extend([f"**{MODULE_LABELS[module]} · {label}：单 case 指标**", ""])
                _table(lines, "case | 状态 | P | R | F1 | 耗时(s) | 备注", [
                    f"{_cell(r.get('case'))} | {_cell(_status_text(r))} | {_prf(metrics, side)} | {_seconds(r.get('elapsed_seconds'))} | {note}" for r, metrics, note in views
                ] + [f"**小计（micro）** | 成功 {sum(r.get('status') == 'succeeded' for r in records)}/{len(records)} | {_prf(stats.get('micro') or {}, side)} | {_total_seconds(records)} | {_coverage_note(stats)}"])
            lines.extend([f"补充统计（{MODULE_LABELS[module]}，仅有效评分）：", ""])
            _table(lines, "类别 | golden | predicted | TP | FP | FN | Jaccard", [
                f"{label} | " + " | ".join(_fmt_count((stats.get('micro', {}).get(side) or {}).get(k)) for k in ('golden', 'predicted', 'tp', 'fp', 'fn')) + f" | {_fmt_metric((stats.get('micro', {}).get(side) or {}).get('jaccard'))}"
                for side, label in SIDE_LABELS
            ])
        lines.extend([f"#### 4.{i}.3 问题与分析", "", "待补充（结合失败、空抽取、评分降级及匹配明细分析）。", ""])
    lines.extend(["## 5. 横向对比与结论", "", "### 5.1 结论", "",
                  "待补充（版本间比较需保证数据集、模式与评分口径一致；门禁与基线诊断见 summary.json）。", "",
                  "### 5.2 待改进项与建议", "", "待补充。", ""])
    return "\n".join(lines).rstrip() + "\n"
