"""MCP tool handlers."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fusion.config.env import is_test_mode
from fusion.config.loader import LibraryConfig, load_library_config, load_verify_config
from fusion.config.paths import resolve_db_path
from fusion.library.distil import (
    Distillation,
    apply_distillation,
    distil,
    distil_prompt,
    pick_distil_model,
)
from fusion.library.retrieve import LessonBlock, render_lessons, retrieve
from fusion.library.store import Feedback, Lesson, LibraryStore, Outcome, inside, project_key
from fusion.mcp_server.patch import patch_output, selection_output
from fusion.mcp_server.schemas import (
    CompareClaudeRunsInput,
    DebugErrorInput,
    DecideArchitectureInput,
    DigestInput,
    EvalAnswerInput,
    FeedbackInput,
    FusionAskInput,
    FusionStatsInput,
    PlanFeatureInput,
    ProposePatchInput,
    ReviewDiffInput,
    SelectPatchInput,
    _OrchestrationInput,
)
from fusion.orchestration.aggregate import resolve_aggregator
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.pipeline import BasePipeline
from fusion.orchestration.pipelines import (
    AnswerEvalPipeline,
    ArchitectureDecisionPipeline,
    CodeReviewPipeline,
    DebugPipeline,
    FusionAskPipeline,
    ImplementationPlanPipeline,
    Settings,
    build_pipelines,
    build_provider_registry,
)
from fusion.orchestration.schemas import (
    AnswerEvalInput as PipelineAnswerEvalInput,
)
from fusion.orchestration.schemas import (
    ArchitectureDecisionInput as PipelineArchitectureInput,
)
from fusion.orchestration.schemas import (
    CodeReviewInput as PipelineCodeReviewInput,
)
from fusion.orchestration.schemas import (
    DebugInput as PipelineDebugInput,
)
from fusion.orchestration.schemas import (
    FusionAskInput as PipelineFusionAskInput,
)
from fusion.orchestration.schemas import (
    ImplementationPlanInput as PipelinePlanInput,
)
from fusion.orchestration.strategy import BenchmarkOnlyError, Strategy
from fusion.providers.base import close_providers
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskType
from fusion.security.redaction import redact_secrets
from fusion.storage.run_store import RunRecord, RunStepRecord, RunStore
from fusion.telemetry.stats_format import format_stats_markdown, stats_to_dict
from fusion.verify.propose import (
    Crosschecker,
    Verification,
    choose_mode,
    copy_fallback,
    host_crosschecker,
    patch_prompt,
    sandbox_crosschecker,
    select_from_results,
)
from fusion.workspace import ProjectFiles, WorkspaceReader, WorkspaceReport

# fusion_propose_patch's strategy when the call names none: every member writes a patch and the
# checks that judge any fix; with verify="none" there is nothing to run, so one is selected.
PATCH_STRATEGY = "verified-synth"
UNVERIFIED_PATCH_STRATEGY = "panel-nano-select"


def _winner_from_delta(
    delta: float | int | None,
    opus_label: str,
    fusion_label: str,
) -> str:
    if delta is None:
        return "unknown"
    if abs(float(delta)) < 1e-9:
        return "tie"
    return fusion_label if delta < 0 else opus_label


def _format_compare_markdown(
    *,
    opus_label: str,
    fusion_label: str,
    better_arm: str,
    cheaper_arm: str,
    faster_arm: str,
    opus_score: float,
    fusion_score: float,
    cost_delta: float | None,
    latency_delta: int | None,
) -> str:
    cost_line = "unknown"
    if cost_delta is not None:
        direction = "cheaper" if cost_delta < 0 else "more expensive"
        cost_line = f"{fusion_label} is ${abs(cost_delta):.4f} {direction}"
    latency_line = "unknown"
    if latency_delta is not None:
        direction = "faster" if latency_delta < 0 else "slower"
        latency_line = f"{fusion_label} is {abs(latency_delta) / 1000:.1f}s {direction}"
    return "\n".join(
        [
            "## Claude Code Run Comparison",
            "",
            "### Verdict",
            f"- Better result: {better_arm}",
            f"- Cheaper: {cheaper_arm}",
            f"- Faster: {faster_arm}",
            "",
            "### Quality",
            f"- {opus_label}: {opus_score:.2f}",
            f"- {fusion_label}: {fusion_score:.2f}",
            "",
            "### Cost & latency",
            f"- Cost delta: {cost_line}",
            f"- Latency delta: {latency_line}",
            "",
            "### Note",
            (
                "- Claude Code executes both arms; Fusion only supplies panel reasoning "
                "and evaluation."
            ),
        ]
    )


SOFT_TIMEOUT_ENV = "FUSION_TOOL_SOFT_TIMEOUT_S"
DEFAULT_SOFT_TIMEOUT_S = 90.0


def soft_timeout_from_env() -> float | None:
    """Seconds a tool call may take before it returns the panel's digest; None disables it.

    ``FUSION_TOOL_SOFT_TIMEOUT_S`` sets it (default 90); 0, ``off`` or ``none`` turn it off. An
    unusable value is reported on stderr and the default applies.
    """
    raw = os.environ.get(SOFT_TIMEOUT_ENV, "").strip().lower()
    if not raw:
        return DEFAULT_SOFT_TIMEOUT_S
    if raw in {"0", "off", "none", "false"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value < 0:
        print(
            f"fusion: ignoring {SOFT_TIMEOUT_ENV}={raw!r} (use seconds, or 0 to turn it off); "
            f"using {DEFAULT_SOFT_TIMEOUT_S:.0f}",
            file=sys.stderr,
        )
        return DEFAULT_SOFT_TIMEOUT_S
    return value or None


@dataclass
class Gathered:
    """What a call asked Fusion to read from the project, ready for a pipeline input."""

    snippets: list[str] = field(default_factory=list)  # ``file_snippets`` form
    files: list[str] = field(default_factory=list)  # paths read, relative to the root
    diff: str = ""
    report: WorkspaceReport | None = None

    def background(self, ref: str | None) -> str:
        """The diff as background text for tools other than review."""
        return f"Changes (git diff {ref}):\n{self.diff}" if self.diff.strip() else ""


READ_ROOT_ENV = "FUSION_READ_ROOT"


def default_root() -> Path:
    """The project root when the MCP client names none: ``FUSION_READ_ROOT``, else cwd."""
    configured = os.environ.get(READ_ROOT_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path.cwd()


def gather_workspace(input: _OrchestrationInput, root: Path | None) -> Gathered:
    """Read the call's ``diff_ref``, ``paths`` and ``globs`` under ``root`` (``default_root()``
    when None). The diff is read first, so it has first claim on the per-call byte limit. Raises
    ``WorkspaceError`` (a ``ValueError``) for a bad ref or glob; unreadable files are skipped."""
    if not input.reads_workspace:
        return Gathered()
    reader = WorkspaceReader.from_env(root or default_root())
    diff = reader.git_diff(input.diff_ref) if input.diff_ref else ""
    read = reader.read_paths([*input.paths, *reader.expand_globs(input.globs)])
    return Gathered(
        snippets=[s.as_file_snippet() for s in read],
        files=[s.path for s in read],
        diff=diff,
        report=reader.report(),
    )


def _with_workspace(output: dict[str, Any], gathered: Gathered) -> dict[str, Any]:
    if gathered.report is not None:
        output["workspace"] = gathered.report.model_dump()
        output["host_tokens_saved_estimate"] = gathered.report.host_tokens_saved_estimate
    return output


def _joined(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part.strip())


def _unique(*lists: list[str]) -> list[str]:
    return list(dict.fromkeys(item for items in lists for item in items))


@dataclass
class Library:
    """The experience library as one call sees it: open (``store``) or off, and why."""

    config: LibraryConfig
    project: str  # the project's key (a hash of its root)
    store: LibraryStore | None = None
    off_reason: str = ""


LIBRARY_OFF = (
    "The experience library is off, so nothing was recorded. Turn it on with "
    "`library.enabled: true` in Fusion's config (or FUSION__LIBRARY__ENABLED=true) to have "
    "Fusion learn per-project lessons from this feedback."
)
LIBRARY_INSIDE = (
    "The experience library is off for this project: Fusion's run database ({db}) is inside the "
    "project, and lessons are never stored inside a project. Point FUSION_DB_PATH elsewhere (the "
    "default is the user data directory)."
)
LESSONS_NOTE = (
    "\n\n_Lessons from this project's library were in the panel prompt: {ids}. Send "
    "fusion_feedback for run `{run_id}` so they keep or lose their standing._"
)
_FEEDBACK_SAID = {
    "created": "Learned a new lesson for this project",
    "updated": "Refined an existing lesson with this run's evidence",
    "dropped": "Dropped a lesson: what is left of its evidence is all confounded",
    "none": "No lesson: this run teaches nothing reusable (NO_ENTRY)",
}


def _feedback_output(
    input: FeedbackInput,
    lesson: Lesson | None,
    action: str,
    called: Distillation | None,
    warnings: list[str],
) -> dict[str, Any]:
    """The fusion_feedback response (before hardening)."""
    title = f"## Fusion Feedback (`{input.run_id}`: {input.outcome})"
    lines = [title, "", f"- {_FEEDBACK_SAID[action]}."]
    kept = lesson if lesson is not None and action in ("created", "updated") else None
    if kept is not None:
        lines += [
            f"- `{kept.id}`: when {kept.cue}: {kept.lesson}",
            f"- verdict {kept.verdict}, {kept.confidence} confidence, "
            f"{len(kept.evidence)} run(s) of evidence",
        ]
    cost: float | None = 0.0
    if called is not None:
        cost = called.cost_usd if called.cost_known else None
        money = f"${cost:.4f}" if cost is not None else "cost unknown"
        lines.append(f"- {money} to distil ({called.model})")
    return {
        "display_markdown": "\n".join(lines),
        "warnings": warnings,
        "run_id": input.run_id,
        "recorded": True,
        "action": action,
        "lesson": kept.model_dump() if kept is not None else None,
        "cost_usd": cost,
    }


DIGEST_INSTRUCTIONS = (
    "Answer from the files provided, in at most {tokens} tokens (about {words} words). Be "
    "concrete: name the files, functions and lines that matter. If the material does not "
    "answer the question, say so instead of guessing."
)


class FusionTools:
    """Handlers for fusion MCP tools.

    Each panel tool takes ``root``: the project directory that ``paths``, ``globs`` and
    ``diff_ref`` are read from (the MCP client's root; None means ``default_root()``).
    """

    def __init__(
        self,
        *,
        code_review: CodeReviewPipeline | None = None,
        ask: FusionAskPipeline | None = None,
        debug: DebugPipeline | None = None,
        architecture: ArchitectureDecisionPipeline | None = None,
        plan: ImplementationPlanPipeline | None = None,
        answer_eval: AnswerEvalPipeline | None = None,
        patch: BasePipeline | None = None,
        db_path: str | None = None,
        use_mock: bool | None = None,
    ) -> None:
        use_mock = is_test_mode(use_mock)
        providers = build_provider_registry(use_mock=use_mock)
        self.providers = providers
        self.run_store = RunStore(db_path=db_path)
        pipelines = build_pipelines(
            Settings(use_mock=use_mock, run_store=self.run_store), providers
        )
        self._code_review = code_review or pipelines["code_review"]
        self._ask = ask or pipelines["ask"]
        self._debug = debug or pipelines["debug"]
        self._architecture = architecture or pipelines["architecture"]
        self._plan = plan or pipelines["plan"]
        self._answer_eval = answer_eval or pipelines["answer_eval"]
        # No soft time limit: after the panel answers, the cross-check runs checks and tests.
        self._patch = patch or pipelines["patch"]
        self._db_path = db_path
        self._use_mock = use_mock
        self._library_store: LibraryStore | None = None  # opened on first use, when enabled
        soft_timeout = soft_timeout_from_env()
        for pipeline in (
            self._code_review,
            self._ask,
            self._debug,
            self._architecture,
            self._plan,
            self._answer_eval,
        ):
            pipeline.soft_timeout_s = soft_timeout

    async def aclose(self) -> None:
        """Close provider HTTP clients and the run database; call when the host shuts down."""
        await close_providers(self.providers)
        self.run_store.close()
        if self._library_store is not None:
            self._library_store.close()

    # -- the experience library ------------------------------------------------------------

    def library(self, root: Path | None) -> Library:
        """The library for the project at ``root`` (``default_root()`` when None): open when
        ``library.enabled`` is set and the run database is outside the project."""
        base = root or default_root()
        config = load_library_config()
        key = project_key(base)
        if not config.enabled:
            return Library(config, key, off_reason=LIBRARY_OFF)
        db = resolve_db_path(self._db_path)
        if inside(db, base):
            return Library(config, key, off_reason=LIBRARY_INSIDE.format(db=db))
        if self._library_store is None:
            self._library_store = LibraryStore(db_path=db)
        return Library(config, key, store=self._library_store)

    async def _lessons(
        self, task_type: TaskType, query: str, root: Path | None
    ) -> LessonBlock | None:
        """The lessons that fit this request, rendered within the configured budget; None when
        the library is off or nothing fits."""
        lib = self.library(root)
        if lib.store is None:
            return None
        found = await asyncio.to_thread(
            retrieve,
            lib.project,
            task_type.value,
            query,
            lib.config.max_lessons,
            store=lib.store,
        )
        block = render_lessons(found, lib.config.max_prompt_tokens)
        return block if block.ids else None

    async def _noted(self, output: dict[str, Any], lessons: LessonBlock | None) -> dict[str, Any]:
        """Record which lessons the run carried and say so in the response."""
        store = self._library_store
        if lessons is None or store is None:
            return output
        run_id = str(output["run_id"])
        await asyncio.to_thread(store.record_uses, run_id, lessons.ids)
        output["lessons_used"] = list(lessons.ids)
        ids = ", ".join(f"`{i}`" for i in lessons.ids)
        output["display_markdown"] = str(output.get("display_markdown", "")) + (
            LESSONS_NOTE.format(ids=ids, run_id=run_id)
        )
        return output

    async def fusion_feedback(
        self, input: FeedbackInput, *, root: Path | None = None
    ) -> dict[str, Any]:
        """Record what became of a run's answer and distil at most one lesson from it."""
        lib = self.library(root)
        store = lib.store
        if store is None:
            return {
                "display_markdown": lib.off_reason,
                "warnings": [],
                "run_id": input.run_id,
                "recorded": False,
            }
        record = await asyncio.to_thread(self.run_store.get_run, input.run_id)
        if record is None:
            msg = f"No run '{input.run_id}'. Use the run_id of a Fusion tool response."
            raise ValueError(msg)
        note = redact_secrets(input.note).text.strip()
        existing = await asyncio.to_thread(store.lessons, lib.project, record.task_type)
        used = await asyncio.to_thread(store.lessons_used, input.run_id)
        warnings: list[str] = []
        called: Distillation | None = None
        if input.outcome != "ignored" or note:  # an unexplained "ignored" teaches nothing
            called = await self._distil(lib, record, input.outcome, note, existing, used, warnings)
        lesson, action = apply_distillation(
            called.proposed if called else None,
            existing=existing,
            run_id=input.run_id,
            outcome=input.outcome,
            note=note,
            project_key=lib.project,
            task_type=record.task_type,
        )
        if lesson is not None and action in ("created", "updated"):
            await asyncio.to_thread(store.save, lesson)
        elif lesson is not None and action == "dropped":
            await asyncio.to_thread(store.forget, lesson.id)
        feedback = Feedback(
            run_id=input.run_id,
            project_key=lib.project,
            task_type=record.task_type,
            outcome=input.outcome,
            note=note,
            lesson_id=lesson.id if lesson is not None else None,
        )
        await asyncio.to_thread(store.record_feedback, feedback)
        if called is not None and called.cost_usd:
            step = RunStepRecord(
                step_name="library_distil",
                model_name=called.model,
                input_tokens=called.input_tokens,
                output_tokens=called.output_tokens,
                cost_usd=called.cost_usd,
                latency_ms=called.latency_ms,
            )
            await asyncio.to_thread(self.run_store.add_step, input.run_id, step)
        return _feedback_output(input, lesson, action, called, warnings)

    async def _distil(
        self,
        lib: Library,
        record: RunRecord,
        outcome: Outcome,
        note: str,
        existing: list[Lesson],
        used: list[str],
        warnings: list[str],
    ) -> Distillation | None:
        """One cheap call that proposes a lesson; None (with a warning) when no model can."""
        deps = self._ask.deps
        try:
            judge: str | None = deps.routing.get_policy(TaskType(record.task_type)).judge_model
        except ValueError:
            judge = None
        model = pick_distil_model(
            configured=lib.config.distil_model,
            judge=judge,
            judges=deps.registry.by_role("judge"),
            models=deps.registry.models,
            providers=deps.providers,
        )
        if model is None:
            warnings.append("No model with a configured provider can distil a lesson")
            return None
        stored = record.sanitized_input or {}
        task = "\n\n".join(
            str(part) for part in (stored.get("primary_content"), stored.get("context")) if part
        )
        called = await distil(
            model=model,
            models=deps.registry.models,
            providers=deps.providers,
            pricing=deps.pricing,
            prompt=distil_prompt(
                task_type=record.task_type,
                task=task,
                answer=str((record.output_data or {}).get("final_answer", "")),
                outcome=outcome,
                note=note,
                existing=existing,
                used=used,
            ),
        )
        warnings.extend(called.warnings)
        if called.error:
            warnings.append(f"The lesson could not be distilled: {called.error}")
        return called

    async def fusion_ask(
        self, input: FusionAskInput, *, root: Path | None = None
    ) -> dict[str, Any]:
        """Answer a general coding task using Fusion as a model-like panel."""
        got = await asyncio.to_thread(gather_workspace, input, root)
        lessons = await self._lessons(TaskType.DEFAULT, f"{input.prompt}\n{input.context}", root)
        result = await self._ask.ask(
            PipelineFusionAskInput(
                prompt=input.prompt,
                context=_joined(input.context, got.background(input.diff_ref)),
                file_snippets=[*input.file_snippets, *got.snippets],
                changed_files=_unique(input.changed_files, got.files),
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
                max_models=input.max_models,
                include_raw_outputs=input.include_raw_outputs,
                shadow_baseline=input.shadow_baseline,
                workspace=got.report,
                lessons=lessons,
            )
        )
        return await self._noted(_with_workspace(result.model_dump(), got), lessons)

    async def fusion_review_diff(
        self, input: ReviewDiffInput, *, root: Path | None = None
    ) -> dict[str, Any]:
        """Review a code diff using multi-model orchestration."""
        got = await asyncio.to_thread(gather_workspace, input, root)
        diff = _joined(input.diff, got.diff)
        if not diff.strip():
            msg = f"git diff {input.diff_ref} has no changes under the project root to review"
            raise ValueError(msg)
        query = f"{input.goals}\n{input.context}\n{diff}"
        lessons = await self._lessons(TaskType.CODE_REVIEW, query, root)
        result = await self._code_review.review(
            PipelineCodeReviewInput(
                diff=diff,
                changed_files=_unique(input.changed_files, got.files),
                repo_context=input.context,
                file_snippets=[*input.file_snippets, *got.snippets],
                goals=input.goals,
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
                max_models=input.max_models,
                include_raw_outputs=input.include_raw_outputs,
                shadow_baseline=input.shadow_baseline,
                workspace=got.report,
                lessons=lessons,
            )
        )
        return await self._noted(_with_workspace(result.model_dump(), got), lessons)

    async def fusion_debug_error(
        self, input: DebugErrorInput, *, root: Path | None = None
    ) -> dict[str, Any]:
        """Debug an error using multi-model orchestration."""
        got = await asyncio.to_thread(gather_workspace, input, root)
        error = input.error_message
        if input.stack_trace:
            error += f"\n\nStack trace:\n{input.stack_trace}"
        lessons = await self._lessons(TaskType.DEBUGGING, f"{error}\n{input.context}", root)
        result = await self._debug.debug(
            PipelineDebugInput(
                error_message=error,
                logs=input.logs,
                code_context=input.context,
                file_snippets=[*input.file_snippets, *got.snippets],
                recent_changes=_joined(input.recent_changes, got.background(input.diff_ref)),
                environment=input.environment,
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
                shadow_baseline=input.shadow_baseline,
                workspace=got.report,
                lessons=lessons,
            )
        )
        return await self._noted(_with_workspace(result.model_dump(), got), lessons)

    async def fusion_decide_architecture(
        self, input: DecideArchitectureInput, *, root: Path | None = None
    ) -> dict[str, Any]:
        """Make an architecture decision using multi-model orchestration."""
        got = await asyncio.to_thread(gather_workspace, input, root)
        query = "\n".join([input.question, input.constraints, *input.options, input.context])
        lessons = await self._lessons(TaskType.ARCHITECTURE_DECISION, query, root)
        result = await self._architecture.decide(
            PipelineArchitectureInput(
                decision_question=input.question,
                constraints=input.constraints,
                options=input.options,
                repo_context=_joined(input.context, got.background(input.diff_ref)),
                file_snippets=[*input.file_snippets, *got.snippets],
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
                shadow_baseline=input.shadow_baseline,
                workspace=got.report,
                lessons=lessons,
            )
        )
        return await self._noted(_with_workspace(result.model_dump(), got), lessons)

    async def fusion_plan_feature(
        self, input: PlanFeatureInput, *, root: Path | None = None
    ) -> dict[str, Any]:
        """Create an implementation plan using multi-model orchestration."""
        got = await asyncio.to_thread(gather_workspace, input, root)
        query = f"{input.feature_description}\n{input.constraints}\n{input.context}"
        lessons = await self._lessons(TaskType.IMPLEMENTATION_PLAN, query, root)
        result = await self._plan.plan(
            PipelinePlanInput(
                feature_request=input.feature_description,
                constraints=input.constraints,
                repo_context=_joined(input.context, got.background(input.diff_ref)),
                existing_patterns=input.existing_patterns,
                file_snippets=[*input.file_snippets, *got.snippets],
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
                shadow_baseline=input.shadow_baseline,
                workspace=got.report,
                lessons=lessons,
            )
        )
        return await self._noted(_with_workspace(result.model_dump(), got), lessons)

    async def fusion_digest(
        self, input: DigestInput, *, root: Path | None = None
    ) -> dict[str, Any]:
        """Cheap models read large material from the project and return a short answer."""
        got = await asyncio.to_thread(gather_workspace, input, root)
        if not got.snippets and not got.diff.strip():
            skipped = got.report.skipped_warning() if got.report else None
            msg = f"Nothing to digest: nothing could be read. {skipped or ''}"
            raise ValueError(msg.strip())
        tokens = input.max_answer_tokens
        rule = DIGEST_INSTRUCTIONS.format(tokens=tokens, words=int(tokens * 0.75))
        lessons = await self._lessons(TaskType.DEFAULT, f"{input.question}\n{input.context}", root)
        result = await self._ask.ask(
            PipelineFusionAskInput(
                prompt=f"{input.question}\n\n{rule}",
                context=_joined(input.context, got.background(input.diff_ref)),
                file_snippets=[*input.file_snippets, *got.snippets],
                changed_files=got.files,
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
                shadow_baseline=input.shadow_baseline,
                workspace=got.report,
                lessons=lessons,
            )
        )
        return await self._noted(_with_workspace(result.model_dump(), got), lessons)

    async def fusion_propose_patch(
        self, input: ProposePatchInput, *, root: Path | None = None
    ) -> dict[str, Any]:
        """Candidate patches from the panel, cross-checked (in a sandbox copy, by the host, or
        not at all), and the best one returned as a unified diff. Never applied."""
        base = root or default_root()
        config = load_verify_config()
        got = await asyncio.to_thread(gather_workspace, input, base)
        reader = WorkspaceReader(base)
        mode, notes = choose_mode(input.verify, config)
        # Host and none modes only normalise patches: they read the files a patch touches. The
        # sandbox needs the whole project, copied into memory once (the project is only read).
        files: Mapping[str, bytes] = ProjectFiles(reader, max_file_bytes=config.max_file_bytes)
        if mode == "sandbox":
            copy = await asyncio.to_thread(
                reader.copy_project,
                max_files=config.max_copy_files,
                max_bytes=config.max_copy_bytes,
                max_file_bytes=config.max_file_bytes,
            )
            if (why := copy_fallback(copy)) is not None:
                mode = "host"
                notes.append(why)
            else:
                files = copy.files
        seen = Verification(mode)
        checker: Crosschecker | None = None
        if mode == "sandbox":
            checker = sandbox_crosschecker(
                files, tests_cmd=input.tests_cmd, config=config, seen=seen
            )
        elif mode == "host":
            checker = host_crosschecker(files, seen=seen)
        default = PATCH_STRATEGY if mode != "none" else UNVERIFIED_PATCH_STRATEGY
        lessons = await self._lessons(TaskType.DEFAULT, f"{input.task}\n{input.context}", base)
        ctx = PipelineContext(
            task_type=TaskType.DEFAULT,
            primary_content=patch_prompt(input.task, input.tests_cmd),
            context=_joined(input.context, got.background(input.diff_ref)),
            file_snippets=[*input.file_snippets, *got.snippets],
            changed_files=got.files,
            budget=BudgetLevel(input.budget),
            strategy=input.strategy or default,
            max_cost_usd=input.max_cost_usd,
            shadow_baseline=input.shadow_baseline,
            expects_patch=True,
            crosscheck=checker,
            workspace=got.report,
            lessons=lessons,
        )
        planned = self._patch.deps.routing.resolve_strategy(ctx.strategy, ctx.budget)
        settled = resolve_aggregator(
            planned, ctx.task_type, patch=True, verified=checker is not None
        )
        seen.cross_checks = planned.kind == "auto" or settled.aggregator == "verified-synth"
        try:
            result = await self._patch.run(ctx)
        except BenchmarkOnlyError as exc:
            raise ValueError(str(exc)) from exc
        output = patch_output(
            result,
            seen=seen,
            files=files,
            tests_cmd=input.tests_cmd,
            cost=self._patch._build_cost_latency(result),
            workspace=got.report,
            notes=notes,
            detail=input.detail,
        )
        return await self._noted(output, lessons)

    async def fusion_select_patch(self, input: SelectPatchInput) -> dict[str, Any]:
        """Pick among a host-mode proposal's candidates from the host's check results. Free."""
        record = await asyncio.to_thread(self.run_store.get_run, input.run_id)
        if record is None:
            msg = f"No run '{input.run_id}'. Use the run_id of a host-mode fusion_propose_patch."
            raise ValueError(msg)
        crosscheck = ((record.output_data or {}).get("disagreement") or {}).get("crosscheck")
        chosen = select_from_results(crosscheck or {}, input.results, input.baseline)
        return selection_output(input.run_id, chosen)

    async def fusion_eval_answer(self, input: EvalAnswerInput) -> dict[str, Any]:
        """Evaluate an answer using multi-model orchestration."""
        rubric = input.rubric
        if input.expected_criteria and not rubric:
            rubric = "\n".join(f"- {c}" for c in input.expected_criteria)
        result = await self._answer_eval.evaluate(
            PipelineAnswerEvalInput(
                question=input.question,
                answer=input.answer,
                context=input.context,
                rubric=rubric,
                detail=input.detail,
            )
        )
        return result.model_dump()

    async def fusion_stats(self, input: FusionStatsInput) -> dict[str, Any]:
        """Return cumulative Fusion cost, latency, and shadow win-rate statistics."""
        stats = await self.run_store.aget_stats()
        recent = self.run_store.list_shadow_comparisons(limit=input.recent_shadow_limit)
        return {
            "display_markdown": format_stats_markdown(stats, recent),
            "result": stats_to_dict(stats, recent),
            "warnings": [],
        }

    def run_record(self, run_id: str) -> dict[str, Any]:
        """One stored run: its answer, claims, per-call cost and warnings (not the input text)."""
        record = self.run_store.get_run(run_id)
        if record is None:
            msg = f"No run '{run_id}'. Run ids come from the run_id field of a tool response."
            raise ValueError(msg)
        return {
            "run_id": record.run_id,
            "task_type": record.task_type,
            "status": record.status,
            "total_cost_usd": record.total_cost_usd,
            "total_latency_ms": record.total_latency_ms,
            "warnings": record.warnings,
            "routing": record.routing,
            "steps": [
                {
                    "step": s.step_name,
                    "model": s.model_name,
                    "provider": s.provider,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "cost_usd": s.cost_usd,
                    "latency_ms": s.latency_ms,
                }
                for s in record.steps
            ],
            "output": record.output_data,
            # What was read from the project and what was refused (paths and reasons only).
            "workspace": record.input_data.get("workspace"),
        }

    def strategies_overview(self) -> dict[str, Any]:
        """The strategies a call may name, and which one each budget preset means."""
        routing = self._code_review.deps.routing
        book = routing.strategies

        def ladder(strategy: Strategy) -> list[dict[str, Any]]:
            policy = routing.escalation_for(strategy)
            usable = policy.climbable()
            return [
                {"rung": i, "name": r.name, "strategy": r.strategy, "allowed": i in usable}
                for i, r in enumerate(policy.rungs)
            ]

        def row(s: Strategy) -> dict[str, Any]:
            found: dict[str, Any] = {
                "name": s.name,
                "kind": s.kind,
                "description": s.description,
                "models": [m.model for m in s.members],
                "rounds": s.rounds,
                "aggregator": s.aggregator,
                "aggregator_model": s.aggregator_model,
                "judge": s.judge,
                "max_cost_usd": s.max_cost_usd,
                "max_latency_s": s.max_latency_s,
            }
            if s.kind == "auto":  # the strategies that answer are the ladder's
                found["rungs"] = ladder(s)
            return found

        return {
            "budget_presets": dict(book.budget_map),
            "strategies": [row(book.get(name)) for name in book.names()],
        }

    async def fusion_compare_claude_runs(
        self,
        input: CompareClaudeRunsInput,
    ) -> dict[str, Any]:
        """Compare Claude Code + Opus output against Claude Code + Fusion output."""
        rubric = input.rubric or (
            "Score for correctness, groundedness in the provided context, Claude Code usefulness, "
            "testability, risk awareness, and minimal unsupported claims."
        )
        question = f"{input.task_prompt}\n\nRubric:\n{rubric}"
        if input.context:
            question += f"\n\nShared context / verification evidence:\n{input.context}"

        opus_eval = await self._answer_eval.evaluate(
            PipelineAnswerEvalInput(
                question=question,
                answer=input.opus_output,
                context=input.context,
                rubric=rubric,
            )
        )
        fusion_eval = await self._answer_eval.evaluate(
            PipelineAnswerEvalInput(
                question=question,
                answer=input.fusion_output,
                context=input.context,
                rubric=rubric,
            )
        )

        quality_delta = fusion_eval.score - opus_eval.score
        better_arm = (
            input.fusion_label
            if quality_delta > 0.02
            else input.opus_label
            if quality_delta < -0.02
            else "tie"
        )
        cost_delta = (
            input.fusion_cost_usd - input.opus_cost_usd
            if input.fusion_cost_usd is not None and input.opus_cost_usd is not None
            else None
        )
        latency_delta = (
            input.fusion_latency_ms - input.opus_latency_ms
            if input.fusion_latency_ms is not None and input.opus_latency_ms is not None
            else None
        )
        cheaper_arm = _winner_from_delta(cost_delta, input.opus_label, input.fusion_label)
        faster_arm = _winner_from_delta(latency_delta, input.opus_label, input.fusion_label)
        result = {
            "task_prompt": input.task_prompt,
            "better_arm": better_arm,
            "cheaper_arm": cheaper_arm,
            "faster_arm": faster_arm,
            "quality_delta": quality_delta,
            "cost_delta_usd": cost_delta,
            "latency_delta_ms": latency_delta,
            "opus": {
                "label": input.opus_label,
                "score": opus_eval.score,
                "confidence": opus_eval.confidence,
                "run_id": input.opus_run_id,
                "cost_usd": input.opus_cost_usd,
                "latency_ms": input.opus_latency_ms,
                "strengths": opus_eval.strengths,
                "weaknesses": opus_eval.weaknesses,
                "unsupported_claims": opus_eval.unsupported_claims,
                "eval_run_id": opus_eval.run_id,
            },
            "fusion": {
                "label": input.fusion_label,
                "score": fusion_eval.score,
                "confidence": fusion_eval.confidence,
                "run_id": input.fusion_run_id,
                "cost_usd": input.fusion_cost_usd,
                "latency_ms": input.fusion_latency_ms,
                "strengths": fusion_eval.strengths,
                "weaknesses": fusion_eval.weaknesses,
                "unsupported_claims": fusion_eval.unsupported_claims,
                "eval_run_id": fusion_eval.run_id,
            },
            "notes": [
                (
                    "Quality is evaluated by Fusion's answer-eval pipeline using the same "
                    "task and rubric."
                ),
                "Cost and latency winners require measured values from both arms.",
                "Claude Code remains the executor for both arms; this tool only compares outputs.",
            ],
        }
        if input.include_raw_evals:
            result["raw_evals"] = {
                "opus": opus_eval.model_dump(),
                "fusion": fusion_eval.model_dump(),
            }
        return {
            "display_markdown": _format_compare_markdown(
                opus_label=input.opus_label,
                fusion_label=input.fusion_label,
                better_arm=better_arm,
                cheaper_arm=cheaper_arm,
                faster_arm=faster_arm,
                opus_score=opus_eval.score,
                fusion_score=fusion_eval.score,
                cost_delta=cost_delta,
                latency_delta=latency_delta,
            ),
            "result": result,
            "evals": {
                "opus_eval_run_id": opus_eval.run_id,
                "fusion_eval_run_id": fusion_eval.run_id,
                "opus_score": opus_eval.score,
                "fusion_score": fusion_eval.score,
            },
            "warnings": [],
        }
