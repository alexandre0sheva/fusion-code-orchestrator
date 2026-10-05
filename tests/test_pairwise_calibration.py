"""PairwiseJudge (both orderings, ties, cross-family rule, majority) and judge calibration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from _bench import config, sim_env, write_dataset
from _judge import make_task, score_env
from fusion.bench.calibrate import default_mock_judges, forecast_usd, run_calibration
from fusion.bench.cli import bench_app
from fusion.bench.runner import run_bench
from fusion.bench.scoring import (
    AnswerView,
    DebugScorer,
    NoEligibleJudgeError,
    PairwiseJudge,
    PointsScorer,
    ReviewScorer,
    RubricScorer,
    ScoringError,
)
from fusion.bench.scoring.calibration import (
    CalibrationCase,
    build_cases,
    calibrate,
    cohens_kappa,
    load_reports,
    save_report,
    seeded_pair,
)
from fusion.bench.scoring.pairwise import eligible_judges, providers_of
from fusion.bench.spec import BenchTask, load_dataset
from fusion.bench.spend import SpendCapError, SpendLedger
from fusion.bench.virtual import run_virtual

TASK = make_task("architecture", {"required_points": ["x"]})


def prefers_good(kind: str, payload: dict) -> dict:
    """A judge that is right whatever the position: the answer containing GOOD wins."""
    a, b = "GOOD" in payload["a"], "GOOD" in payload["b"]
    return {"winner": "tie" if a == b else "A" if a else "B"}


def always(side: str):
    return lambda kind, payload: {"winner": side}


# -- pairwise ------------------------------------------------------------------------------------


async def test_a_consistent_judge_picks_the_better_answer_in_either_position() -> None:
    env, provider = score_env({"claude-haiku": prefers_good})
    judge = PairwiseJudge()
    won = await judge.compare(TASK, "GOOD answer", "poor answer", env)
    lost = await judge.compare(TASK, "poor answer", "GOOD answer", env)
    assert (won.winner, lost.winner) == ("a", "b")
    assert (won.value, lost.value) == (1.0, 0.0)
    assert len(provider.requests) == 4  # two orderings per comparison


async def test_both_orderings_are_asked_with_the_answers_swapped() -> None:
    env, provider = score_env({"claude-haiku": prefers_good})
    await PairwiseJudge().compare(TASK, "GOOD answer", "poor answer", env)
    first, second = provider.payloads("pairwise")
    assert (first["a"], first["b"]) == ("GOOD answer", "poor answer")
    assert (second["a"], second["b"]) == ("poor answer", "GOOD answer")


async def test_position_bias_cancels_into_a_tie_and_is_flagged() -> None:
    env, _ = score_env({"claude-haiku": always("A")})
    result = await PairwiseJudge().compare(TASK, "GOOD answer", "poor answer", env)
    assert result.winner == "tie"
    (vote,) = result.votes
    assert vote.position_inconsistent is True


async def test_a_judge_that_calls_a_tie_both_times_is_a_tie_not_a_bias() -> None:
    env, _ = score_env({"claude-haiku": always("tie")})
    result = await PairwiseJudge().compare(TASK, "one", "two", env)
    assert result.winner == "tie"
    assert result.votes[0].position_inconsistent is False


async def test_a_failed_ordering_makes_the_judge_abstain() -> None:
    calls = {"n": 0}

    def flaky(kind: str, payload: dict) -> dict | None:
        calls["n"] += 1
        return prefers_good(kind, payload) if calls["n"] == 1 else None

    env, _ = score_env({"claude-haiku": flaky})
    result = await PairwiseJudge().compare(TASK, "GOOD answer", "poor answer", env)
    assert result.winner == "tie"
    assert "failed" in " ".join(env.errors)


async def test_the_majority_of_judges_decides_and_equal_votes_tie() -> None:
    judge = PairwiseJudge()
    env, _ = score_env(
        {"claude-haiku": prefers_good, "gemini-flash": prefers_good, "gpt-luna": always("tie")}
    )
    assert (await judge.compare(TASK, "GOOD x", "poor y", env)).winner == "a"
    bad = lambda k, p: {"winner": "B" if "GOOD" in p["a"] else "A"}  # noqa: E731 — always wrong
    env, _ = score_env({"claude-haiku": prefers_good, "gemini-flash": bad})
    assert (await judge.compare(TASK, "GOOD x", "poor y", env)).winner == "tie"


async def test_judges_that_share_a_provider_with_an_arm_are_excluded() -> None:
    env, provider = score_env({"claude-haiku": prefers_good, "gpt-luna": prefers_good})
    result = await PairwiseJudge().compare(
        TASK, "GOOD x", "poor y", env, exclude_providers={"anthropic"}
    )
    assert result.excluded == ["claude-haiku"]
    assert [v.judge for v in result.votes] == ["gpt-luna"]
    assert {r.model_id for r in provider.requests} == {"gpt-6-luna"}


async def test_with_every_judge_excluded_the_comparison_refuses() -> None:
    env, _ = score_env({"claude-haiku": prefers_good})
    with pytest.raises(NoEligibleJudgeError, match="shares a provider"):
        await PairwiseJudge().compare(TASK, "a", "b", env, exclude_providers={"anthropic"})
    allowed = await PairwiseJudge(cross_family=False).compare(
        TASK, "GOOD a", "b", env, exclude_providers={"anthropic"}
    )
    assert allowed.winner == "a"
    empty, _ = score_env({})
    with pytest.raises(NoEligibleJudgeError, match="no judge models"):
        await PairwiseJudge().compare(TASK, "a", "b", empty)


def test_arm_providers_come_from_the_catalog() -> None:
    env, _ = score_env({"claude-haiku": prefers_good})
    models = env.gateway.models
    assert providers_of(["claude-haiku", "gpt-luna", "unknown"], models) == {"anthropic", "openai"}
    usable, removed = eligible_judges(["claude-haiku", "gpt-luna"], models, {"openai"})
    assert (usable, removed) == (["claude-haiku"], ["gpt-luna"])


async def test_judging_costs_are_the_scorers_and_reported() -> None:
    env, _ = score_env({"claude-haiku": prefers_good})
    result = await PairwiseJudge().compare(TASK, "GOOD a", "b", env)
    assert result.cost_usd == pytest.approx(0.004)


# -- kappa ---------------------------------------------------------------------------------------


def test_kappa_of_perfect_chance_and_textbook_agreement() -> None:
    assert cohens_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    assert cohens_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == pytest.approx(0.0)
    # 20 yes/yes, 5 yes/no, 10 no/yes, 15 no/no: po 0.7, pe 0.5
    first = ["y"] * 25 + ["n"] * 25
    second = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert cohens_kappa(first, second) == pytest.approx(0.4)
    assert cohens_kappa(["a", "a", "b", "b"], ["b", "b", "a", "a"]) == pytest.approx(-1.0)


def test_kappa_when_chance_agreement_is_total() -> None:
    assert cohens_kappa(["a", "a"], ["a", "a"]) == 1.0
    assert cohens_kappa(["a", "a"], ["b", "b"]) == 0.0
    assert cohens_kappa([], []) == 0.0
    with pytest.raises(ValueError, match="same items"):
        cohens_kappa(["a"], [])


# -- seeded pairs --------------------------------------------------------------------------------

POINTS = make_task(
    "code_review",
    {
        "points": [
            {"id": f"p{n}", "keywords": [f"alpha{n}"], "text": f"alpha{n} is broken"}
            for n in range(3)
        ],
        "decoys": [{"id": "d", "keywords": ["bogus"], "text": "bogus is the cause"}],
    },
    id="pts",
    prompt="Review the points task before release please.",
)
BUGS = make_task(
    "code_review",
    {
        "bugs": [
            {
                "file": "a.py",
                "line": 5,
                "category": "sql-injection",
                "severity": "high",
                "description": "query built from user input",
            }
        ]
    },
    id="bugs",
    prompt="Review the bugs task before release please.",
    files={"a.py": "\n".join("x" for _ in range(30))},
)
DEBUG = make_task(
    "debugging",
    {
        "root_cause_tags": ["race-condition"],
        "root_cause": "two threads update the counter",
        "fix_keywords": ["mutex"],
    },
    id="dbg",
    prompt="Debug the failing counter task please.",
)
RUBRIC = make_task(
    "architecture",
    {
        "required_points": [
            {"id": "r1", "text": "choose postgres", "keywords": ["postgres"]},
            {"id": "r2", "text": "bound the retries", "keywords": ["retries"]},
        ],
        "forbidden_points": [{"id": "f1", "text": "rewrite everything", "keywords": ["rewrite"]}],
    },
    id="rub",
    prompt="Design the storage architecture task please.",
)
CLEAN = make_task(
    "code_review", {"bugs": []}, id="clean", prompt="Review the clean change task please."
)


async def test_a_seeded_good_answer_beats_its_flawed_twin_under_the_deterministic_scorer() -> None:
    env, _ = score_env()
    for task, scorer in (
        (POINTS, PointsScorer()),
        (BUGS, ReviewScorer()),
        (DEBUG, DebugScorer()),
        (RUBRIC, RubricScorer()),
        (CLEAN, ReviewScorer()),
    ):
        pair = seeded_pair(task)
        assert pair is not None, task.id
        good, flawed = [
            (await scorer.score(task, AnswerView(final_answer=t), env)).quality for t in pair
        ]
        assert good >= 0.9, task.id
        assert flawed <= 0.5, (task.id, flawed)


def test_a_task_without_known_truth_has_no_seeded_pair() -> None:
    other = make_task("coding", {"tests": "x"})
    assert seeded_pair(other) is None
    assert [c.task_id for c in build_cases([other, POINTS])] == ["pts"]


# -- calibration ---------------------------------------------------------------------------------

TASKS = [POINTS, BUGS, DEBUG, RUBRIC, CLEAN]


async def run(replies, tasks: list[BenchTask] = TASKS):
    env, provider = score_env(replies)
    report = await calibrate(tasks, build_cases(tasks), env, list(replies))
    return report, provider


def oracle(kind: str, payload: dict) -> dict:
    """Knows a seeded good answer by the words only it has, and a flawed one by its decoys."""
    marks = ("is broken", "Root cause", "choose postgres", "No issues found", "sql-injection")
    wrong = ("bogus", "rewrite everything", "nonexistent/", "style:", "network is probably")

    def goodness(text: str) -> int:
        return sum(text.count(m) for m in marks) - sum(text.count(w) for w in wrong)

    a, b = goodness(payload["a"]), goodness(payload["b"])
    return {"winner": "tie" if a == b else "A" if a > b else "B"}


async def test_a_judge_that_is_right_has_full_accuracy_and_kappa() -> None:
    report, provider = await run({"claude-haiku": oracle})
    (stats,) = report.judges
    assert (stats.accuracy, stats.kappa) == (1.0, 1.0)
    assert (stats.tie_rate, stats.inconsistent_rate, stats.failed_calls) == (0.0, 0.0, 0)
    assert report.cases == len(TASKS)
    assert report.agreement is None  # one judge: nothing to agree with
    assert len(provider.requests) == 2 * len(TASKS)
    assert report.cost_usd == pytest.approx(0.002 * 2 * len(TASKS))


async def test_a_judge_that_always_says_a_has_no_kappa_however_often_it_is_right_once() -> None:
    report, _ = await run({"claude-haiku": always("A")})
    (stats,) = report.judges
    assert stats.kappa == pytest.approx(0.0)
    assert stats.accuracy == 0.0
    assert stats.inconsistent_rate == 1.0


async def test_judges_are_compared_with_each_other() -> None:
    report, _ = await run({"claude-haiku": oracle, "gemini-flash": oracle, "gpt-luna": always("A")})
    assert report.agreement == pytest.approx((1.0 + 0.0 + 0.0) / 3)
    assert report.pair_kappa["claude-haiku|gemini-flash"] == 1.0
    assert report.pair_kappa["claude-haiku|gpt-luna"] == pytest.approx(0.0)


async def test_failed_calls_are_counted_not_hidden() -> None:
    report, _ = await run({"claude-haiku": lambda k, p: None})
    (stats,) = report.judges
    assert stats.failed_calls == 2 * len(TASKS)
    assert stats.accuracy == 0.0


async def test_calibration_needs_judges_cases_and_known_tasks() -> None:
    env, _ = score_env({"claude-haiku": oracle})
    with pytest.raises(ScoringError, match="at least one judge"):
        await calibrate(TASKS, build_cases(TASKS), env, [])
    with pytest.raises(ScoringError, match="no cases"):
        await calibrate(TASKS, [], env, ["claude-haiku"])
    with pytest.raises(ScoringError, match="unknown tasks"):
        await calibrate(TASKS, [CalibrationCase(task_id="zzz", good="a", flawed="b")], env, ["x"])


async def test_reports_are_stored_and_read_back(tmp_path: Path) -> None:
    report, _ = await run({"claude-haiku": oracle})
    first = save_report(report, tmp_path)
    second = save_report(report, tmp_path)  # the same second: must not overwrite
    assert first != second
    assert [r.judges[0].kappa for r in load_reports(tmp_path)] == [1.0, 1.0]
    assert load_reports(tmp_path / "nowhere") == []


# -- simulated judges, the planner, the runner and the CLI -----------------------------------------


def toy(tmp_path: Path) -> tuple[Path, list[BenchTask]]:
    path = write_dataset(tmp_path / "toy.jsonl", count=6)
    return path, load_dataset(path)


def test_simulated_judges_calibrate_offline_and_identically_on_a_rerun(tmp_path: Path) -> None:
    path, tasks = toy(tmp_path)
    cfg = config(path)

    def once() -> dict:
        env = sim_env(cfg, tmp_path / "r")
        judges = default_mock_judges(env)
        report = run_virtual(run_calibration(env, tasks, build_cases(tasks), judges, mock=True))
        data = report.model_dump()
        data.pop("id"), data.pop("created")
        return data

    first = once()
    assert first == once()
    assert first["mock"] is True
    assert len(first["judges"]) == 3
    assert all(0.0 <= j["accuracy"] <= 1.0 for j in first["judges"])
    assert first["cost_usd"] > 0  # simulated models are priced like the real ones


def test_a_live_calibration_obeys_max_usd_and_the_spend_cap(tmp_path: Path) -> None:
    path, tasks = toy(tmp_path)
    cfg = config(path)
    ledger = SpendLedger(tmp_path / "spend.json")
    env = sim_env(cfg, tmp_path / "r", spend=ledger)  # a spend ledger makes it behave as live
    cases = build_cases(tasks)
    judges = ["claude-haiku"]
    estimate = forecast_usd(env, tasks, cases, judges)
    assert estimate > 0

    with pytest.raises(ScoringError, match="needs --max-usd"):
        run_virtual(run_calibration(env, tasks, cases, judges))
    with pytest.raises(ScoringError, match="over --max-usd"):
        run_virtual(run_calibration(env, tasks, cases, judges, max_usd=estimate / 10))
    assert ledger.total() == 0.0  # refused before any call

    tight = SpendLedger(tmp_path / "tight.json", cap_usd=estimate / 10)
    with pytest.raises(SpendCapError):
        run_virtual(
            run_calibration(
                sim_env(cfg, tmp_path / "r2", spend=tight), tasks, cases, judges, max_usd=1.0
            )
        )

    report = run_virtual(run_calibration(env, tasks, cases, judges, max_usd=1.0))
    assert ledger.total() == pytest.approx(report.cost_usd)
    entry = ledger.entries()[0]
    assert entry["task"] == "calibration"


def test_a_study_scores_a_review_dataset_and_prices_its_judge(tmp_path: Path) -> None:
    rows = [
        {
            "id": f"r{n}",
            "category": "code_review",
            "prompt": f"Review change number {n} to the payments module before release.",
            "files": {"pay.py": "\n".join("x" for _ in range(40))},
            "truth": {
                "bugs": [
                    {
                        "file": "pay.py",
                        "line": 10 + n,
                        "category": "rounding",
                        "description": f"amount rounding drops cents for plan {n}",
                    }
                ]
            },
        }
        for n in range(3)
    ]
    path = tmp_path / "review.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    cfg = config(path, repeats=1, judge_models=["claude-haiku"])
    env = sim_env(cfg, tmp_path / "r")
    run = run_virtual(run_bench(cfg, env=env))
    assert run.status == "completed"
    assert {i.score.scorer for i in run.items if i.score} == {"review"}
    assert all(i.metrics.quality is not None for i in run.items)

    from fusion.bench.plan import estimate_job
    from fusion.bench.scoring import get_scorer

    def eval_usd(judges: list[str]) -> float:
        from fusion.bench.arms import arm_book
        from fusion.routing.policy import RoutingPolicy

        task = load_dataset(path)[0]
        book = arm_book(env.book, cfg.arms)
        routing = RoutingPolicy(env.routing_config, registry=env.registry, strategies=book)
        return estimate_job(
            task,
            book.get(cfg.arms[0].name),
            routing=routing,
            registry=env.registry,
            pricing=env.pricing,
            scorer=get_scorer(task.category),
            judge_models=judges,
        ).eval_usd

    assert eval_usd([]) == 0.0
    assert eval_usd(["claude-haiku"]) > 0.0


def test_bad_truth_fails_when_the_dataset_loads() -> None:
    with pytest.raises(ValueError, match="line"):
        make_task(
            "code_review",
            {"bugs": [{"file": "a.py", "line": 0, "category": "x", "description": "d"}]},
        )
    with pytest.raises(ValueError, match="root_cause_tags"):
        make_task("debugging", {"root_cause_tags": []})


def test_the_calibrate_judge_command_runs_offline_and_stores_its_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = toy(tmp_path)
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path / "results"))
    result = CliRunner().invoke(bench_app, ["calibrate-judge", "--dataset", str(path), "--mock"])
    assert result.exit_code == 0, result.output
    assert "accuracy" in result.output
    assert "kappa" in result.output
    assert len(load_reports(tmp_path / "results")) == 1

    as_json = CliRunner().invoke(
        bench_app,
        [
            "calibrate-judge",
            "--dataset",
            str(path),
            "--mock",
            "--json",
            "--judge-models",
            "gpt-luna",
        ],
    )
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.output)
    assert [j["judge"] for j in data["judges"]] == ["gpt-luna"]
    assert data["mock"] is True


def test_the_calibrate_judge_command_asks_for_a_dataset_and_judges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path / "results"))
    assert CliRunner().invoke(bench_app, ["calibrate-judge"]).exit_code == 2
    path, _ = toy(tmp_path)
    result = CliRunner().invoke(
        bench_app, ["calibrate-judge", "--dataset", str(path), "--mock", "--judge-models", "nope"]
    )
    assert result.exit_code == 2
    assert "not in the catalog" in result.output
