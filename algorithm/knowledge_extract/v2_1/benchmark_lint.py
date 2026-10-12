"""Strict integrity checks for the knowledge extraction V2 benchmark.

The quality evaluator must never repair or silently normalize broken golden
data.  This module provides a preflight gate for schema validity, duplicate
facts, dangling relation endpoints, evidence location, and frozen case hashes.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .graph_utils import canonical_name
from .markdown_quality import lint_heading_hierarchy, lint_markdown
from .ontology_compiler import compile_plan
from .ontology_schema import OntologySchema


ONTOLOGY_SUFFIXES = {".puml", ".uml", ".plantuml"}
TEXT_SUFFIXES = {".md"}
MANIFEST_NAME = "manifest.json"
MARKDOWN_PART_NAME = re.compile(r"^(?P<stem>.+)\.part-(?P<index>\d{3})\.md$")
WHOLE_DOCUMENT_CHAR_LIMIT = 12_000
LONG_DOCUMENT_CHUNK_CHARS = 4_000


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    path: Path
    docs_dir: Path
    ontology_dir: Path
    golden_path: Path


@dataclass(frozen=True)
class LintIssue:
    code: str
    message: str
    case_id: str | None = None
    path: str = ""
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, "")}


@dataclass
class CaseLintReport:
    case_id: str
    sha256: str
    entities: int = 0
    relations: int = 0
    evidence_items: int = 0
    located_evidence_items: int = 0
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        total = self.evidence_items
        evidence_rate = self.located_evidence_items / total if total else 1.0
        return {
            "case_id": self.case_id,
            "sha256": self.sha256,
            "ok": self.ok,
            "entities": self.entities,
            "relations": self.relations,
            "evidence_location_rate": evidence_rate,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class BenchmarkLintReport:
    root: Path
    cases: list[CaseLintReport]
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues and all(case.ok for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "ok": self.ok,
            "case_count": len(self.cases),
            "issues": [issue.to_dict() for issue in self.issues],
            "cases": [case.to_dict() for case in self.cases],
        }


def discover_cases(root: Path) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    simple = root / "simple"
    if simple.is_dir():
        cases.append(
            BenchmarkCase(
                case_id="simple",
                path=simple,
                docs_dir=simple / "docs",
                ontology_dir=simple / "ontology",
                golden_path=simple / "extract" / "golden.json",
            )
        )
    for suite in ("official", "standard"):
        suite_dir = root / suite
        if not suite_dir.is_dir():
            continue
        for case_dir in sorted(path for path in suite_dir.iterdir() if path.is_dir()):
            cases.append(
                BenchmarkCase(
                    case_id=case_dir.name,
                    path=case_dir,
                    docs_dir=case_dir / "docs",
                    ontology_dir=case_dir / "ontology",
                    golden_path=case_dir / "extract" / "golden.json",
                )
            )
    return cases


def case_sha256(case: BenchmarkCase) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in case.path.rglob("*") if path.is_file())
    for path in files:
        digest.update(path.relative_to(case.path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_manifest(root: Path, cases: Iterable[BenchmarkCase] | None = None) -> dict[str, Any]:
    resolved_cases = list(cases) if cases is not None else discover_cases(root)
    return {
        "version": 1,
        "split": "dev",
        "cases": [
            {
                "id": case.case_id,
                "path": case.path.relative_to(root).as_posix(),
                "split": "dev",
                "sha256": case_sha256(case),
            }
            for case in resolved_cases
        ],
    }


def write_manifest(root: Path) -> Path:
    target = root / MANIFEST_NAME
    target.write_text(
        json.dumps(build_manifest(root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _ontology_text(case: BenchmarkCase) -> str:
    files = sorted(
        path
        for path in case.ontology_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in ONTOLOGY_SUFFIXES
    )
    if not files:
        raise ValueError("ontology 目录没有 PlantUML 文件")
    return "\n\n".join(path.read_text(encoding="utf-8-sig") for path in files)


def _document_text(case: BenchmarkCase) -> str:
    if not case.docs_dir.is_dir():
        return ""
    files = sorted(
        path
        for path in case.docs_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in TEXT_SUFFIXES
    )
    return "\n\n".join(path.read_text(encoding="utf-8-sig") for path in files)


def _lint_markdown_documents(case: BenchmarkCase, report: CaseLintReport) -> None:
    if not case.docs_dir.is_dir():
        report.issues.append(
            LintIssue("DOCS_MISSING", "缺少 docs 目录", case.case_id, "docs")
        )
        return
    files = sorted(path for path in case.docs_dir.rglob("*") if path.is_file())
    markdown_files = [path for path in files if path.suffix.casefold() == ".md"]
    for path in files:
        if path.suffix.casefold() == ".md":
            continue
        report.issues.append(
            LintIssue(
                "DOCS_NON_MARKDOWN",
                "benchmark docs 目录只允许 Markdown 文件",
                case.case_id,
                path.relative_to(case.path).as_posix(),
                path.name,
            )
        )
    if not markdown_files:
        report.issues.append(
            LintIssue("MARKDOWN_MISSING", "docs 目录没有 Markdown 文件", case.case_id, "docs")
        )
        return
    split_groups: dict[tuple[Path, str], list[tuple[int, Path]]] = {}
    markdown_texts: dict[Path, str] = {}
    for path in markdown_files:
        relative = path.relative_to(case.path).as_posix()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            report.issues.append(
                LintIssue("MARKDOWN_DECODE_FAILED", str(exc), case.case_id, relative)
            )
            continue
        markdown_texts[path] = text
        if not text.strip():
            report.issues.append(
                LintIssue("MARKDOWN_EMPTY", "Markdown 文件为空", case.case_id, relative)
            )
            continue
        part_match = MARKDOWN_PART_NAME.match(path.name)
        if part_match is None:
            if len(text) > WHOLE_DOCUMENT_CHAR_LIMIT:
                report.issues.append(
                    LintIssue(
                        "MARKDOWN_REQUIRES_SPLIT",
                        (
                            f"单个 Markdown 超过 {WHOLE_DOCUMENT_CHAR_LIMIT} 字符，"
                            f"必须物理拆分为不超过 {LONG_DOCUMENT_CHUNK_CHARS} 字符的 Markdown"
                        ),
                        case.case_id,
                        relative,
                        len(text),
                    )
                )
        else:
            index = int(part_match.group("index"))
            key = (path.parent, part_match.group("stem"))
            split_groups.setdefault(key, []).append((index, path))
            if len(text) > LONG_DOCUMENT_CHUNK_CHARS:
                report.issues.append(
                    LintIssue(
                        "MARKDOWN_CHUNK_TOO_LARGE",
                        f"Markdown 分片不得超过 {LONG_DOCUMENT_CHUNK_CHARS} 字符",
                        case.case_id,
                        relative,
                        len(text),
                    )
                )
        for issue in lint_markdown(text, document_path=path):
            location = f"{relative}:{issue.line}" if issue.line is not None else relative
            report.issues.append(
                LintIssue(
                    "MARKDOWN_" + issue.code,
                    issue.message,
                    case.case_id,
                    location,
                    issue.value,
                )
            )
    for (directory, stem), indexed_paths in sorted(
        split_groups.items(),
        key=lambda item: (item[0][0].as_posix(), item[0][1]),
    ):
        indexes = sorted(index for index, _ in indexed_paths)
        expected = list(range(1, len(indexes) + 1))
        if len(indexes) < 2 or indexes != expected:
            report.issues.append(
                LintIssue(
                    "MARKDOWN_CHUNK_SEQUENCE_INVALID",
                    "Markdown 分片必须从 part-001 开始连续编号，且至少包含两个文件",
                    case.case_id,
                    directory.relative_to(case.path).as_posix(),
                    indexes,
                )
            )
        source = directory / f"{stem}.md"
        if source.is_file():
            report.issues.append(
                LintIssue(
                    "MARKDOWN_SPLIT_SOURCE_RETAINED",
                    "已生成 Markdown 分片时不得保留被替代的超长源 Markdown",
                    case.case_id,
                    source.relative_to(case.path).as_posix(),
                )
            )
    logical_documents: list[tuple[str, list[str]]] = []
    split_paths = {
        path
        for indexed_paths in split_groups.values()
        for _, path in indexed_paths
    }
    for path in markdown_files:
        if path in split_paths or path not in markdown_texts:
            continue
        logical_documents.append(
            (
                path.relative_to(case.path).as_posix(),
                [markdown_texts[path]],
            )
        )
    for (directory, stem), indexed_paths in split_groups.items():
        ordered = [path for _, path in sorted(indexed_paths)]
        if not all(path in markdown_texts for path in ordered):
            continue
        logical_documents.append(
            (
                f"{directory.relative_to(case.path).as_posix()}/{stem}.part-*.md",
                [markdown_texts[path] for path in ordered],
            )
        )
    for logical_path, texts in logical_documents:
        for issue in lint_heading_hierarchy(
            texts,
            require_h1=True,
            single_h1=True,
        ):
            report.issues.append(
                LintIssue(
                    "MARKDOWN_" + issue.code,
                    issue.message,
                    case.case_id,
                    logical_path,
                    issue.value,
                )
            )


def _canonical_quote(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _evidence_quotes(item: dict[str, Any]) -> list[str]:
    evidence_items = _evidence_records(item)
    quotes: list[str] = []
    for evidence in evidence_items:
        quote = evidence.get("quote") or evidence.get("text")
        if isinstance(quote, str) and quote.strip():
            quotes.append(quote)
    return quotes


def _evidence_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("evidence")
    evidence_items = raw if isinstance(raw, list) else [raw]
    return [evidence for evidence in evidence_items if isinstance(evidence, dict)]


def _validate_evidence_locator(
    case: BenchmarkCase,
    evidence: dict[str, Any],
    quote: str,
) -> tuple[bool, LintIssue | None]:
    document_name = evidence.get("document")
    if not isinstance(document_name, str) or not document_name.strip():
        return False, None
    relative = Path(document_name)
    if relative.is_absolute() or ".." in relative.parts:
        return False, LintIssue(
            "EVIDENCE_DOCUMENT_INVALID",
            "evidence.document 必须是 docs 目录内的相对路径",
            case.case_id,
            value=document_name,
        )
    document_path = case.docs_dir / relative
    if not document_path.is_file() or document_path.suffix.casefold() not in TEXT_SUFFIXES:
        return False, LintIssue(
            "EVIDENCE_DOCUMENT_MISSING",
            "evidence.document 不存在或不是 Markdown",
            case.case_id,
            value=document_name,
        )
    text = document_path.read_text(encoding="utf-8-sig")
    if _canonical_quote(quote) not in _canonical_quote(text):
        return False, LintIssue(
            "EVIDENCE_NOT_FOUND",
            "evidence quote 无法在指定 Markdown 原文中定位",
            case.case_id,
            value={"document": document_name, "quote": quote},
        )

    start_char = evidence.get("start_char")
    end_char = evidence.get("end_char")
    if start_char is not None or end_char is not None:
        if (
            not isinstance(start_char, int)
            or isinstance(start_char, bool)
            or not isinstance(end_char, int)
            or isinstance(end_char, bool)
            or start_char < 0
            or end_char <= start_char
            or end_char > len(text)
            or text[start_char:end_char] != quote
        ):
            return False, LintIssue(
                "EVIDENCE_CHAR_RANGE_INVALID",
                "evidence 字符区间不能精确恢复 quote",
                case.case_id,
                value={"document": document_name, "start_char": start_char, "end_char": end_char},
            )

    line_start = evidence.get("line_start")
    line_end = evidence.get("line_end")
    if line_start is not None or line_end is not None:
        lines = text.splitlines(keepends=True)
        if (
            not isinstance(line_start, int)
            or isinstance(line_start, bool)
            or not isinstance(line_end, int)
            or isinstance(line_end, bool)
            or line_start < 1
            or line_end < line_start
            or line_end > len(lines)
            or _canonical_quote(quote)
            not in _canonical_quote("".join(lines[line_start - 1 : line_end]))
        ):
            return False, LintIssue(
                "EVIDENCE_LINE_RANGE_INVALID",
                "evidence 行号范围不能恢复 quote",
                case.case_id,
                value={"document": document_name, "line_start": line_start, "line_end": line_end},
            )
    return True, None


def _identity_key(entity: dict[str, Any]) -> tuple[str, str]:
    return canonical_name(entity.get("type")), canonical_name(entity.get("name"))


def _relation_key(relation: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        canonical_name(relation.get("from_type")),
        canonical_name(relation.get("from_name")),
        canonical_name(relation.get("rel_type")),
        canonical_name(relation.get("to_type")),
        canonical_name(relation.get("to_name")),
    )


def _duplicate_indexes(items: list[dict[str, Any]], key_fn: Any) -> list[tuple[int, int, Any]]:
    first_seen: dict[Any, int] = {}
    duplicates: list[tuple[int, int, Any]] = []
    for index, item in enumerate(items):
        key = key_fn(item)
        if key in first_seen:
            duplicates.append((first_seen[key], index, key))
        else:
            first_seen[key] = index
    return duplicates


def lint_case(case: BenchmarkCase, *, require_evidence: bool = True) -> CaseLintReport:
    report = CaseLintReport(case_id=case.case_id, sha256=case_sha256(case))
    if not case.golden_path.is_file():
        report.issues.append(
            LintIssue("GOLDEN_MISSING", "缺少 extract/golden.json", case.case_id, "extract/golden.json")
        )
        return report
    try:
        ontology = _ontology_text(case)
        golden = _load_json(case.golden_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report.issues.append(LintIssue("CASE_LOAD_FAILED", str(exc), case.case_id))
        return report
    if not isinstance(golden, dict):
        report.issues.append(LintIssue("GOLDEN_INVALID", "golden 必须是 JSON 对象", case.case_id))
        return report

    _lint_markdown_documents(case, report)

    plan = compile_plan(ontology, mode="strict")
    for warning in plan.schema_warnings:
        if warning.severity == "error":
            report.issues.append(
                LintIssue(
                    "ONTOLOGY_" + warning.code,
                    warning.message,
                    case.case_id,
                    warning.path,
                    warning.value,
                )
            )

    schema = OntologySchema.parse(ontology)
    normalized = schema.normalize_extraction(golden)
    sanitized, validation_warnings = schema.validate_extraction(normalized, mode="strict")
    for warning in validation_warnings:
        report.issues.append(
            LintIssue(
                warning.get("code", "SCHEMA_INVALID"),
                warning.get("message", "schema validation failed"),
                case.case_id,
                warning.get("path", ""),
                warning.get("value"),
            )
        )

    entities = [item for item in normalized.get("entities", []) if isinstance(item, dict)]
    relations = [item for item in normalized.get("relations", []) if isinstance(item, dict)]
    report.entities = len(entities)
    report.relations = len(relations)
    for first, duplicate, key in _duplicate_indexes(entities, _identity_key):
        report.issues.append(
            LintIssue(
                "DUPLICATE_ENTITY",
                f"实体 identity 与 entities[{first}] 重复",
                case.case_id,
                f"entities[{duplicate}]",
                key,
            )
        )
    for first, duplicate, key in _duplicate_indexes(relations, _relation_key):
        report.issues.append(
            LintIssue(
                "DUPLICATE_RELATION",
                f"关系与 relations[{first}] 重复",
                case.case_id,
                f"relations[{duplicate}]",
                key,
            )
        )

    accepted_entities = {_identity_key(entity) for entity in sanitized.get("entities", [])}
    for index, relation in enumerate(sanitized.get("relations", [])):
        source = (canonical_name(relation.get("from_type")), canonical_name(relation.get("from_name")))
        target = (canonical_name(relation.get("to_type")), canonical_name(relation.get("to_name")))
        if source not in accepted_entities or target not in accepted_entities:
            report.issues.append(
                LintIssue(
                    "DANGLING_RELATION",
                    "关系端点不在 golden 实体集合",
                    case.case_id,
                    f"relations[{index}]",
                    {"source": source, "target": target},
                )
            )

    if require_evidence:
        document = _canonical_quote(_document_text(case))
        facts = [("entities", entities), ("relations", relations)]
        for group_name, items in facts:
            for index, item in enumerate(items):
                report.evidence_items += 1
                evidence_records = _evidence_records(item)
                quotes = _evidence_quotes(item)
                if not quotes:
                    report.issues.append(
                        LintIssue(
                            "EVIDENCE_MISSING",
                            "事实缺少 evidence quote",
                            case.case_id,
                            f"{group_name}[{index}].evidence",
                        )
                    )
                    continue
                located = False
                locator_issues: list[LintIssue] = []
                for evidence in evidence_records:
                    quote = evidence.get("quote") or evidence.get("text")
                    if not isinstance(quote, str) or not quote.strip():
                        continue
                    locator_valid, locator_issue = _validate_evidence_locator(case, evidence, quote)
                    if locator_issue is not None:
                        locator_issues.append(locator_issue)
                        continue
                    if locator_valid or (document and _canonical_quote(quote) in document):
                        located = True
                        break
                if located:
                    report.located_evidence_items += 1
                else:
                    if locator_issues:
                        for issue in locator_issues:
                            report.issues.append(
                                LintIssue(
                                    issue.code,
                                    issue.message,
                                    issue.case_id,
                                    f"{group_name}[{index}].evidence",
                                    issue.value,
                                )
                            )
                    else:
                        report.issues.append(
                            LintIssue(
                                "EVIDENCE_NOT_FOUND",
                                "evidence quote 无法在 Markdown 原文中定位",
                                case.case_id,
                                f"{group_name}[{index}].evidence",
                                quotes,
                            )
                        )
    return report


def lint_benchmark(
    root: Path,
    *,
    require_evidence: bool = True,
    require_manifest: bool = True,
) -> BenchmarkLintReport:
    root = root.resolve()
    cases = discover_cases(root)
    report = BenchmarkLintReport(root=root, cases=[])
    if not cases:
        report.issues.append(LintIssue("NO_CASES", "没有发现 benchmark cases"))
        return report

    manifest_by_id: dict[str, dict[str, Any]] = {}
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            manifest = _load_json(manifest_path)
            if not isinstance(manifest, dict) or not isinstance(manifest.get("cases"), list):
                raise ValueError("manifest.cases 必须是数组")
            for entry in manifest["cases"]:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    manifest_by_id[entry["id"]] = entry
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            report.issues.append(LintIssue("MANIFEST_INVALID", str(exc), path=MANIFEST_NAME))
    elif require_manifest:
        report.issues.append(LintIssue("MANIFEST_MISSING", "缺少 benchmark/manifest.json", path=MANIFEST_NAME))

    for case in cases:
        case_report = lint_case(case, require_evidence=require_evidence)
        report.cases.append(case_report)
        if manifest_by_id:
            entry = manifest_by_id.get(case.case_id)
            if entry is None:
                case_report.issues.append(
                    LintIssue("MANIFEST_CASE_MISSING", "manifest 缺少 case", case.case_id)
                )
            else:
                if entry.get("split") != "dev":
                    case_report.issues.append(
                        LintIssue("INVALID_SPLIT", "现有 benchmark 必须标记为 dev", case.case_id, value=entry.get("split"))
                    )
                if entry.get("sha256") != case_report.sha256:
                    case_report.issues.append(
                        LintIssue(
                            "HASH_MISMATCH",
                            "case 内容与 manifest sha256 不一致",
                            case.case_id,
                            value={"expected": entry.get("sha256"), "actual": case_report.sha256},
                        )
                    )
    extra_manifest_cases = sorted(set(manifest_by_id) - {case.case_id for case in cases})
    for case_id in extra_manifest_cases:
        report.issues.append(LintIssue("MANIFEST_CASE_UNKNOWN", "manifest 引用了不存在的 case", case_id))
    return report


def _default_root() -> Path:
    return Path(__file__).resolve().parent / "benchmark"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint knowledge_extract_v2_1 benchmark")
    parser.add_argument("--root", type=Path, default=_default_root())
    parser.add_argument("--no-evidence", action="store_true", help="仅用于迁移诊断；发布门禁不得关闭 evidence")
    parser.add_argument("--write-manifest", action="store_true", help="按当前内容重建 dev manifest/hash")
    parser.add_argument("--json", action="store_true", help="输出 JSON report")
    args = parser.parse_args(argv)
    if args.write_manifest:
        write_manifest(args.root)
    report = lint_benchmark(args.root, require_evidence=not args.no_evidence)
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"benchmark lint: {'PASS' if report.ok else 'FAIL'} ({len(report.cases)} cases)")
        for issue in report.issues:
            print(f"- {issue.code}: {issue.message}")
        for case in report.cases:
            if case.ok:
                continue
            print(f"- {case.case_id}: {len(case.issues)} issue(s)")
            for issue in case.issues:
                print(f"  - {issue.code} {issue.path}: {issue.message}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BenchmarkCase",
    "BenchmarkLintReport",
    "CaseLintReport",
    "LintIssue",
    "build_manifest",
    "case_sha256",
    "discover_cases",
    "lint_benchmark",
    "lint_case",
    "main",
    "write_manifest",
]
