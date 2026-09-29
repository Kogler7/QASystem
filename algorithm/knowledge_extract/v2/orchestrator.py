"""Bounded Plan/Verify/Repair loop for targeted extraction fixes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .verification import VerificationReport


@dataclass(frozen=True)
class RepairRound:
    round_number: int
    issue_codes: tuple[str, ...]
    accepted_facts_added: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_number,
            "issue_codes": list(self.issue_codes),
            "accepted_facts_added": self.accepted_facts_added,
        }


@dataclass(frozen=True)
class RepairLoopResult:
    state: Any
    report: VerificationReport
    rounds: tuple[RepairRound, ...]
    stop_reason: str

    @property
    def succeeded(self) -> bool:
        # A report can be structurally valid while still containing an
        # unresolved coverage gap.  Such a run must remain observable as
        # incomplete instead of being reported as a successful repair.
        return self.report.valid and not self.report.repairable_issues

    @property
    def coverage_complete(self) -> bool:
        """Whether the targeted repair loop resolved all repairable issues."""
        return not self.report.repairable_issues

    @property
    def unresolved_repairable_count(self) -> int:
        return len(self.report.repairable_issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "coverage_complete": self.coverage_complete,
            "unresolved_repairable_count": self.unresolved_repairable_count,
            "unresolved_repairable": [
                issue.to_dict() for issue in self.report.repairable_issues
            ],
            "rounds": [round_result.to_dict() for round_result in self.rounds],
            "stop_reason": self.stop_reason,
        }


class TargetedRepairOrchestrator:
    """Run only concrete repairable gaps, with a hard two-round ceiling."""

    def __init__(self, *, max_rounds: int = 2):
        if not 0 <= max_rounds <= 2:
            raise ValueError("max_rounds must be between 0 and 2")
        self.max_rounds = max_rounds

    def run(
        self,
        initial_state: Any,
        *,
        verify: Callable[[Any], VerificationReport],
        repair: Callable[[Any, tuple[Any, ...], int], Any],
        apply: Callable[[Any, Any], tuple[Any, int]],
    ) -> RepairLoopResult:
        state = initial_state
        report = verify(state)
        rounds: list[RepairRound] = []
        if report.valid and not report.repairable_issues:
            return RepairLoopResult(state, report, (), "validation_complete")

        for round_number in range(1, self.max_rounds + 1):
            repairable = tuple(report.repairable_issues)
            if not repairable:
                return RepairLoopResult(state, report, tuple(rounds), "hard_gap_not_repairable")
            proposal = repair(state, repairable, round_number)
            state, accepted_facts_added = apply(state, proposal)
            rounds.append(
                RepairRound(
                    round_number=round_number,
                    issue_codes=tuple(issue.code for issue in repairable),
                    accepted_facts_added=max(0, int(accepted_facts_added)),
                )
            )
            report = verify(state)
            if report.valid and not report.repairable_issues:
                return RepairLoopResult(state, report, tuple(rounds), "validation_complete")
            if accepted_facts_added <= 0:
                return RepairLoopResult(state, report, tuple(rounds), "repair_no_new_facts")

        return RepairLoopResult(state, report, tuple(rounds), "repair_round_limit")


__all__ = ["RepairLoopResult", "RepairRound", "TargetedRepairOrchestrator"]
