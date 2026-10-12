"""Final extraction verification with explicit hard and repairable issues."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .coverage import CoverageAuditor, CoverageGap
from .evidence import verify_evidence
from .graph_utils import canonical_name


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str
    path: str = ""
    severity: str = "error"
    repair_type: str | None = None
    context: dict[str, Any] | None = None

    @property
    def is_hard(self) -> bool:
        return self.severity == "error"

    @property
    def is_repairable(self) -> bool:
        return self.repair_type is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
            "repair_type": self.repair_type,
            "context": dict(self.context or {}),
        }


@dataclass(frozen=True)
class VerificationReport:
    issues: tuple[VerificationIssue, ...]
    coverage_gaps: tuple[CoverageGap, ...] = ()

    @property
    def hard_issues(self) -> tuple[VerificationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.is_hard)

    @property
    def repairable_issues(self) -> tuple[VerificationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.is_repairable)

    @property
    def valid(self) -> bool:
        return not self.hard_issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "hard_issue_count": len(self.hard_issues),
            "repairable_issue_count": len(self.repairable_issues),
            "issues": [issue.to_dict() for issue in self.issues],
            "coverage_gaps": [gap.to_dict() for gap in self.coverage_gaps],
        }


class ExtractionVerifier:
    """Verify the frozen business result without creating or deleting facts."""

    def verify(
        self,
        plan: Any,
        schema: Any,
        manager: Any,
        request: Any,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        *,
        rejected_warnings: Iterable[dict[str, Any]] = (),
    ) -> VerificationReport:
        issues: list[VerificationIssue] = []
        raw = {"entities": entities, "relations": relations}
        sanitized, schema_warnings = schema.validate_extraction(
            raw,
            target_types=request.target_types,
            mode="strict",
        )
        for warning in schema_warnings:
            issues.append(
                VerificationIssue(
                    code=str(warning.get("code") or "SCHEMA_INVALID"),
                    message=str(warning.get("message") or "final result violates ontology"),
                    path=str(warning.get("path") or ""),
                    repair_type=_repair_type_for_code(str(warning.get("code") or "")),
                    context={"value": warning.get("value")},
                )
            )
        if len(sanitized["entities"]) != len(entities) or len(sanitized["relations"]) != len(relations):
            issues.append(
                VerificationIssue(
                    code="FINAL_SCHEMA_FACT_LOSS",
                    message="strict schema validation would remove accepted facts",
                )
            )

        entity_keys: set[tuple[str, str]] = set()
        for index, entity in enumerate(entities):
            key = (str(entity.get("type") or ""), str(entity.get("name") or ""))
            if key in entity_keys:
                issues.append(
                    VerificationIssue(
                        code="DUPLICATE_ENTITY",
                        message=f"duplicate final entity: {key[0]}::{entity.get('name')}",
                        path=f"entities[{index}]",
                    )
                )
            entity_keys.add(key)
            for prop, value in (entity.get("properties") or {}).items():
                receipt = (entity.get("property_evidence") or {}).get(prop) or {}
                spans = receipt.get("sources") or []
                if (receipt.get("value") != value or receipt.get("support") not in {"literal", "llm"}
                        or not spans or any(not span.get("source_block_ids") or
                            not all(manager.has_chunk(cid) and manager.find_span(cid, span.get("quote", "")) is not None
                                    for cid in span["source_block_ids"]) for span in spans)):
                    issues.append(VerificationIssue(code="PROPERTY_EVIDENCE_INVALID",
                        message="属性缺少与当前值一致且可定位的支持证据", path=f"entities[{index}].properties.{prop}"))

        for verdict in verify_evidence(raw, manager):
            if verdict.status == "fail":
                issues.append(
                    VerificationIssue(
                        code="EVIDENCE_INVALID",
                        message=verdict.message,
                        path=verdict.ref,
                        severity="warning",
                    )
                )

        coverage_gaps = tuple(CoverageAuditor().audit(plan, manager, request, entities, relations))
        for gap in coverage_gaps:
            issue = _coverage_issue(gap, plan)
            if issue is not None:
                issues.append(issue)

        for warning in rejected_warnings:
            code = str(warning.get("code") or "")
            repair_type = _repair_type_for_code(code)
            if repair_type is None:
                continue
            issues.append(
                VerificationIssue(
                    code=code,
                    message=str(warning.get("message") or code),
                    path=str(warning.get("path") or ""),
                    repair_type=repair_type,
                    context={"value": warning.get("value")},
                )
            )

        return VerificationReport(tuple(_deduplicate_issues(issues)), coverage_gaps)


def _coverage_issue(gap: CoverageGap, plan: Any) -> VerificationIssue | None:
    if gap.gap_type == "evidence_missing":
        return VerificationIssue(
            code="EVIDENCE_MISSING",
            message=gap.reason,
            path=gap.span_id,
            severity="warning",
        )
    if gap.gap_type == "feature_coverage":
        specs = plan.feature_specs.get(gap.target_type or "", ())
        required = {
            spec.identifier
            for spec in specs
            if spec.kind == "intrinsic" and spec.required is True
        }
        missing_required = required.intersection(gap.expected_features)
        if missing_required:
            return VerificationIssue(
                code="REQUIRED_PROPERTY_MISSING",
                message=gap.reason,
                path=gap.span_id,
                repair_type="required_property",
                context={**gap.to_dict(), "required": sorted(missing_required)},
            )
        return None
    if gap.gap_type == "structural_coverage":
        return VerificationIssue(
            code="UNCOVERED_BLOCK",
            message=gap.reason,
            path=gap.span_id,
            severity="warning",
            repair_type="uncovered_block",
            context=gap.to_dict(),
        )
    return None


def _repair_type_for_code(code: str) -> str | None:
    return {
    }.get(code)


def _deduplicate_issues(issues: list[VerificationIssue]) -> list[VerificationIssue]:
    unique: list[VerificationIssue] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue.code, issue.path, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


__all__ = ["ExtractionVerifier", "VerificationIssue", "VerificationReport"]
