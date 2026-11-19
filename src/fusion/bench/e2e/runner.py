"""Run the end-to-end study: headless Claude Code on executable tasks, with and without Fusion.

Arms are executor x Fusion: Claude Code on Opus, Sonnet or Haiku (``EXECUTORS``), each with Fusion
off (no MCP server) or on (the Fusion MCP server from this checkout, the plugin's skills, agent and
commands, and a system-prompt line telling it to consult Fusion). Every arm gets the same task
prompt: the task's own text, asking for edits in place instead of a patch.

Each run works on a **sandboxed temp copy** of the task's repo (roadmap Part 3, rule 5): the task's
files are copied into a fresh ``Sandbox`` (a git snapshot is committed there, so ``git diff`` and
Fusion's ``diff_ref`` work), ``claude`` runs in it with a scrubbed environment, ``HOME`` inside the
sandbox and ``claude_cli.claude_settings`` (Bash sandboxed with no network, edits only in the
copy), and the copy is deleted afterwards. The edited tree becomes a diff that the task's hidden
tests judge in a second, network-less sandbox (``run_hidden_tests``), exactly as a patch from a
bench arm is judged. The task's source is only ever read.

Money: before each run, its worst case (``run_budget_usd``, which Claude Code itself enforces with
``--max-budget-usd``, plus ``fusion_allowance_usd`` for a Fusion arm) is reserved against the run's
``max_usd`` and the live-spend ledger; the run stops cleanly, resumable, when it would not fit.
After each run its measured cost (host plus Fusion) is appended to the ledger.

Items go to the bench store like any other run (``BenchItem.e2e`` holds the end-to-end numbers),
so ``fusion bench show`` and ``report`` read them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fusion import __version__
from fusion.bench.datasets.coding import patch_request
from fusion.bench.e2e.claude_cli import (
    FUSION_SERVER,
    ClaudeInvocation,
    ClaudeResult,
    build_argv,
    claude_settings,
    mcp_config,
    parse_output,
)
from fusion.bench.e2e.fake_claude import RECORDING_ENV, mock_recording
from fusion.bench.e2e.metrics import E2EMetrics, bench_metrics, fusion_ledger, price_host_usage
from fusion.bench.meta import ArmMeta, RunMeta, TaskMeta, dataset_hash, model_meta, write_meta
from fusion.bench.patch import diff_files
from fusion.bench.runner import BenchProgress, BenchRun
from fusion.bench.sandbox import RunResult, Sandbox, SandboxError, SandboxLimits
from fusion.bench.scoring import ScoreResult
from fusion.bench.scoring.coding import run_hidden_tests
from fusion.bench.spec import (
    Arm,
    BenchConfig,
    BenchTask,
    CodingTruth,
    load_dataset,
    select_tasks,
    task_hash,
)
from fusion.bench.spend import SpendCapError, SpendLedger
from fusion.bench.store import BenchItem, BenchStore, ItemStatus, RunStatus, new_bench_run_id
from fusion.routing.model_registry import ModelRegistry
from fusion.security.redaction import redact_error

__all__ = [
    "E2E_FILE",
    "EXECUTORS",
    "E2EArm",
    "E2EConfig",
    "E2EEnv",
    "arm_matrix",
    "default_plugin_dir",
    "e2e_prompt",
    "e2e_tasks",
    "is_e2e_run",
    "load_e2e_config",
    "parse_e2e_arms",
    "run_e2e",
]

Executor = Literal["opus", "sonnet", "haiku"]
# Executor -> catalog alias; the catalog's model id pins the model Claude Code runs.
EXECUTORS: dict[str, str] = {
    "opus": "claude-opus",
    "sonnet": "claude-sonnet",
    "haiku": "claude-haiku",
}
E2E_FILE = "e2e.json"  # the study's own config, beside the bench config of the run
SPEND_TASK = "bench-e2e"  # the ``task`` field of spend-ledger entries written by this study
STRATEGY = "claude-code"  # what the bench config and meta call these arms' strategy
_EPSILON = 1e-9
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024  # a long stream-json transcript must arrive whole
_MAX_READ_BYTES = 2 * 1024 * 1024  # files bigger than this in the edited copy are not diffed
_SKIP_DIRS = frozenset({".git", ".claude", "__pycache__", ".pytest_cache", ".mypy_cache"})
_MAX_PATCH_CHARS = 200_000  # of the diff kept with the item
TRANSCRIPT_DIR = "evidence/e2e"  # under the run's folder: <job key>.jsonl per run
_UNKNOWN_MODEL = "unrecognized_model"  # what Claude Code prints for a model id it does not know

E2E_REQUEST = (
    "Edit the files in the current directory to make the change (and nothing outside it). "
    "Do not edit or delete the tests. {limits} You may run the tests "
    "(`python -m unittest` or `python -m pytest`); commands have no network. When you are done, "
    "reply with one short paragraph saying what you changed."
)
FUSION_GUIDANCE = (
    "Fusion MCP tools (mcp__fusion__*) give you a second opinion from a panel of cheaper models. "
    "Use them for the substantive reasoning of this task: fusion_ask or fusion_plan before "
    "you edit, fusion_debug_error when a test fails, fusion_propose_patch when you want candidate "
    "fixes checked, and fusion_review_diff (diff_ref HEAD) before you finish. Name files in "
    "`paths` instead of pasting them. Apply edits and run commands yourself."
)


class E2EArm(BaseModel):
    """One contestant: Claude Code on ``executor``, with or without Fusion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    executor: Executor
    fusion: bool


def arm_matrix(
    executors: Sequence[str] = ("opus", "sonnet", "haiku"),
    fusion: Sequence[bool] = (False, True),
) -> list[E2EArm]:
    """Every executor with every Fusion setting, executor-major: opus, opus+fusion, sonnet, ..."""
    return [
        E2EArm(name=f"{e}+fusion" if on else e, executor=e, fusion=on)  # type: ignore[arg-type]
        for e in executors
        for on in fusion
    ]


def parse_e2e_arms(executors: str | None, fusion: str) -> list[E2EArm]:
    """Arms from the CLI: ``executors`` comma-separated (default all three), ``fusion`` one of
    ``both``, ``off``, ``on``."""
    names = [e.strip() for e in (executors or "opus,sonnet,haiku").split(",") if e.strip()]
    unknown = [n for n in names if n not in EXECUTORS]
    if unknown or not names:
        msg = f"unknown executor {', '.join(unknown) or '(none)'}; use {', '.join(EXECUTORS)}"
        raise ValueError(msg)
    modes = {"both": (False, True), "off": (False,), "on": (True,)}
    if fusion not in modes:
        msg = f"--fusion must be both, off or on, not '{fusion}'"
        raise ValueError(msg)
    return arm_matrix(list(dict.fromkeys(names)), modes[fusion])


class E2EConfig(BaseModel):
    """Everything that defines an end-to-end study. Stored with the run (``e2e.json``)."""

    model_config = ConfigDict(extra="forbid")

    dataset: Path
    arms: list[E2EArm] = Field(min_length=1)
    split: Literal["dev", "test", "all"] = "dev"
    limit: int | None = Field(default=None, ge=1)
    repeats: int = Field(default=1, ge=1)
    seed: int = 0
    max_usd: float = Field(gt=0)
    run_budget_usd: float = Field(default=1.0, gt=0)  # Claude Code's --max-budget-usd per run
    fusion_allowance_usd: float = Field(default=0.25, ge=0)  # reserved for Fusion per Fusion run
    timeout_s: float = Field(default=900.0, gt=0)
    concurrency: int = Field(default=1, ge=1)
    mock: bool = False
    spend_stop_usd: float | None = Field(default=None, gt=0)  # stop short of the ledger's cap
    claude_version: str | None = None  # what ``claude --version`` said when the run started

    def reserve_for(self, arm: E2EArm) -> float:
        return self.run_budget_usd + (self.fusion_allowance_usd if arm.fusion else 0.0)


def e2e_tasks(cfg: E2EConfig) -> list[BenchTask]:
    """The executable coding tasks of the dataset (hidden tests decide them), split and limited."""
    tasks = [
        t
        for t in load_dataset(cfg.dataset, cfg.split)
        if t.expects_patch and t.category == "coding"
    ]
    return select_tasks(tasks, cfg.limit, cfg.seed)


def e2e_prompt(task: BenchTask) -> str:
    """The task's text with its "answer with a patch" paragraph replaced by "edit the files"."""
    closing = patch_request(task.category)
    text = task.prompt.strip()
    if text.endswith(closing.strip()):
        text = text[: -len(closing.strip())].rstrip()
    limits = closing.split("Do not edit or delete the tests. ", 1)[-1].strip()
    request = E2E_REQUEST.format(limits=limits)
    context = f"\n\n{task.context.strip()}" if task.context.strip() else ""
    return f"{text}{context}\n\n{request}"


def default_plugin_dir() -> Path | None:
    """The Claude Code plugin of this checkout (it is not part of the wheel)."""
    root = Path(__file__).resolve().parents[4] / "plugin"
    return root if (root / ".claude-plugin" / "plugin.json").is_file() else None


@dataclass
class E2EEnv:
    """What a study needs from outside: the ``claude`` command, storage, catalog and money."""

    claude_cmd: list[str]
    store: BenchStore
    registry: ModelRegistry
    spend: SpendLedger | None = None  # live runs only
    plugin_dir: Path | None = None  # copied into each Fusion run (its .mcp.json left out)
    auth_env: dict[str, str] = field(default_factory=dict)  # for Claude Code: ANTHROPIC_API_KEY
    provider_env: dict[str, str] = field(default_factory=dict)  # for the Fusion server only
    fusion_cmd: list[str] = field(
        default_factory=lambda: [sys.executable, "-m", "fusion.cli.main", "mcp"]
    )


@dataclass(frozen=True)
class _Job:
    key: str
    task: BenchTask
    arm: E2EArm
    repeat: int
    seed: int


def _job_key(
    task: BenchTask, arm: E2EArm, model_id: str, repeat: int, seed: int, mock: bool
) -> str:
    blob = json.dumps(
        {
            "kind": "e2e",
            "task": task_hash(task),
            "arm": arm.model_dump(mode="json"),
            "model": model_id,
            "repeat": repeat,
            "seed": seed,
            "mock": mock,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


class _Budget:
    """Reserves each run's worst case against the run's cap and the live-spend ledger.

    A run that does not fit only because of the runs in flight waits for them to settle (their
    actual cost is usually far below what they reserved); the study stops only when a run would
    not fit even with nothing in flight."""

    def __init__(self, max_usd: float, spent: float, spend: SpendLedger | None) -> None:
        self.max_usd = max_usd
        self.spent = spent
        self.reserved = 0.0
        self.spend = spend
        self.reason: str | None = None
        self._cond = threading.Condition()

    def _refusal(self, estimate: float, reserved: float) -> str | None:
        """Why a run reserving ``estimate`` beside ``reserved`` does not fit; None if it does."""
        if self.spent + reserved + estimate > self.max_usd + _EPSILON:
            return (
                f"max_usd reached: ${self.spent:.4f} of ${self.max_usd:.2f} spent and the "
                f"next run may cost up to ${estimate:.4f}"
            )
        if self.spend is not None:
            try:
                self.spend.check(reserved + estimate, "a Claude Code run")
            except SpendCapError as exc:
                return str(exc)
        return None

    def acquire(self, estimate: float) -> bool:
        with self._cond:
            while True:
                if self.reason is not None:
                    return False
                refusal = self._refusal(estimate, self.reserved)
                if refusal is None:
                    self.reserved += estimate
                    return True
                if self.reserved > 0 and self._refusal(estimate, 0.0) is None:
                    self._cond.wait()  # fits once the runs in flight settle
                    continue
                self.reason = refusal
                self._cond.notify_all()
                return False

    def settle(self, estimate: float, actual: float) -> None:
        with self._cond:
            self.reserved -= estimate
            self.spent += actual
            self._cond.notify_all()


def bench_config(cfg: E2EConfig) -> BenchConfig:
    """The run's record in the bench store (arms named, strategy ``claude-code``)."""
    return BenchConfig(
        dataset=cfg.dataset,
        arms=[Arm(name=a.name, strategy=STRATEGY) for a in cfg.arms],
        repeats=cfg.repeats,
        seed=cfg.seed,
        max_usd=cfg.max_usd,
        concurrency=cfg.concurrency,
        mock=cfg.mock,
        cache=False,
        split=cfg.split,
        limit=cfg.limit,
        spend_stop_usd=cfg.spend_stop_usd,
    )


def is_e2e_run(store: BenchStore, run_id: str) -> bool:
    return (store.run_dir(run_id) / E2E_FILE).is_file()


def load_e2e_config(store: BenchStore, run_id: str) -> E2EConfig:
    return E2EConfig.model_validate_json(
        (store.run_dir(run_id) / E2E_FILE).read_text(encoding="utf-8")
    )


def _meta(cfg: E2EConfig, tasks: list[BenchTask], registry: ModelRegistry) -> RunMeta:
    aliases = [EXECUTORS[a.executor] for a in cfg.arms]
    return RunMeta(
        version=__version__,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        dataset=str(cfg.dataset),
        dataset_hash=dataset_hash(tasks),
        split=cfg.split,
        task_count=len(tasks),
        tasks={t.id: TaskMeta(category=t.category, difficulty=t.difficulty) for t in tasks},
        arms=[
            ArmMeta(
                name=a.name,
                strategy=f"{STRATEGY} {a.executor}" + (" + fusion" if a.fusion else ""),
                kind=STRATEGY,
                members=[EXECUTORS[a.executor]],
            )
            for a in cfg.arms
        ],
        models={
            alias: model_meta(alias, registry.models[alias])
            for alias in dict.fromkeys(aliases)
            if alias in registry.models
        },
    )


def run_e2e(
    cfg: E2EConfig,
    env: E2EEnv,
    *,
    run_id: str | None = None,
    on_item: Callable[[BenchItem, BenchProgress], None] | None = None,
) -> BenchRun:
    """Run (or resume, when ``run_id`` names an existing run) the study ``cfg`` describes."""
    tasks = e2e_tasks(cfg)
    models = {a.executor: env.registry.models[EXECUTORS[a.executor]].model_id for a in cfg.arms}
    spend = env.spend if not cfg.mock else None
    if spend is not None and cfg.spend_stop_usd is not None:
        spend = SpendLedger(
            spend.path, budget=spend.budget, cap_usd=min(spend.cap_usd, cfg.spend_stop_usd)
        )
    jobs = [
        _Job(
            key=_job_key(task, arm, models[arm.executor], repeat, cfg.seed + repeat - 1, cfg.mock),
            task=task,
            arm=arm,
            repeat=repeat,
            seed=cfg.seed + repeat - 1,
        )
        for repeat in range(1, cfg.repeats + 1)  # repeats outermost, as in ``run_bench``
        for task in tasks
        for arm in cfg.arms
    ]
    store = env.store
    bench_cfg = bench_config(cfg)
    rid = run_id or new_bench_run_id()
    if store.get_run(rid) is None:
        store.create_run(rid, bench_cfg, len(jobs))
        (store.run_dir(rid) / E2E_FILE).write_text(cfg.model_dump_json(indent=1), encoding="utf-8")
        write_meta(store.run_dir(rid), _meta(cfg, tasks, env.registry))
    else:
        store.sync_index(rid)
        store.update_run(rid, status="running", total_jobs=len(jobs))
    finished = store.completed_keys(rid)
    wanted = {j.key for j in jobs}
    resumed = len(finished & wanted)
    pending = [j for j in jobs if j.key not in finished]
    spent, _ = store.spent(rid)
    budget = _Budget(cfg.max_usd, spent, spend)
    lock = threading.Lock()
    done = resumed

    def work(job: _Job) -> None:
        nonlocal done, spent
        reserve = cfg.reserve_for(job.arm)
        if not budget.acquire(reserve):
            return
        item: BenchItem | None = None
        try:
            item = _run_job(job, rid, cfg, env, models[job.arm.executor])
        finally:
            actual = item.metrics.cost_usd if item else 0.0
            if spend is not None and actual > 0:
                spend.append(actual, task=SPEND_TASK, purpose=f"{rid} {job.arm.name} {job.task.id}")
            budget.settle(reserve, actual)
        with lock:
            store.add_item(item)
            spent += item.metrics.cost_usd
            if item.status in ("completed", "halted"):
                done += 1
            store.update_run(rid, status="running", done_jobs=done, spent_usd=spent)
            if on_item is not None:
                on_item(item, BenchProgress(done, len(jobs), spent))

    status: RunStatus = "failed"
    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            for future in [pool.submit(work, j) for j in pending]:
                future.result()
        status = "stopped" if budget.reason else "completed"
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    finally:
        store.update_run(
            rid, status=status, stop_reason=budget.reason, done_jobs=done, spent_usd=spent
        )
    items = [i for i in store.items(rid) if i.job_key in wanted]
    return BenchRun(
        run_id=rid,
        status=status,
        stop_reason=budget.reason,
        config=bench_cfg,
        total_jobs=len(jobs),
        done_jobs=done,
        resumed_jobs=resumed,
        failed_jobs=sum(1 for i in items if i.status == "error"),
        spent_usd=spent,
        eval_spent_usd=0.0,
        items=items,
    )


# -- one run --------------------------------------------------------------------------------------


def _git_snapshot(box: Sandbox) -> bool:
    """Commit the copy as it was given, so ``git diff`` shows the agent's edits. False: no git."""
    if shutil.which("git") is None:
        return False
    ident = ["-c", "user.name=fusion-e2e", "-c", "user.email=fusion-e2e@localhost"]
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", *ident, "commit", "-q", "-m", "task as given"],
    ):
        if not box.run(cmd, timeout=60).ok:
            return False
    return True


def _copy_plugin(source: Path, target: Path) -> Path:
    """The plugin without its .mcp.json (the run's own --mcp-config starts this checkout's server),
    with its tool names pointed at that server."""
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".mcp.json", "__pycache__"))
    for path in target.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        renamed = text.replace("mcp__plugin_fusion_fusion", f"mcp__{FUSION_SERVER}")
        if renamed != text:
            path.write_text(renamed, encoding="utf-8")
    return target


def _read_tree(root: Path) -> dict[str, str]:
    """The text files of the edited copy (git, caches, binaries and huge files left out)."""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if not path.is_file() or path.is_symlink() or path.suffix in (".pyc", ".pyo"):
            continue
        if path.stat().st_size > _MAX_READ_BYTES:
            continue
        try:
            tree[rel.as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return tree


def _claude_env(box: Sandbox, env: E2EEnv, recording: Path | None) -> dict[str, str]:
    run_env = {
        "CLAUDE_CONFIG_DIR": str(box.root / "home" / ".claude"),
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # Fusion's interpreter first, so `python -m pytest` in the agent's Bash finds the test tools
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
        **env.auth_env,
    }
    if recording is not None:
        run_env[RECORDING_ENV] = str(recording)
    return run_env


def _run_claude(
    job: _Job, cfg: E2EConfig, env: E2EEnv, model_id: str
) -> tuple[RunResult, ClaudeResult, tuple[int, float], dict[str, str], bool]:
    """Claude Code on a sandboxed copy of the task: its result, Fusion's ledger, the edited tree."""
    limits = SandboxLimits(
        timeout_s=cfg.timeout_s, mem_mb=None, max_output_bytes=_MAX_OUTPUT_BYTES, max_file_mb=256
    )
    # Claude Code needs the network for its own API calls, and on macOS its Bash sandbox cannot run
    # inside another Seatbelt profile; so this outer sandbox gives the temp copy, the scrubbed
    # environment and the limits, and Claude Code's sandbox takes the network away from commands.
    with Sandbox(isolation="off", limits=limits) as box:
        box.copy_in(job.task.files)
        git = _git_snapshot(box)
        secrets = box.root / "aux"
        fusion_dir = secrets / "fusion"
        fusion_db = fusion_dir / "runs.db"
        server_env = {
            "FUSION_DB_PATH": str(fusion_db),
            "FUSION_DATA_DIR": str(fusion_dir / "data"),
            "FUSION_CONFIG_DIR": str(fusion_dir / "config"),
            "FUSION_PROJECT_DIR": str(box.work),
            "FUSION_READ_ROOT": str(box.work),
            **env.provider_env,
        }
        if cfg.mock:
            server_env["FUSION_DEFAULT_PROVIDER"] = "mock"
        server = env.fusion_cmd if job.arm.fusion else None
        mcp_path = box.write_aux("mcp.json", json.dumps(mcp_config(server, server_env)))
        settings_path = box.write_aux(
            "settings.json", json.dumps(claude_settings(secrets_dir=secrets))
        )
        plugin = (
            _copy_plugin(env.plugin_dir, secrets / "plugin")
            if job.arm.fusion and env.plugin_dir is not None
            else None
        )
        recording = (
            box.write_aux(
                "recording.json",
                json.dumps(
                    mock_recording(
                        job.task, job.arm, model_id, seed=job.seed, registry=env.registry
                    )
                ),
            )
            if cfg.mock
            else None
        )
        invocation = ClaudeInvocation(
            model=model_id,
            prompt=e2e_prompt(job.task),
            max_budget_usd=cfg.run_budget_usd,
            settings_path=settings_path,
            mcp_config_path=mcp_path,
            plugin_dir=plugin,
            append_system_prompt=FUSION_GUIDANCE if job.arm.fusion else "",
            allowed_tools=[f"mcp__{FUSION_SERVER}"] if job.arm.fusion else [],
        )
        result = box.run(
            build_argv(env.claude_cmd, invocation), env=_claude_env(box, env, recording)
        )
        parsed = parse_output(result.stdout)
        ledger = fusion_ledger(fusion_db)
        after = _read_tree(box.work)
    return result, parsed, ledger, after, git


def _keep_transcript(run_dir: Path, key: str, result: RunResult) -> None:
    """Claude Code's stream-json output (and stderr) beside the run, to read a run afterwards."""
    folder = run_dir / TRANSCRIPT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{key}.jsonl").write_text(result.stdout, encoding="utf-8")
    if result.stderr.strip():
        (folder / f"{key}.stderr.txt").write_text(result.stderr, encoding="utf-8")


def _run_job(job: _Job, run_id: str, cfg: E2EConfig, env: E2EEnv, model_id: str) -> BenchItem:
    """One run, scored. Never raises for a failed run: that becomes an ``error`` item."""
    task, arm = job.task, job.arm
    truth = CodingTruth.model_validate(task.truth)
    expected = len(truth.expected_pass)
    common = {
        "run_id": run_id,
        "job_key": job.key,
        "task_id": task.id,
        "category": task.category,
        "arm": arm.name,
        "repeat": job.repeat,
        "seed": job.seed,
    }
    try:
        result, parsed, (fusion_runs, fusion_cost), after, git = _run_claude(
            job, cfg, env, model_id
        )
    except Exception as exc:  # noqa: BLE001 — a run that could not start is a result
        metrics = E2EMetrics(
            executor=arm.executor,
            model_id=model_id,
            fusion=arm.fusion,
            host_cost_usd=None,
            wall_seconds=0.0,
            tests_expected=expected,
            claude_error=redact_error(f"{type(exc).__name__}: {exc}"),
        )
        return BenchItem(
            **common,  # type: ignore[arg-type]
            status="error",
            error=metrics.claude_error,
            metrics=bench_metrics(metrics).model_copy(update={"quality": None}),
            e2e=metrics.model_dump(mode="json"),
        )

    patch = diff_files(task.files, after, delete_missing=True)
    changed = sorted(p for p in {*task.files, *after} if task.files.get(p) != after.get(p))
    wall = result.seconds
    if cfg.mock and parsed.duration_ms is not None:
        wall = parsed.duration_ms / 1000  # the fake answers at once; use the time it reports
    _keep_transcript(env.store.run_dir(run_id), job.key, result)
    warnings: list[str] = []
    if not git:
        warnings.append("no git snapshot in the copy: `git diff` was not available to the agent")
    host_cost, unpriced = price_host_usage(parsed.model_usage, env.registry.models)
    if host_cost is None and not parsed.model_usage:
        host_cost = parsed.host_cost_usd  # no per-model usage printed: Claude Code's figure
    if unpriced:
        warnings.append(
            "counted at Claude Code's own cost figure (not in the catalog): " + ", ".join(unpriced)
        )
    if _UNKNOWN_MODEL in result.stdout or _UNKNOWN_MODEL in result.stderr:
        warnings.append(
            f"Claude Code does not know the model {model_id}: its own cost figure and "
            "--max-budget-usd use a default price, so the run may stop early (update Claude Code)"
        )
    claude_error = parsed.error
    if result.timed_out:
        claude_error = f"timed out after {cfg.timeout_s:g} s"
    elif not parsed.ok and result.returncode not in (0, None):
        tail = (result.stderr or result.stdout)[-400:].strip()
        claude_error = f"{parsed.error or 'failed'} (exit {result.returncode}): {tail}"
    metrics = E2EMetrics(
        executor=arm.executor,
        model_id=model_id,
        fusion=arm.fusion,
        host_cost_usd=host_cost,
        host_cost_reported_usd=parsed.host_cost_usd,
        host_usage=parsed.model_usage,
        fusion_cost_usd=fusion_cost,
        wall_seconds=wall,
        turns=parsed.num_turns,
        tool_calls=parsed.total_tool_calls,
        fusion_calls=fusion_runs,
        fusion_tool_calls=parsed.fusion_tool_calls,
        tests_expected=expected,
        input_tokens=parsed.input_tokens,
        output_tokens=parsed.output_tokens,
        cache_read_tokens=parsed.cache_read_tokens,
        files_changed=changed,
        permission_denials=parsed.permission_denials,
        timed_out=result.timed_out,
        claude_error=redact_error(claude_error) if claude_error else None,
        fusion_connected=parsed.fusion_connected,
        claude_seconds=parsed.duration_ms / 1000 if parsed.duration_ms is not None else None,
    )
    status: ItemStatus = "completed"
    error: str | None = None
    if parsed.result_text == "" and parsed.host_cost_usd is None and not result.timed_out:
        status, error = "error", metrics.claude_error or "Claude Code produced no result"
    elif arm.fusion and parsed.fusion_connected is False:
        status, error = (
            "error",
            "the Fusion MCP server did not connect; the run is not a Fusion run",
        )
    elif claude_error:
        status, error = "halted", metrics.claude_error
    outcome = None
    if status != "error":
        try:
            outcome = run_hidden_tests(task, patch or None, cache=False)
        except (SandboxError, OSError) as exc:  # the run and its cost are kept; a resume rescores
            status, error = "error", redact_error(f"scoring failed: {type(exc).__name__}: {exc}")
    if outcome is None:
        return BenchItem(
            **common,  # type: ignore[arg-type]
            status=status,
            error=error,
            answer=parsed.result_text,
            warnings=warnings,
            metrics=bench_metrics(metrics).model_copy(update={"quality": None}),
            e2e={**metrics.model_dump(mode="json"), "patch": patch[:_MAX_PATCH_CHARS]},
        )
    metrics = metrics.model_copy(update={"tests_passed": len(outcome.passed)})
    return BenchItem(
        **common,  # type: ignore[arg-type]
        status=status,
        error=error,
        answer=parsed.result_text,
        warnings=warnings,
        metrics=bench_metrics(metrics, eval_seconds=outcome.seconds),
        score=ScoreResult(
            quality=outcome.quality, scorer="tests", details=outcome.details(expected)
        ),
        e2e={**metrics.model_dump(mode="json"), "patch": patch[:_MAX_PATCH_CHARS]},
    )
