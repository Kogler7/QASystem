#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用一次纯 official 评测结果替换混合评测目录中的 official 结果。

用法：
    python merge_official_results.py \
        --source eval_results_official_20260909_211036 \
        --target eval_results_official_standard_20260909_141526
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return data


def _latest_summary(directory: Path) -> Path:
    candidates = sorted(directory.glob("summary_*.json"))
    if not candidates:
        raise FileNotFoundError(f"目录下找不到 summary_*.json: {directory}")
    return candidates[-1]


def _metrics_sum(metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
    counters = {
        "entities": {"tp": 0, "fp": 0, "fn": 0, "golden": 0, "predicted": 0},
        "relations": {"tp": 0, "fp": 0, "fn": 0, "golden": 0, "predicted": 0},
    }
    for metrics in metrics_list:
        for name in ("entities", "relations"):
            item = metrics[name]
            counters[name]["tp"] += int(item["tp"])
            counters[name]["fp"] += int(item["fp"])
            counters[name]["fn"] += int(item["fn"])
            counters[name]["golden"] += int(item["golden"])
            counters[name]["predicted"] += int(item["predicted"])

    summary: dict[str, Any] = {}
    for name in ("entities", "relations"):
        c = counters[name]
        precision = c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) else 0.0
        recall = c["tp"] / (c["tp"] + c["fn"]) if (c["tp"] + c["fn"]) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        summary[name] = {
            "golden": c["golden"],
            "predicted": c["predicted"],
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "tp": c["tp"],
            "fp": c["fp"],
            "fn": c["fn"],
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="替换混合评测目录中的 official 结果")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("eval_results_official_20260909_211036"),
        help="只包含 official 结果的源目录",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("eval_results_official_standard_20260909_141526"),
        help="需要替换 official 结果的混合结果目录",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    target = args.target.resolve()
    if not source.is_dir():
        raise SystemExit(f"源目录不存在: {source}")
    if not target.is_dir():
        raise SystemExit(f"目标目录不存在: {target}")

    source_summary_path = _latest_summary(source)
    target_summary_path = _latest_summary(target)
    source_summary = _load_json(source_summary_path)
    target_summary = _load_json(target_summary_path)

    # 1. 替换 official predictions 文件
    source_official_dir = source / "predictions" / "official"
    target_official_dir = target / "predictions" / "official"
    if not source_official_dir.is_dir():
        raise SystemExit(f"源目录没有 official predictions: {source_official_dir}")
    target_official_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for source_file in sorted(source_official_dir.iterdir()):
        if not source_file.is_file():
            continue
        target_file = target_official_dir / source_file.name
        shutil.copy2(source_file, target_file)
        copied.append(str(target_file))

    # 2. 替换 target summary 中的 official per_case
    source_official_cases = [
        item for item in (source_summary.get("per_case") or [])
        if item.get("group") == "official"
    ]
    if not source_official_cases:
        raise SystemExit(f"源 summary 中没有 official per_case: {source_summary_path}")

    target_cases = [
        item for item in (target_summary.get("per_case") or [])
        if item.get("group") != "official"
    ]
    target_cases.extend(source_official_cases)

    # 3. 分组指标：official 用源 summary，其余分组重新汇总
    datasets_summary: dict[str, Any] = {
        "official": source_summary.get("datasets", {}).get("official")
        or _metrics_sum(source_official_cases),
    }
    for group in sorted({str(item.get("group") or "") for item in target_cases}):
        if not group or group == "official":
            continue
        group_cases = [item for item in target_cases if item.get("group") == group]
        if group_cases:
            datasets_summary[group] = _metrics_sum(group_cases)

    merged_summary = dict(target_summary)
    merged_summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
    merged_summary["datasets"] = datasets_summary
    merged_summary["per_case"] = target_cases
    merged_summary["overall"] = _metrics_sum(target_cases)
    merged_summary["output_dir"] = str(target)
    merged_summary["merge_source_summary"] = str(source_summary_path)
    merged_summary["merge_note"] = "official per_case/predictions replaced from source"

    # 4. 备份原 summary，再写回同一个 summary 文件
    backup_dir = target_summary_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{target_summary_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(target_summary_path, backup_path)
    target_summary_path.write_text(
        json.dumps(merged_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"source summary : {source_summary_path}")
    print(f"target summary : {target_summary_path}")
    print(f"summary backup : {backup_path}")
    print("copied official predictions:")
    for path in copied:
        print(f"  {path}")
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
