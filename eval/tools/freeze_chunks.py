#!/usr/bin/env python3
"""冻结 benchmark 切片：逐 case 调 Backend 解析 + 切片，产物落回 case 目录。

只调已有 HTTP 接口（`/api/v1/documents/parse`、`/api/v1/documents/chunk`），
不 import backend / algorithm，参数与语义与 `eval/run.py` 的 chunk-extract 一致。

产物（<workspace>/<类型>/<case>/）：

    parse/<stem>.md        单文件 Markdown（解析结果正文）
    parse/<stem>.json      单文件解析响应 envelope（缓存，重跑直接复用，不重复解析）
    chunks/<stem>.json     单文件切片响应 envelope（data.items）
    document.md            多文件拼接后的 Markdown（= run.py chunk 模块的输入）
    chunks.json            拼接后 Markdown 的切片 envelope（= run.py 的 chunks.json）

`document.md` + `chunks.json` 与 `eval/run.py` 运行目录布局一致，因此可以直接
`python3 eval/run.py --mode extract --from-run <workspace> ...` 复用这批切片只跑抽取。

批次记录写在 `tmp/eval/chunk_runs/<run_id>/summary.json`；docs/ / ontology/ /
extract/golden.json 一律不改。`parse/`、`chunks/` 都在 run.py 的保留目录名里，
扫描器不会把它们当成 case。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # pytest / python3 -m 方式
    from eval.run import (  # type: ignore
        KIT_DIR,
        call_with_retries,
        case_names_for_type,
        chunk_document,
        collect_source_files,
        add_runtime_arguments,
        params_from_args,
        discover_cases,
        discover_types,
        elapsed_seconds,
        log,
        parse_document,
        selection_from_targets,
        write_json,
        write_text,
    )
except ImportError:  # 直接 python3 eval/tools/freeze_chunks.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from eval.run import (  # type: ignore
        KIT_DIR,
        call_with_retries,
        case_names_for_type,
        chunk_document,
        collect_source_files,
        add_runtime_arguments,
        params_from_args,
        discover_cases,
        discover_types,
        elapsed_seconds,
        log,
        parse_document,
        selection_from_targets,
        write_json,
        write_text,
    )

RUNS_DIR = KIT_DIR.parent / "tmp" / "eval" / "chunk_runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 benchmark 的 case 全部解析 + 切片，产物落回各 case 目录（parse/ + chunks/ + chunks.json）。",
        epilog="示例: python3 eval/tools/freeze_chunks.py --groups official,standard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="*", metavar="路径", help="评测目录，如 official/official_01 或 test_data/eval；传类型目录则跑该类型全部 case")
    parser.add_argument("--workspace", default=None, help="工作区根目录，默认 test_data/eval")
    add_runtime_arguments(parser)
    parser.add_argument("--groups", default="", help="只处理指定类型，逗号分隔，如 official,standard,rule,simple")
    parser.add_argument("--cases", default="", help="只处理指定 case 目录名，逗号分隔")
    parser.add_argument("--force", action="store_true", help="忽略已有产物，重新解析并重新切片")
    parser.add_argument("--rechunk-only", action="store_true", help="复用已有 parse/<stem>.json，只重跑切片")
    parser.add_argument("--dry-run", action="store_true", help="只打印本次会处理的 case 与产物路径，不调 Backend")
    return parser


def parse_file(
    base_url: str,
    source: Path,
    options: dict[str, Any],
    params: dict[str, Any],
    parse_dir: Path,
    *,
    force: bool,
) -> dict[str, Any]:
    """解析单个源文件；命中 parse/<stem>.json 缓存则直接复用。"""
    envelope_path = parse_dir / f"{source.stem}.json"
    markdown_path = parse_dir / f"{source.stem}.md"
    if envelope_path.is_file() and not force:
        try:
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            markdown = str((envelope.get("data") or {}).get("markdown") or "")
        except json.JSONDecodeError:
            envelope, markdown = {}, ""
        if markdown.strip():
            if not markdown_path.is_file():
                write_text(markdown_path, markdown)
            log(f"    解析复用 {envelope_path.name}（{len(markdown)} 字符）")
            return {"status": "reused", "markdown": markdown, "elapsed_seconds": 0.0, "source": str(envelope_path)}
    started = time.perf_counter()
    try:
        envelope, attempts = call_with_retries(
            lambda: parse_document(base_url, source, options, params),
            label=f"解析 {source.name}",
        )
    except Exception as exc:  # noqa: BLE001 - 单文件失败不影响后续 case
        return {
            "status": "failed",
            "markdown": "",
            "elapsed_seconds": elapsed_seconds(started),
            "error": f"{type(exc).__name__}: {exc}",
        }
    markdown = str((envelope.get("data") or {}).get("markdown") or "")
    if not markdown.strip():
        return {"status": "failed", "markdown": "", "elapsed_seconds": elapsed_seconds(started), "error": "解析结果为空"}
    write_json(envelope_path, envelope)
    write_text(markdown_path, markdown)
    elapsed = elapsed_seconds(started)
    log(f"    解析成功 {source.name} {elapsed:.1f}s {len(markdown)} 字符（尝试 {attempts} 次）")
    return {"status": "parsed", "markdown": markdown, "elapsed_seconds": elapsed, "attempts": attempts}


def chunk_markdown(
    base_url: str,
    markdown: str,
    file_name: str,
    options: dict[str, Any],
    params: dict[str, Any],
    out_path: Path,
    *,
    force: bool,
) -> dict[str, Any]:
    """切片并落盘 envelope；失败即失败，不做整篇兜底（冻结输入必须真实）。"""
    if out_path.is_file() and not force:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        items = list((payload.get("data") or {}).get("items") or [])
        if items:
            log(f"    切片复用 {out_path.name}（{len(items)} 片）")
            return {"status": "reused", "items": len(items), "elapsed_seconds": 0.0, "source": str(out_path)}
    started = time.perf_counter()
    try:
        envelope, attempts = call_with_retries(
            lambda: chunk_document(base_url, markdown, file_name, options, params),
            label=f"切片 {file_name}",
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "items": 0, "elapsed_seconds": elapsed_seconds(started), "error": f"{type(exc).__name__}: {exc}"}
    items = list((envelope.get("data") or {}).get("items") or [])
    if not items:
        return {"status": "failed", "items": 0, "elapsed_seconds": elapsed_seconds(started), "error": "切片结果为空"}
    write_json(out_path, envelope)
    elapsed = elapsed_seconds(started)
    log(f"    切片成功 {out_path.name} {elapsed:.1f}s {len(items)} 片（尝试 {attempts} 次）")
    return {"status": "chunked", "items": len(items), "elapsed_seconds": elapsed, "attempts": attempts}


def process_case(
    base_url: str,
    case_dir: Path,
    options: dict[str, Any],
    params: dict[str, Any],
    *,
    force: bool,
    rechunk_only: bool,
    dry_run: bool,
) -> dict[str, Any]:
    docs_dir = case_dir / "docs"
    sources = collect_source_files(docs_dir)
    parse_dir = case_dir / "parse"
    chunks_dir = case_dir / "chunks"
    joined_md_path = case_dir / "document.md"
    joined_chunks_path = case_dir / "chunks.json"
    record: dict[str, Any] = {
        "case": case_dir.name,
        "docs_dir": str(docs_dir),
        "files": [item.name for item in sources],
        "outputs": {
            "parse_dir": str(parse_dir),
            "chunks_dir": str(chunks_dir),
            "document_md": str(joined_md_path),
            "chunks_json": str(joined_chunks_path),
        },
        "per_file": [],
        "status": "pending",
    }
    if not sources:
        record["status"] = "failed"
        record["error"] = "docs/ 下没有源文件"
        log("    失败: docs/ 下没有源文件")
        return record
    if dry_run:
        record["status"] = "dry-run"
        return record

    started = time.perf_counter()
    markdown_parts: list[str] = []
    failures: list[dict[str, Any]] = []
    for source in sources:
        log(f"  - {source.name}")
        if rechunk_only:
            cached = parse_dir / f"{source.stem}.json"
            if not cached.is_file():
                failures.append({"file": source.name, "error": f"--rechunk-only 缺少解析缓存 {cached}"})
                log(f"    失败: --rechunk-only 缺少解析缓存 {cached.name}")
                continue
            envelope = json.loads(cached.read_text(encoding="utf-8"))
            markdown = str((envelope.get("data") or {}).get("markdown") or "")
            parse_info = {"status": "reused", "markdown": markdown, "elapsed_seconds": 0.0, "source": str(cached)}
        else:
            parse_info = parse_file(base_url, source, options, params, parse_dir, force=force)
        markdown = str(parse_info.get("markdown") or "")
        if parse_info.get("status") == "failed" or not markdown.strip():
            failures.append({"file": source.name, "error": parse_info.get("error") or "解析结果为空"})
            continue
        markdown_parts.append(markdown)
        per_file_chunk = chunk_markdown(
            base_url,
            markdown,
            source.stem,
            options,
            params,
            chunks_dir / f"{source.stem}.json",
            force=force or rechunk_only,
        )
        entry = {
            "file": source.name,
            "markdown_length": len(markdown),
            "parse": {key: value for key, value in parse_info.items() if key != "markdown"},
            "chunk": per_file_chunk,
        }
        if per_file_chunk.get("status") == "failed":
            failures.append({"file": source.name, "error": f"切片失败: {per_file_chunk.get('error')}"})
        record["per_file"].append(entry)

    if not markdown_parts:
        record["status"] = "failed"
        record["error"] = "全部源文件解析失败"
        record["failures"] = failures
        record["elapsed_seconds"] = elapsed_seconds(started)
        log(f"    失败: 全部源文件解析失败（{len(failures)} 个）")
        return record

    joined = "\n\n".join(markdown_parts)
    write_text(joined_md_path, joined)
    joined_chunk = chunk_markdown(
        base_url, joined, case_dir.name, options, params, joined_chunks_path, force=force or rechunk_only
    )
    record["markdown_length"] = len(joined)
    record["chunk"] = joined_chunk
    record["failures"] = failures
    record["elapsed_seconds"] = elapsed_seconds(started)
    if joined_chunk.get("status") == "failed":
        record["status"] = "failed"
        record["error"] = f"拼接切片失败: {joined_chunk.get('error')}"
        log(f"    失败: {record['error']}")
    elif failures:
        record["status"] = "partial"
        log(f"    部分成功: {len(record['per_file'])}/{len(sources)} 个文件，{joined_chunk.get('items')} 片，{record['elapsed_seconds']:.1f}s")
    else:
        record["status"] = "succeeded"
        log(f"    完成: {len(record['per_file'])} 个文件，{joined_chunk.get('items')} 片，{record['elapsed_seconds']:.1f}s")
    return record


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else KIT_DIR.parent / "test_data" / "eval"
    if not workspace.is_dir():
        log(f"工作区不存在: {workspace}")
        return 1
    try:
        params = params_from_args(args)
    except ValueError as exc:
        log(str(exc))
        return 1
    base_url = str(args.backend_url or params.get("backend_url") or "").rstrip("/")
    if not base_url:
        log("缺少 --backend-url")
        return 1
    options = params.get("options") or {}
    chunking = options.get("chunking") or {}

    selection = None
    if args.targets:
        try:
            selection = selection_from_targets(workspace, list(args.targets))
        except (FileNotFoundError, ValueError) as exc:
            log(f"目标路径无效: {exc}")
            return 1
    group_filter = [item.strip() for item in args.groups.split(",") if item.strip()]
    case_filter = [item.strip() for item in args.cases.split(",") if item.strip()]
    groups = discover_types(workspace, group_filter or None)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / run_id
    log(f"工作区: {workspace}")
    log("参数来源: 本轮命令行")
    log(f"Backend: {base_url}")
    log(f"切片选项: strategy={chunking.get('strategy')} size={chunking.get('size')} {chunking.get('size_unit')} "
        f"overlap={chunking.get('chunk_overlap')} breakpoint={chunking.get('breakpoint_method')}")
    log(f"批次记录: {run_dir}")
    if args.dry_run:
        log("dry-run：只扫描，不调用 Backend")

    summary: dict[str, Any] = {
        "run_id": run_id,
        "workspace": str(workspace),
        "params": params,
        "backend_url": base_url,
        "chunking": chunking,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": bool(args.dry_run),
        "force": bool(args.force),
        "rechunk_only": bool(args.rechunk_only),
        "groups": {},
        "totals": {},
    }
    cases_total = 0
    started = time.perf_counter()
    for type_dir in groups:
        names = case_names_for_type(type_dir.name, selection=selection, case_filter=case_filter)
        cases = discover_cases(type_dir, names)
        if selection is not None and type_dir.name not in selection:
            continue
        log(f"类型 {type_dir.name}: {len(cases)} 个 case")
        group_record: dict[str, Any] = {"cases": {}, "case_count": len(cases)}
        for case_dir in cases:
            cases_total += 1
            log(f"[{cases_total}] {type_dir.name}/{case_dir.name}")
            record = process_case(
                base_url,
                case_dir,
                options,
                params,
                force=args.force,
                rechunk_only=args.rechunk_only,
                dry_run=args.dry_run,
            )
            group_record["cases"][case_dir.name] = record
        summary["groups"][type_dir.name] = group_record

    statuses: dict[str, int] = {}
    chunk_total = 0
    for group_record in summary["groups"].values():
        for record in group_record["cases"].values():
            statuses[record["status"]] = statuses.get(record["status"], 0) + 1
            chunk_total += int((record.get("chunk") or {}).get("items") or 0)
    summary["totals"] = {
        "cases": cases_total,
        "statuses": statuses,
        "chunks": chunk_total,
        "elapsed_seconds": elapsed_seconds(started),
    }
    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    if not args.dry_run:
        write_json(run_dir / "summary.json", summary)
    log(f"总计: {cases_total} 个 case，{chunk_total} 片，状态 {statuses}，耗时 {summary['totals']['elapsed_seconds']:.1f}s")
    if not args.dry_run:
        log(f"批次记录: {run_dir / 'summary.json'}")
    failed = statuses.get("failed", 0)
    if failed:
        log(f"有 {failed} 个 case 失败，见 summary.json")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
