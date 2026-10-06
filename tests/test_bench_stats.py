"""Study statistics: intervals that cover, paired comparisons, the frontier, verdict rules."""

from __future__ import annotations

import pytest

from _stats import item, two_arms
from fusion.bench.stats import (
    Comparison,
    Interval,
    PairedQuality,
    Rules,
    bootstrap_interval,
    build_cells,
    compare_arms,
    pareto_frontier,
    percentile,
    sign_test_p,
    study_stats,
    verdicts,
)

FAST = Rules(n_boot=400)


# ------------------------------------------------------------------------------ small pieces


def test_sign_test_matches_the_exact_binomial() -> None:
    assert sign_test_p(8, 2) == pytest.approx(0.109375)
    assert sign_test_p(5, 5) == 1.0
    assert sign_test_p(10, 0) == pytest.approx(2 / 1024)
    assert sign_test_p(0, 0) is None  # nothing differed: no test


def test_percentile_is_nearest_rank() -> None:
    values = [float(n) for n in range(1, 11)]
    assert percentile(values, 0.5) == 5.0
    assert percentile(values, 0.9) == 9.0
    assert percentile([], 0.5) is None


def test_bootstrap_is_deterministic_and_brackets_the_mean() -> None:
    values = [0.2, 0.4, 0.5, 0.6, 0.9, 0.7, 0.3, 0.55]

    def mean(xs: list[float] | tuple[float, ...]) -> float:
        return sum(xs) / len(xs)

    a = bootstrap_interval(values, mean, key="k", seed=3, n_boot=500)
    b = bootstrap_interval(values, mean, key="k", seed=3, n_boot=500)
    assert a == b and a is not None
    assert a.low is not None and a.high is not None
    assert a.low < a.estimate < a.high and a.n == 8
    other = bootstrap_interval(values, mean, key="other", seed=3, n_boot=500)
    assert other != a  # the key decorrelates intervals of different statistics


def test_bootstrap_of_one_task_has_an_estimate_and_no_bounds() -> None:
    interval = bootstrap_interval([0.7], lambda xs: sum(xs) / len(xs), key="k")
    assert interval is not None and interval.estimate == 0.7
    assert interval.low is None and interval.high is None


def test_a_statistic_undefined_on_the_data_gives_no_interval() -> None:
    assert bootstrap_interval([1.0, 2.0], lambda xs: None, key="k") is None


# ---------------------------------------------------------------- what the intervals mean


def test_repeats_of_a_task_do_not_narrow_the_interval() -> None:
    """Resampling is by task: five copies of every task's runs say nothing more than one."""
    one = two_arms(30, noise=0.0, repeats=1)
    five = [
        item(
            i.arm,
            i.task_id,
            i.metrics.quality,
            repeat=r,
            cost=i.metrics.cost_usd,
            seconds=i.metrics.seconds_to_complete,
        )  # fmt: skip
        for i in one
        for r in range(1, 6)
    ]
    narrow = study_stats(five, rules=FAST).arms[0].mean_quality
    base = study_stats(one, rules=FAST).arms[0].mean_quality
    assert narrow is not None and base is not None
    assert narrow.low == pytest.approx(base.low) and narrow.high == pytest.approx(base.high)


def test_the_interval_covers_the_true_effect_about_as_often_as_promised() -> None:
    truth, covered, runs = 0.08, 0, 120
    for seed in range(runs):
        items = two_arms(30, effect=truth, noise=0.12, repeats=1, seed=seed)
        cells = build_cells(items)
        cmp = compare_arms(
            "fusion", "solo", cells["fusion"], cells["solo"], rules=Rules(n_boot=300)
        )
        d = cmp.quality.difference
        assert d is not None and d.low is not None and d.high is not None
        covered += d.low <= truth <= d.high
    assert covered / runs >= 0.88  # nominally 95%; the bound leaves room for sampling noise


def test_a_known_effect_is_recovered_and_called_better() -> None:
    stats = study_stats(two_arms(40, effect=0.15, noise=0.08), baseline="solo", rules=FAST)
    cmp = next(c for c in stats.comparisons if c.scope == "all")
    d = cmp.quality.difference
    assert d is not None and d.low is not None
    assert d.estimate == pytest.approx(0.15, abs=0.04) and d.low > 0
    assert cmp.quality.wins > cmp.quality.losses and cmp.quality.sign_test_p is not None
    assert cmp.quality.sign_test_p < 0.01
    assert cmp.quality.effect_size is not None and cmp.quality.effect_size > 1
    outcome = {v.claim: v.outcome for v in cmp.verdicts}
    assert outcome == {"cheaper": "yes", "faster": "yes", "not_worse": "yes", "better": "yes"}


def test_a_null_effect_is_not_called_better_or_worse() -> None:
    stats = study_stats(two_arms(60, effect=0.0, noise=0.05), baseline="solo", rules=FAST)
    cmp = next(c for c in stats.comparisons if c.scope == "all")
    outcome = {v.claim: v.outcome for v in cmp.verdicts}
    assert outcome["better"] == "inconclusive"
    assert outcome["not_worse"] == "yes"  # equal within the margin, and cheaper and faster
    d = cmp.quality.difference
    assert d is not None and d.low is not None and d.high is not None and d.low < 0 < d.high


def test_a_clearly_worse_arm_is_not_better_and_not_not_worse() -> None:
    stats = study_stats(two_arms(40, effect=-0.2, noise=0.05), baseline="solo", rules=FAST)
    cmp = next(c for c in stats.comparisons if c.scope == "all")
    outcome = {v.claim: v.outcome for v in cmp.verdicts}
    assert outcome["better"] == "no" and outcome["not_worse"] == "no"


def test_a_noisy_small_study_is_inconclusive() -> None:
    stats = study_stats(two_arms(12, effect=0.05, noise=0.3), baseline="solo", rules=FAST)
    cmp = next(c for c in stats.comparisons if c.scope == "all")
    outcome = {v.claim: v.outcome for v in cmp.verdicts}
    assert outcome["better"] == "inconclusive"


# ------------------------------------------------------------------------ the money numbers


def test_cost_per_solved_is_total_cost_over_solved_items() -> None:
    items = [
        item("a", "t1", 0.9, cost=0.10),
        item("a", "t2", 0.2, cost=0.30),  # spent and not solved: still part of the total
        item("a", "t3", 0.8, cost=0.20),
        item("a", "t4", None, cost=0.40, status="error"),  # an error's money counts too
    ]
    arm = study_stats(items, rules=FAST).arms[0]
    assert arm.solved == 2 and arm.scored_items == 3
    assert arm.cost_total_usd == pytest.approx(1.0)
    assert arm.cost_per_solved is not None and arm.cost_per_solved.estimate == pytest.approx(0.5)
    assert arm.cost_per_task is not None and arm.cost_per_task.estimate == pytest.approx(0.25)
    assert arm.errors == 1
    assert arm.tokens_per_solved == pytest.approx(4 * 1500 / 2)
    assert arm.quality_per_dollar == pytest.approx(
        arm.mean_quality.estimate / 0.25 if arm.mean_quality else 0
    )


def test_an_arm_that_solves_nothing_has_no_cost_per_solved() -> None:
    items = [item("a", f"t{n}", 0.1) for n in range(5)]
    arm = study_stats(items, rules=FAST).arms[0]
    assert arm.solved == 0 and arm.cost_per_solved is None and arm.tokens_per_solved is None


def test_latency_leaves_out_runs_replayed_from_the_cache() -> None:
    items = [
        item("a", "t1", 0.9, seconds=10.0),
        item("a", "t2", 0.9, seconds=20.0),
        item("a", "t3", 0.9, seconds=0.1, latency_valid=False, cache_hits=3),
    ]
    arm = study_stats(items, rules=FAST).arms[0]
    assert arm.timed_items == 2 and arm.seconds_p50 == 10.0 and arm.seconds_p90 == 20.0
    assert arm.cache_hits == 3


def test_decode_speed_and_first_token_time_are_averaged_per_model() -> None:
    items = [
        item("a", "t1", 0.9, decode_tokens_per_s={"m": 100.0}, ttft_ms={"m": 300.0}),
        item("a", "t2", 0.9, decode_tokens_per_s={"m": 200.0}, ttft_ms={"m": 500.0}),
    ]
    arm = study_stats(items, rules=FAST).arms[0]
    assert arm.decode_tokens_per_s == {"m": 150.0} and arm.ttft_ms == {"m": 400.0}


def test_eval_cost_is_reported_beside_the_arm_and_never_inside_it() -> None:
    items = [
        item("a", f"t{n}", 0.9, cost=0.1, eval_cost_usd=0.5, eval_seconds=4.0) for n in range(4)
    ]
    arm = study_stats(items, rules=FAST).arms[0]
    assert arm.cost_total_usd == pytest.approx(0.4)
    assert arm.eval_cost_usd == pytest.approx(2.0) and arm.eval_cost_per_item == pytest.approx(0.5)
    assert arm.eval_seconds_per_item == pytest.approx(4.0)
    assert arm.seconds_mean == pytest.approx(10.0)  # eval time is not part of the arm's latency


def test_variance_across_repeats_and_flipping_tasks() -> None:
    items = [
        item("a", "t1", 0.9, repeat=1),
        item("a", "t1", 0.3, repeat=2),  # solved once, not the other time
        item("a", "t2", 0.8, repeat=1),
        item("a", "t2", 0.8, repeat=2),
    ]
    arm = study_stats(items, rules=FAST).arms[0]
    assert arm.flip_rate == 0.5
    assert arm.repeat_sd == pytest.approx((0.6 / 2**0.5 + 0.0) / 2)


def test_quality_per_minute_uses_the_arms_own_time() -> None:
    items = [item("a", f"t{n}", 0.6, seconds=60.0) for n in range(4)]
    arm = study_stats(items, rules=FAST).arms[0]
    assert arm.quality_per_minute == pytest.approx(0.6)


# ------------------------------------------------------------------------------ the frontier


def test_pareto_frontier_keeps_what_nothing_beats() -> None:
    points = {
        "cheap-bad": (1.0, 0.4),
        "mid": (2.0, 0.7),
        "dominated": (3.0, 0.6),  # costs more than mid and is worse
        "expensive-best": (5.0, 0.9),
    }
    assert pareto_frontier(points) == ["cheap-bad", "mid", "expensive-best"]


def test_pareto_frontier_keeps_ties_and_a_lone_point() -> None:
    assert pareto_frontier({"a": (1.0, 0.5), "b": (1.0, 0.5)}) == ["a", "b"]
    assert pareto_frontier({"only": (1.0, 0.5)}) == ["only"]
    assert pareto_frontier({}) == []


def test_same_cost_better_quality_dominates() -> None:
    assert pareto_frontier({"a": (1.0, 0.5), "b": (1.0, 0.6)}) == ["b"]


def test_the_study_has_a_cost_and_a_latency_frontier() -> None:
    items = [item("slow-good", f"t{n}", 0.9, cost=0.5, seconds=30.0) for n in range(3)]
    items += [item("fast-ok", f"t{n}", 0.7, cost=0.1, seconds=5.0) for n in range(3)]
    items += [item("bad", f"t{n}", 0.5, cost=0.6, seconds=40.0) for n in range(3)]
    stats = study_stats(items, rules=FAST)
    assert stats.pareto_cost == ["fast-ok", "slow-good"]
    assert stats.pareto_latency == ["fast-ok", "slow-good"]


# -------------------------------------------------------------------------- verdict boundaries


def _cmp(low: float, high: float, *, n: int = 30, estimate: float | None = None) -> Comparison:
    est = (low + high) / 2 if estimate is None else estimate
    ratio = Interval(estimate=0.5, low=0.4, high=0.6, n=n)
    return Comparison(
        challenger="f",
        baseline="s",
        scope="all",
        n_tasks=n,
        quality=PairedQuality(
            n_tasks=n, difference=Interval(estimate=est, low=low, high=high, n=n)
        ),
        cost_per_solved_ratio=ratio,
        cost_per_task_ratio=ratio,
        seconds_ratio=ratio,
    )


def _outcomes(cmp: Comparison, rules: Rules | None = None) -> dict[str, str]:
    return {v.claim: v.outcome for v in verdicts(cmp, rules or Rules())}


def test_not_worse_needs_the_lower_bound_strictly_above_minus_margin() -> None:
    assert _outcomes(_cmp(-0.029, 0.05))["not_worse"] == "yes"
    assert _outcomes(_cmp(-0.03, 0.05))["not_worse"] == "inconclusive"  # on the margin: not above
    assert _outcomes(_cmp(-0.08, 0.02))["not_worse"] == "inconclusive"
    assert _outcomes(_cmp(-0.10, -0.031))["not_worse"] == "no"
    assert _outcomes(_cmp(-0.10, -0.03))["not_worse"] == "inconclusive"


def test_the_margin_is_configurable() -> None:
    cmp = _cmp(-0.06, 0.04)
    assert _outcomes(cmp, Rules(margin=0.03))["not_worse"] == "inconclusive"
    assert _outcomes(cmp, Rules(margin=0.10))["not_worse"] == "yes"


def test_better_needs_the_interval_wholly_above_zero() -> None:
    assert _outcomes(_cmp(0.001, 0.2))["better"] == "yes"
    assert _outcomes(_cmp(0.0, 0.2))["better"] == "inconclusive"
    assert _outcomes(_cmp(-0.1, 0.0))["better"] == "no"
    assert _outcomes(_cmp(-0.1, 0.001))["better"] == "inconclusive"


def test_cheaper_and_faster_need_the_ratio_interval_wholly_below_one() -> None:
    def with_ratio(low: float, high: float) -> Comparison:
        cmp = _cmp(0.0, 0.1)
        ratio = Interval(estimate=(low + high) / 2, low=low, high=high, n=30)
        cmp.cost_per_solved_ratio = ratio
        cmp.seconds_ratio = ratio
        return cmp

    assert _outcomes(with_ratio(0.5, 0.99))["cheaper"] == "yes"
    assert _outcomes(with_ratio(0.5, 1.0))["cheaper"] == "inconclusive"
    assert _outcomes(with_ratio(1.0, 1.5))["cheaper"] == "no"
    assert _outcomes(with_ratio(0.9, 1.2))["faster"] == "inconclusive"
    assert _outcomes(with_ratio(1.2, 2.0))["faster"] == "no"


def test_a_ratio_that_is_undefined_is_inconclusive_with_the_reason() -> None:
    cmp = _cmp(0.0, 0.1)
    cmp.cost_per_solved_ratio = None
    verdict = next(v for v in verdicts(cmp, Rules()) if v.claim == "cheaper")
    assert verdict.outcome == "inconclusive" and "solved no task" in verdict.reason


def test_too_few_tasks_make_every_claim_inconclusive_and_say_why() -> None:
    result = verdicts(_cmp(0.1, 0.2, n=9), Rules(min_tasks=10))
    assert {v.outcome for v in result} == {"inconclusive"}
    assert "only 9 task" in result[0].reason and "10" in result[0].reason
    assert {v.outcome for v in verdicts(_cmp(0.1, 0.2, n=10), Rules(min_tasks=10))} != {
        "inconclusive"
    }


def test_blocked_verdicts_carry_the_reason() -> None:
    result = verdicts(_cmp(0.1, 0.2), Rules(), blocked="judges not calibrated")
    assert {v.outcome for v in result} == {"blocked"}
    assert all("not calibrated" in v.reason for v in result)


def test_every_verdict_states_why_with_the_numbers() -> None:
    for v in verdicts(_cmp(0.01, 0.1), Rules()):
        assert v.reason and any(ch.isdigit() for ch in v.reason)


# ----------------------------------------------------------------------- scopes and structure


def test_comparisons_cover_each_arm_against_the_baseline_overall_and_per_category() -> None:
    items = two_arms(12, category="code_review") + two_arms(
        12, category="debugging", seed=2, prefix="d"
    )
    stats = study_stats(items, baseline="solo", rules=FAST)
    assert stats.baseline == "solo" and [a.arm for a in stats.arms] == ["solo", "fusion"]
    scopes = {c.scope for c in stats.comparisons}
    assert scopes == {"all", "code_review", "debugging"}
    assert all(c.challenger == "fusion" and c.baseline == "solo" for c in stats.comparisons)
    assert set(stats.by_category) == {"code_review", "debugging"}


def test_difficulty_breakdown_needs_the_task_difficulties() -> None:
    items = two_arms(10)
    assert study_stats(items, rules=FAST).by_difficulty == {}
    difficulties = {f"t{n:03d}": ("easy" if n < 5 else "hard") for n in range(10)}
    stats = study_stats(items, difficulties=difficulties, rules=FAST)
    assert set(stats.by_difficulty) == {"easy", "hard"}
    assert {a.n_tasks for a in stats.by_difficulty["easy"]} == {5}


def test_blocked_categories_withhold_only_their_own_verdicts() -> None:
    items = two_arms(12, category="code_review") + two_arms(
        12, category="frontend", seed=2, prefix="f"
    )
    stats = study_stats(
        items,
        baseline="solo",
        rules=FAST,
        blocked_categories=frozenset({"frontend"}),
    )
    by_scope = {c.scope: {v.outcome for v in c.verdicts} for c in stats.comparisons}
    assert by_scope["frontend"] == {"blocked"}
    assert "blocked" not in by_scope["code_review"]


def test_a_blocked_study_has_no_headline_verdict() -> None:
    stats = study_stats(two_arms(12), baseline="solo", rules=FAST, blocked="judges unproven")
    assert {v.outcome for c in stats.comparisons for v in c.verdicts} == {"blocked"}
    assert stats.blocked == "judges unproven"


def test_the_default_baseline_is_the_frontier_solo_arm_when_there_is_one() -> None:
    items = [item(a, f"t{n}", 0.7) for a in ("panel", "solo-frontier", "x") for n in range(3)]
    assert study_stats(items, rules=FAST).baseline == "solo-frontier"
    items = [item(a, f"t{n}", 0.7) for a in ("panel", "x") for n in range(3)]
    assert study_stats(items, rules=FAST).baseline == "panel"


def test_an_unknown_baseline_or_an_empty_run_is_an_error() -> None:
    with pytest.raises(ValueError, match="not in the run"):
        study_stats(two_arms(4), baseline="nope", rules=FAST)
    with pytest.raises(ValueError, match="no items"):
        study_stats([], rules=FAST)


def test_win_tie_loss_uses_the_tie_band() -> None:
    items = []
    for n, (a, b) in enumerate([(0.9, 0.5), (0.5, 0.9), (0.70, 0.71), (0.8, 0.5)]):
        items += [item("f", f"t{n}", a), item("s", f"t{n}", b)]
    cmp = compare_arms(
        "f",
        "s",
        build_cells(items)["f"],
        build_cells(items)["s"],
        rules=Rules(n_boot=50, tie=0.02),
    )
    assert (cmp.quality.wins, cmp.quality.ties, cmp.quality.losses) == (2, 1, 1)


def test_only_tasks_both_arms_were_scored_on_are_paired() -> None:
    items = [
        item("f", "t1", 0.9),
        item("s", "t1", 0.5),
        item("f", "t2", 0.9),
        item("s", "t2", None),
    ]
    cells = build_cells(items)
    assert compare_arms("f", "s", cells["f"], cells["s"], rules=Rules(n_boot=50)).n_tasks == 1
