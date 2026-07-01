"""Run Opus vs Fusion implementation benchmarks."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from fusion.agent.loop import run_coding_agent
from fusion.agent.prompts import FUSION_AGENT_APPENDIX
from fusion.agent.schemas import AgentUsage, CompareArmResult, CompareImplementOutput
from fusion.agent.tools import run_verify_command
from fusion.agent.workspace import WorkspaceError, WorkspaceGuard
from fusion.orchestration.pipelines import build_provider_registry, create_pipelines
from fusion.orchestration.schemas import ImplementationPlanInput
from fusion.routing.budget import BudgetLevel
from fusion.routing.model_registry import ModelRegistry
from fusion.security.policy import SecurityPolicy


def _copy_workspace(source: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="fusion-bench-"))
    ignore = shutil.ignore_patterns(
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "fusion_runs.db",
        ".ruff_cache",
    )
    shutil.copytree(source, dest, dirs_exist_ok=True, ignore=ignore)
    return dest


async def compare_implementations(
    *,
    task: str,
    workspace_root: Path,
    constraints: str = "",
    verify_command: str = "",
    max_agent_steps: int = 40,
    budget: BudgetLevel = BudgetLevel.MEDIUM,
    opus_model: str = "claude-opus",
    fusion_executor_model: str = "claude-sonnet",
    db_path: str | None = None,
    use_mock: bool = False,
) -> CompareImplementOutput:
    """Implement the same task twice: Opus agent vs Fusion-orchestrated agent."""
    security = SecurityPolicy.from_env()
    if not security.allow_file_writes:
        msg = "Agent mode disabled — set FUSION_AGENT_MODE=true"
        empty = CompareArmResult(arm="opus", summary="", usage=_empty_usage(opus_model))
        fusion_empty = CompareArmResult(
            arm="fusion",
            summary="",
            usage=_empty_usage(fusion_executor_model),
        )
        return CompareImplementOutput(
            task=task,
            workspace_root=str(workspace_root),
            opus=empty.model_copy(update={"error": msg}),
            fusion=fusion_empty.model_copy(update={"error": msg}),
            cost_delta_usd=0.0,
            latency_delta_ms=0.0,
            cheaper_arm="n/a",
            faster_arm="n/a",
            notes=[msg],
        )

    registry = ModelRegistry()
    providers = build_provider_registry(use_mock=use_mock)
    pipelines = create_pipelines(providers=providers, db_path=db_path, use_mock=use_mock)
    notes: list[str] = []

    opus_ws: Path | None = None
    fusion_ws: Path | None = None
    try:
        opus_ws = _copy_workspace(workspace_root)
        fusion_ws = _copy_workspace(workspace_root)
        opus_guard = WorkspaceGuard(opus_ws)
        opus_task = task if not constraints else f"{task}\n\nConstraints:\n{constraints}"
        opus_start = time.perf_counter()
        opus_result = await run_coding_agent(
            task=opus_task,
            workspace=opus_guard,
            model_alias=opus_model,
            registry=registry,
            providers=providers,
            security=security,
            max_steps=max_agent_steps,
        )
        if verify_command:
            code, out = await run_verify_command(guard=opus_guard, command=verify_command)
            opus_result.verify_exit_code = code
            opus_result.verify_output = out
        opus_latency = (time.perf_counter() - opus_start) * 1000

        fusion_guard = WorkspaceGuard(fusion_ws)
        plan_input = ImplementationPlanInput(
            feature_request=task,
            constraints=constraints,
            repo_context=f"Workspace root: {workspace_root}",
            budget=budget,
        )
        plan_output = await pipelines["plan"].plan(plan_input)
        orch_latency = plan_output.cost_latency.total_latency_ms
        plan_text = "\n".join(
            [
                "Implementation sequence:",
                *[f"- {s}" for s in plan_output.implementation_sequence],
                "",
                "Affected modules:",
                *[f"- {m}" for m in plan_output.affected_modules],
                "",
                "Tests to add:",
                *[f"- {t}" for t in plan_output.tests_to_add],
                *(
                    ["", "Risks:", *[f"- {r}" for r in plan_output.risks]]
                    if plan_output.risks
                    else []
                ),
            ]
        )
        fusion_task = (
            f"{opus_task}\n\n"
            "Fusion orchestration plan (use this content when writing deliverables):\n"
            f"{plan_text}"
        )
        fusion_extra = FUSION_AGENT_APPENDIX.format(plan_text=plan_text)
        fusion_start = time.perf_counter()
        fusion_result = await run_coding_agent(
            task=fusion_task,
            workspace=fusion_guard,
            model_alias=fusion_executor_model,
            registry=registry,
            providers=providers,
            security=security,
            extra_system=fusion_extra,
            max_steps=max_agent_steps,
        )
        if verify_command:
            code, out = await run_verify_command(guard=fusion_guard, command=verify_command)
            fusion_result.verify_exit_code = code
            fusion_result.verify_output = out
        fusion_latency = (time.perf_counter() - fusion_start) * 1000 + orch_latency

        opus_total_cost = opus_result.usage.cost_usd
        fusion_total_cost = fusion_result.usage.cost_usd + plan_output.cost_latency.total_cost_usd
        opus_total_latency = opus_latency
        fusion_total_latency = fusion_latency

        opus_arm = CompareArmResult(
            arm="opus",
            summary=opus_result.summary,
            files_changed=opus_result.files_changed,
            usage=opus_result.usage.model_copy(update={"latency_ms": opus_total_latency}),
            verify_exit_code=opus_result.verify_exit_code,
            verify_output=opus_result.verify_output,
            workspace_copy=str(opus_ws),
            error=opus_result.error,
        )
        fusion_arm = CompareArmResult(
            arm="fusion",
            summary=fusion_result.summary,
            files_changed=fusion_result.files_changed,
            usage=fusion_result.usage.model_copy(update={"latency_ms": fusion_total_latency}),
            orchestration=plan_output.cost_latency,
            orchestration_run_id=plan_output.run_id,
            verify_exit_code=fusion_result.verify_exit_code,
            verify_output=fusion_result.verify_output,
            workspace_copy=str(fusion_ws),
            error=fusion_result.error,
        )

        cost_delta = fusion_total_cost - opus_total_cost
        latency_delta = fusion_total_latency - opus_total_latency
        cheaper = "opus" if opus_total_cost <= fusion_total_cost else "fusion"
        faster = "opus" if opus_total_latency <= fusion_total_latency else "fusion"

        notes.append(
            "Fusion arm includes orchestration plan cost/latency plus executor agent cost/latency."
        )
        notes.append("Each arm runs in an isolated workspace copy — originals are unchanged.")
        notes.append("Opus arm uses direct Anthropic API, not Claude Code session billing.")

        return CompareImplementOutput(
            task=task,
            workspace_root=str(workspace_root.resolve()),
            opus=opus_arm,
            fusion=fusion_arm,
            cost_delta_usd=cost_delta,
            latency_delta_ms=latency_delta,
            cheaper_arm=cheaper,
            faster_arm=faster,
            notes=notes,
        )
    except WorkspaceError as exc:
        empty = CompareArmResult(
            arm="opus",
            summary="",
            usage=_empty_usage(opus_model),
            error=str(exc),
        )
        fusion_empty = CompareArmResult(
            arm="fusion", summary="", usage=_empty_usage(fusion_executor_model), error=str(exc)
        )
        return CompareImplementOutput(
            task=task,
            workspace_root=str(workspace_root),
            opus=empty,
            fusion=fusion_empty,
            cost_delta_usd=0.0,
            latency_delta_ms=0.0,
            cheaper_arm="n/a",
            faster_arm="n/a",
            notes=[str(exc)],
        )
    finally:
        if opus_ws is not None:
            shutil.rmtree(opus_ws, ignore_errors=True)
        if fusion_ws is not None:
            shutil.rmtree(fusion_ws, ignore_errors=True)


def _empty_usage(model_alias: str) -> AgentUsage:
    return AgentUsage(model_alias=model_alias, model_id="", provider="")
