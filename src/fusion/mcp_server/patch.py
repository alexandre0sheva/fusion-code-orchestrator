"""The responses of ``fusion_propose_patch`` and ``fusion_select_patch``.

A patch response is built for a coding agent that will apply the patch itself: the diff in full
(never shortened), how to apply it, and only as much of the verification as the next step needs.
In host mode the next step is the agent's: the candidates and every check's script are in the
text, with the call that picks among them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fusion.bench.patch import PatchError, extract_patch
from fusion.mcp_server.response import details_uri, hardened
from fusion.mcp_server.schemas import PatchToolResult, SelectPatchResult
from fusion.orchestration.result import PipelineResult
from fusion.orchestration.schemas import CostLatencyInfo
from fusion.verify.crosscheck import CrossCheckReport
from fusion.verify.propose import (
    HOST_TOP,
    TESTS_CHECK,
    HostPick,
    NormalizedPatch,
    Verification,
    VerifyMode,
    normalize_patch,
)
from fusion.verify.synthesis import TASK_AUTHOR
from fusion.workspace import WorkspaceReport

__all__ = ["APPLY_HINT", "patch_output", "present_patch", "present_selection", "selection_output"]

APPLY_HINT = (
    "Fusion changed no file. Save the diff to a file and run `git apply <file>` from the project "
    "root (`git apply --check <file>` first), or make the same edits with your edit tool."
)
_HOST_STEPS = (
    "Fusion ran nothing. In a scratch copy of the project (for example `git worktree add "
    "/tmp/fusion-try HEAD`, never your working tree), run every check below on the code as it is, "
    "then with each candidate applied (`git apply`). A check passes when it exits 0. Then call "
    "`fusion_select_patch` with this run_id, `results` ({candidate: {check id: pass|fail|error|"
    "timeout}}) and `baseline` ({check id: result before any candidate}). Add your own tests' "
    "result under a new id such as 'tests'."
)


def _winner_id(result: PipelineResult) -> str | None:
    record = result.disagreement.get("crosscheck") or {}
    picked = record.get("winner") or (result.disagreement.get("selection") or {}).get("picked")
    return str(picked) if picked else None


def _chosen_patch(
    result: PipelineResult, seen: Verification, files: Mapping[str, bytes], warnings: list[str]
) -> tuple[str | None, NormalizedPatch | None]:
    """(the member whose patch was chosen, that patch normalised), or None and a warning why."""
    winner = _winner_id(result)
    by_id = {c.id: c for c in seen.candidates}
    raw = by_id[winner].patch if winner in by_id else None
    if raw is None:
        found = extract_patch(result.final_answer, result.structured_output)
        raw = found.text
        if raw is None:
            warnings.append(f"No patch to return: {found.problem or 'the answer holds none'}")
            return winner, None
    try:
        return winner, normalize_patch(files, raw)
    except PatchError as exc:
        warnings.append(f"The chosen patch was rejected: {exc}")
        return winner, None


def _sandbox_checks(report: CrossCheckReport, winner: str | None) -> list[dict[str, Any]]:
    row = report.matrix.cells.get(winner or "", {})
    return [
        {
            "id": r.id,
            "author": r.author,
            "kind": r.check.kind,
            "language": r.check.language,
            "body": r.check.body,
            "result": row.get(r.id),
        }
        for r in report.kept
    ]


def _dropped_line(report: CrossCheckReport) -> str:
    counts: dict[str, int] = {}
    for record in report.checks:
        if record.status != "kept":
            counts[record.status] = counts.get(record.status, 0) + 1
    if not counts:
        return ""
    shown = ", ".join(f"{n} {status.replace('_', ' ')}" for status, n in sorted(counts.items()))
    return f" ({shown} dropped)"


def _host_candidates(report: CrossCheckReport, first: str | None) -> list[dict[str, Any]]:
    """The top ``HOST_TOP`` distinct, applicable candidates: the selector's pick first."""
    handoff = report.handoff or {}
    entries = [c for c in handoff.get("candidates", []) if c.get("patch")]
    entries.sort(key=lambda c: c["id"] != first)  # stable: panel order after the pick
    top: list[dict[str, Any]] = []
    groups: set[str] = set()
    for entry in entries:
        if entry["group"] in groups:
            continue
        groups.add(entry["group"])
        top.append({"id": entry["id"], "patch": entry["patch"]})
        if len(top) == HOST_TOP:
            break
    return top


def _cost_line(cost: CostLatencyInfo, models: int, saved: int | None) -> str:
    money = (
        f"${cost.total_cost_usd:.4f}"
        if cost.total_cost_known and cost.total_cost_usd is not None
        else "cost unknown"
    )
    parts = [money, f"{cost.total_latency_ms / 1000:.1f}s", f"{models} model call(s)"]
    if saved:
        parts.append(f"~{saved:,} host tokens saved (files Fusion read itself)")
    return "- " + " · ".join(parts)


def _fence(body: str, lang: str) -> str:
    ticks = "```"
    while ticks in body:
        ticks += "`"
    return f"{ticks}{lang}\n{body.rstrip()}\n{ticks}"


def patch_output(
    result: PipelineResult,
    *,
    seen: Verification,
    files: Mapping[str, bytes],
    tests_cmd: str | None,
    cost: CostLatencyInfo,
    workspace: WorkspaceReport | None,
    notes: list[str],
    detail: str,
) -> dict[str, Any]:
    """The fusion_propose_patch response for one finished run (before hardening)."""
    mode: VerifyMode = seen.mode
    warnings = [*notes, *result.warnings]
    report = seen.report
    if mode != "none" and report is None and not seen.cross_checks:
        warnings.append(
            f"The strategy '{result.routing.strategy}' does not cross-check patches, so nothing "
            "was verified (verified-synth, the default, does)"
        )
    winner, chosen = _chosen_patch(result, seen, files, warnings)
    saved = workspace.host_tokens_saved_estimate if workspace else None
    usage = result.usage
    models = usage.successful_model_calls if usage is not None else 0
    out: dict[str, Any] = {
        "run_id": result.run_id,
        "verify": mode,
        "strategy": result.routing.strategy,
        "cost_usd": cost.total_cost_usd if cost.total_cost_known else None,
        "latency_s": round(cost.total_latency_ms / 1000, 2),
        "models_called": models,
        "details_uri": details_uri(result.run_id),
        "host_tokens_saved_estimate": saved,
        "winner": winner,
        "reason": report.reason if report is not None else "",
    }
    title = {"sandbox": "verified in a sandbox", "host": "for you to verify", "none": "unverified"}
    heading = title[mode] if report is not None else "unverified"
    lines = [f"## Fusion Patch ({heading})", ""]
    if mode == "host" and report is not None:
        top = _host_candidates(report, winner if chosen else None)
        checks = list((report.handoff or {}).get("checks", []))
        out.update(candidates=top, checks=checks, apply=_HOST_STEPS)
        if not top:
            warnings.append("No candidate patch applies to the project as it is")
        lines += ["### Next step", _HOST_STEPS, ""]
        if tests_cmd:
            lines += [f"Your tests: run `{tests_cmd}` and report it as `{TESTS_CHECK}`.", ""]
        for entry in top:
            lines += [f"### Candidate `{entry['id']}`", _fence(entry["patch"], "diff"), ""]
        lines.append(f"### Checks ({len(checks)})")
        for check in checks:
            what = (
                "invariant: passes before and after a correct fix"
                if check["kind"] == "invariant"
                else f"{check['kind']}: fails before a correct fix, passes after"
            )
            lang = "python" if check["language"] == "python" else "bash"
            lines += [f"- `{check['id']}` ({what})", _fence(check["body"], lang)]
    else:
        if chosen is not None:
            out.update(patch=chosen.diff, files=chosen.files, apply=APPLY_HINT)
            lines += ["### Patch", _fence(chosen.diff, "diff"), "", "### Apply", APPLY_HINT]
        else:
            out["apply"] = "No patch to apply; see the warnings."
            lines += ["### Patch", "No applicable patch was produced."]
        if mode == "sandbox" and report is not None:
            checks = _sandbox_checks(report, winner)
            out.update(checks=checks, matrix=report.matrix.cells, isolation=report.isolation)
            passed = sum(c["result"] == "pass" for c in checks)
            lines += [
                "",
                "### Verification",
                f"- {report.reason}",
                f"- {passed} of {len(checks)} checks pass with this patch"
                f"{_dropped_line(report)}; isolation: {report.isolation}",
            ]
            for check in checks:
                who = "your tests_cmd" if check["author"] == TASK_AUTHOR else check["author"]
                lines.append(f"- `{check['id']}` ({check['kind']}, by {who}): {check['result']}")
                if detail == "full":
                    lang = "python" if check["language"] == "python" else "bash"
                    lines.append(_fence(check["body"], lang))
    lines += ["", "### Cost", _cost_line(cost, models, saved)]
    if warnings:
        lines += ["", "### Caveats", *(f"- {w}" for w in warnings)]
    out["warnings"] = warnings
    out["display_markdown"] = "\n".join(lines)
    return out


def present_patch(output: dict[str, Any]) -> PatchToolResult:
    return PatchToolResult(**hardened(output))


def selection_output(run_id: str, chosen: HostPick) -> dict[str, Any]:
    """The fusion_select_patch response (before hardening)."""
    lines = ["## Fusion Patch (picked from your results)", "", f"- {chosen.reason}"]
    for check, why in chosen.dropped.items():
        lines.append(f"- `{check}` not counted: {why}")
    if chosen.host_checks:
        lines.append(f"- Your own checks, counted in full: {', '.join(chosen.host_checks)}")
    warnings: list[str] = []
    if chosen.patch:
        lines += ["", "### Patch", _fence(chosen.patch, "diff"), "", "### Apply", APPLY_HINT]
    else:
        warnings.append("No candidate can be picked: none of those you tried applies")
    scores = {k: round(v, 2) for k, v in chosen.scores.items()}
    lines += ["", "### Scores", f"- {json.dumps(scores)}"]
    return {
        "display_markdown": "\n".join(lines),
        "warnings": warnings,
        "run_id": run_id,
        "winner": chosen.winner,
        "patch": chosen.patch,
        "reason": chosen.reason,
        "scores": chosen.scores,
        "apply": APPLY_HINT if chosen.patch else "",
    }


def present_selection(output: dict[str, Any]) -> SelectPatchResult:
    return SelectPatchResult(**hardened(output))
