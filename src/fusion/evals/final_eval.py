"""Final answer evaluation: safety checks on the text, quality measured from the claims."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fusion.evals.deterministic import check_claims_include_tests, run_deterministic_checks
from fusion.evals.schemas import FinalEvalResult

if TYPE_CHECKING:
    from fusion.orchestration.claims import AgreementReport, ClaimCluster

_RISK = {"critical": 0.9, "high": 0.7, "med": 0.4, "low": 0.2}
_NO_RISK_FOUND = 0.2
_NOT_APPLICABLE = 0.5
_ACTIONABLE = {"recommendation", "test"}


def evaluate_final_answer(
    content: str,
    *,
    is_coding_task: bool = False,
    known_files: list[str] | None = None,
    clusters: list[ClaimCluster] | None = None,
    report: AgreementReport | None = None,
) -> FinalEvalResult:
    """Evaluate the final answer.

    Safety checks run on the text (secrets, dangerous commands, unsupported file references).
    Everything else is derived from the panel's claim clusters and agreement report, never from
    the wording of the answer: with no claims (a halted run) the quality measures are 0.
    ``confidence`` is the calibrated confidence from ``report``.
    """
    det_passed, det_issues = run_deterministic_checks(
        content,
        min_length=1,
        known_files=known_files,
        structured=True,
        claim_files=[c.file for c in clusters or [] if c.file],
    )
    if not clusters or report is None:
        return FinalEvalResult(
            final_answer_quality=0.0,
            claude_code_usefulness=0.0,
            implementation_readiness=0.0,
            test_plan_quality=0.0,
            residual_risk=0.5,
            confidence=0.0,
            overall_score=0.0,
            deterministic_passed=det_passed,
            deterministic_issues=det_issues,
            notes="No panel claims to evaluate",
        )
    total = len(clusters)
    ok, issues = check_claims_include_tests(
        (c.kind for c in clusters), is_coding_task=is_coding_task
    )
    if not ok:
        det_passed = False
        det_issues = [*det_issues, *issues]
    has_test = any(c.kind == "test" for c in clusters)
    severities = [_RISK[c.severity] for c in clusters if c.severity]

    usefulness = sum(1 for c in clusters if c.kind in _ACTIONABLE) / total
    readiness = sum(1 for c in clusters if c.file and c.line) / total
    quality = report.evidence_rate / 2
    if not report.low_information:
        quality = (report.score + report.evidence_rate) / 2
    test_quality = (1.0 if has_test else 0.0) if is_coding_task else _NOT_APPLICABLE
    risk = max(severities) if severities else _NO_RISK_FOUND
    confidence = report.confidence
    overall = (quality + usefulness + readiness + test_quality + confidence) / 5.0
    return FinalEvalResult(
        final_answer_quality=quality,
        claude_code_usefulness=usefulness,
        implementation_readiness=readiness,
        test_plan_quality=test_quality,
        residual_risk=risk,
        confidence=confidence,
        overall_score=overall,
        deterministic_passed=det_passed,
        deterministic_issues=det_issues,
        notes=(
            f"Measured from {total} claim clusters across {report.n_models} model(s): "
            f"agreement {report.score:.2f}, evidence {report.evidence_rate:.2f}"
            + ("; low information (fewer than two models)" if report.low_information else "")
        ),
    )
