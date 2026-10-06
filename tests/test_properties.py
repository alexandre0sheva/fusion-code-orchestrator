"""Property tests: invariants that must hold for any input, not just the examples we thought of.

Hypothesis generates the inputs and shrinks a failure to the smallest one. The areas are the ones
where a wrong number or a leaked secret is silent: claim clustering and agreement, cost arithmetic,
the statistics behind the benchmark verdicts, redaction and output cleaning.
"""

from __future__ import annotations

import math
import string
from datetime import date

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fusion.bench.stats import pareto_frontier, percentile, sign_test_p
from fusion.config.catalog import PriceSchedule
from fusion.orchestration.claims import (
    AgreementReport,
    Claim,
    PanelAnswer,
    agreement_score,
    calibrated_confidence,
    cluster_claims,
)
from fusion.orchestration.ledger import CallRecord, RunLedger
from fusion.security.output import strip_control
from fusion.security.redaction import EntropyConfig, redact_secrets
from fusion.telemetry.cost import _token_cost

FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# ------------------------------------------------------------------- claims and agreement

WORDS = ["lock", "race", "counter", "retry", "backoff", "sql", "query", "cache", "timeout", "leak"]
KINDS = ["finding", "hypothesis", "recommendation", "risk", "test"]
SEVERITIES = [None, "low", "med", "high", "critical"]
MODELS = ["m1", "m2", "m3", "m4"]

claims = st.builds(
    Claim,
    text=st.lists(st.sampled_from(WORDS), min_size=1, max_size=8).map(" ".join),
    kind=st.sampled_from(KINDS),
    severity=st.sampled_from(SEVERITIES),
    file=st.sampled_from([None, "a.py", "b.py"]),
    line=st.one_of(st.none(), st.integers(1, 60)),
    evidence=st.one_of(st.none(), st.text(max_size=12)),
)
answers = st.builds(PanelAnswer, claims=st.lists(claims, max_size=6))
panels = st.dictionaries(st.sampled_from(MODELS), answers, max_size=4)


@FAST
@given(panels)
def test_clustering_neither_loses_nor_invents_a_claim(panel: dict[str, PanelAnswer]) -> None:
    clusters = cluster_claims(panel)
    assert sum(len(c.members) for c in clusters) == sum(len(a.claims) for a in panel.values())


@FAST
@given(panels)
def test_a_cluster_holds_at_most_one_claim_per_model(panel: dict[str, PanelAnswer]) -> None:
    for cluster in cluster_claims(panel):
        models = [m.model for m in cluster.members]
        assert len(models) == len(set(models))
        assert cluster.models == sorted(set(models))
        assert cluster.support == len(models) >= 1


@FAST
@given(panels)
def test_cluster_ids_are_sequential_and_unique(panel: dict[str, PanelAnswer]) -> None:
    ids = [c.id for c in cluster_claims(panel)]
    assert ids == [f"C{i}" for i in range(1, len(ids) + 1)]


@FAST
@given(panels)
def test_clustering_does_not_depend_on_the_order_models_are_listed(
    panel: dict[str, PanelAnswer],
) -> None:
    forward = cluster_claims(panel)
    backward = cluster_claims(dict(reversed(list(panel.items()))))
    assert [c.model_dump() for c in forward] == [c.model_dump() for c in backward]


@FAST
@given(panels, st.integers(0, 3))
def test_agreement_is_bounded_and_partitions_the_clusters(
    panel: dict[str, PanelAnswer], missing: int
) -> None:
    clusters = cluster_claims(panel)
    n = len(panel)
    report = agreement_score(clusters, n, n_requested=n + missing)
    assert 0.0 <= report.score <= 1.0
    assert 0.0 <= report.confidence <= 0.95
    assert 0.0 <= report.evidence_rate <= 1.0
    assert 0.0 <= report.coverage <= 1.0
    assert report.low_information == (n < 2)
    shared = [report.consensus, report.unique, report.contradicted]
    flat = [cid for ids in shared for cid in ids]
    assert len(flat) == len(set(flat)) and set(flat) <= {c.id for c in clusters}
    partial = [c for c in clusters if c.status == "partial"]
    assert len(flat) + len(partial) == len(clusters)
    assert set(report.unique) == {c.id for c in clusters if c.support < 2}
    if report.low_information:
        assert report.score == 0.0 and report.confidence <= 0.5
    if n < 3:
        assert report.outliers == []
    assert set(report.outliers) <= set(panel)


@FAST
@given(
    st.lists(
        st.lists(st.sampled_from(WORDS), min_size=1, max_size=6).map(" ".join),
        min_size=1,
        max_size=5,
        unique=True,
    ),
    st.integers(2, 4),
)
def test_models_that_say_exactly_the_same_thing_fully_agree(texts: list[str], n: int) -> None:
    panel = {
        f"m{i}": PanelAnswer(claims=[Claim(text=t, kind="finding", severity="med") for t in texts])
        for i in range(n)
    }
    clusters = cluster_claims(panel)
    report = agreement_score(clusters, n)
    assert all(c.support == n for c in clusters)
    assert report.score == pytest.approx(1.0)


unit = st.floats(0.0, 1.0, allow_nan=False)


def report_with(score: float, evidence: float, **over: object) -> AgreementReport:
    base = {
        "n_models": 3,
        "n_requested": 3,
        "n_clusters": 4,
        "score": score,
        "evidence_rate": evidence,
        "coverage": 1.0,
        "structured_share": 1.0,
        "low_information": False,
    }
    return AgreementReport(**{**base, **over})  # type: ignore[arg-type]


@FAST
@given(unit, unit, unit)
def test_confidence_never_falls_when_agreement_or_evidence_rises(
    low: float, high: float, evidence: float
) -> None:
    lo, hi = sorted((low, high))
    assert calibrated_confidence(report_with(lo, evidence)) <= calibrated_confidence(
        report_with(hi, evidence)
    )
    assert calibrated_confidence(report_with(lo, lo)) <= calibrated_confidence(
        report_with(lo, hi)
    )


@FAST
@given(unit, unit, st.integers(0, 4))
def test_a_disputed_point_can_only_lower_confidence(
    score: float, evidence: float, disputes: int
) -> None:
    plain = calibrated_confidence(report_with(score, evidence))
    disputed = calibrated_confidence(
        report_with(score, evidence, contradicted=[f"C{i}" for i in range(disputes)])
    )
    assert disputed <= plain


# ------------------------------------------------------------------------------ cost math

tokens = st.integers(0, 5_000_000)
price = st.floats(0.0, 200.0, allow_nan=False, allow_infinity=False)


@st.composite
def schedules(draw: st.DrawFn, *, cheap_cache: bool = False) -> PriceSchedule:
    input_price = draw(price)
    cached = draw(st.one_of(st.none(), price))
    if cheap_cache and cached is not None:
        cached = min(cached, input_price)
    return PriceSchedule(
        input_per_1m=input_price,
        output_per_1m=draw(price),
        cached_input_per_1m=cached,
        cache_write_per_1m=draw(st.one_of(st.none(), price)),
        reasoning_per_1m=draw(st.one_of(st.none(), price)),
        verified_on=date(2026, 10, 5),
        source_url="https://example.test/p",
    )


@FAST
@given(schedules(), tokens, tokens, tokens, tokens, tokens)
def test_a_call_never_costs_a_negative_or_infinite_amount(
    schedule: PriceSchedule, i: int, o: int, cached: int, written: int, reasoning: int
) -> None:
    cost = _token_cost(
        schedule,
        input_tokens=i,
        output_tokens=o,
        cached_input_tokens=cached,
        cache_write_tokens=written,
        reasoning_tokens=reasoning,
    )
    assert cost >= 0.0 and math.isfinite(cost)


@FAST
@given(schedules())
def test_no_tokens_cost_nothing(schedule: PriceSchedule) -> None:
    assert _token_cost(schedule, input_tokens=0, output_tokens=0) == 0.0


@FAST
@given(schedules(), tokens, tokens, tokens, tokens)
def test_more_tokens_never_cost_less(
    schedule: PriceSchedule, i: int, o: int, more_i: int, more_o: int
) -> None:
    base = _token_cost(schedule, input_tokens=i, output_tokens=o)
    assert _token_cost(schedule, input_tokens=i + more_i, output_tokens=o) >= base - 1e-9
    assert _token_cost(schedule, input_tokens=i, output_tokens=o + more_o) >= base - 1e-9


@FAST
@given(schedules(), tokens, tokens)
def test_output_cost_is_linear_in_tokens(schedule: PriceSchedule, a: int, b: int) -> None:
    zero = _token_cost(schedule, input_tokens=0, output_tokens=0)
    both = _token_cost(schedule, input_tokens=0, output_tokens=a + b)
    parts = _token_cost(schedule, input_tokens=0, output_tokens=a) + _token_cost(
        schedule, input_tokens=0, output_tokens=b
    )
    assert both - zero == pytest.approx(parts - 2 * zero, rel=1e-9, abs=1e-9)


@FAST
@given(schedules(cheap_cache=True), tokens, tokens, tokens)
def test_reading_from_the_cache_never_costs_more_than_reading_fresh(
    schedule: PriceSchedule, i: int, o: int, cached: int
) -> None:
    fresh = _token_cost(schedule, input_tokens=i, output_tokens=o)
    cached_cost = _token_cost(schedule, input_tokens=i, output_tokens=o, cached_input_tokens=cached)
    assert cached_cost <= fresh + 1e-9


@FAST
@given(schedules(), tokens, tokens, tokens)
def test_more_cached_tokens_than_input_are_treated_as_all_input_cached(
    schedule: PriceSchedule, i: int, o: int, extra: int
) -> None:
    exact = _token_cost(schedule, input_tokens=i, output_tokens=o, cached_input_tokens=i)
    over = _token_cost(schedule, input_tokens=i, output_tokens=o, cached_input_tokens=i + extra)
    assert over == pytest.approx(exact)


@FAST
@given(schedules(), tokens, tokens, tokens)
def test_reasoning_tokens_are_free_unless_the_catalog_prices_them_separately(
    schedule: PriceSchedule, i: int, o: int, reasoning: int
) -> None:
    unpriced = schedule.model_copy(update={"reasoning_per_1m": None})
    assert _token_cost(unpriced, input_tokens=i, output_tokens=o, reasoning_tokens=reasoning) == (
        _token_cost(unpriced, input_tokens=i, output_tokens=o)
    )


costs = st.one_of(st.none(), st.floats(0.0, 10.0, allow_nan=False))


def record(cost: float | None, stage: str = "panel") -> CallRecord:
    return CallRecord(
        stage=stage,  # type: ignore[arg-type]
        model_alias="m",
        provider="p",
        cost_usd=cost,
        cost_known=cost is not None,
    )


@FAST
@given(st.lists(costs, max_size=12), st.randoms(use_true_random=False))
def test_the_ledger_total_is_the_sum_of_its_calls_in_any_order(
    values: list[float | None], rng: object
) -> None:
    shuffled = list(values)
    rng.shuffle(shuffled)  # type: ignore[attr-defined]
    first, second = RunLedger(), RunLedger()
    for value in values:
        first.add(record(value))
    for value in shuffled:
        second.add(record(value))
    total = first.total_cost()
    assert total.usd == pytest.approx(second.total_cost().usd)
    assert total.known == (None not in values)
    assert total.usd == pytest.approx(sum(v for v in values if v is not None))


@FAST
@given(st.lists(st.floats(0.0, 10.0, allow_nan=False), max_size=8), st.floats(0.0, 10.0))
def test_shadow_calls_never_change_what_the_run_cost(
    values: list[float], shadow_cost: float
) -> None:
    plain, with_shadow = RunLedger(), RunLedger()
    for value in values:
        plain.add(record(value))
        with_shadow.add(record(value))
    with_shadow.add(record(shadow_cost, "shadow_baseline"))
    with_shadow.add(record(shadow_cost, "shadow_judge"))
    assert with_shadow.total_cost().usd == pytest.approx(plain.total_cost().usd)


# --------------------------------------------------------------------------------------- statistics

numbers = st.lists(st.floats(-1e6, 1e6, allow_nan=False), min_size=1, max_size=40)


@FAST
@given(numbers, unit, unit)
def test_a_percentile_is_one_of_the_values_and_rises_with_q(
    values: list[float], q1: float, q2: float
) -> None:
    lo, hi = sorted((q1, q2))
    a, b = percentile(values, lo), percentile(values, hi)
    assert a in values and b in values
    assert a is not None and b is not None and a <= b


@FAST
@given(numbers, unit)
def test_a_percentile_is_the_nearest_rank(values: list[float], q: float) -> None:
    """The smallest value with at least a ``q`` share of the data at or below it."""
    result = percentile(values, q)
    assert result is not None

    def share_at_or_below(v: float) -> float:
        return sum(x <= v for x in values) / len(values)

    assert share_at_or_below(result) >= q - 1e-12
    assert all(share_at_or_below(v) < q for v in values if v < result)
    assert percentile(values, 1.0) == max(values)
    assert percentile(values, 0.0) == min(values)


@FAST
@given(st.integers(0, 60), st.integers(0, 60))
def test_the_sign_test_is_a_symmetric_probability(wins: int, losses: int) -> None:
    p = sign_test_p(wins, losses)
    assert p == sign_test_p(losses, wins)
    if wins + losses == 0:
        assert p is None
    else:
        assert p is not None and 0.0 <= p <= 1.0
        if wins == losses:
            assert p == 1.0


points = st.dictionaries(
    st.sampled_from(list(string.ascii_lowercase[:8])),
    st.tuples(st.integers(0, 20).map(float), st.integers(0, 20).map(float)),
    min_size=1,
    max_size=8,
)


def dominates(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] <= b[0] and a[1] >= b[1] and (a[0] < b[0] or a[1] > b[1])


@FAST
@given(points)
def test_the_pareto_frontier_is_undominated_and_dominates_everything_else(
    pts: dict[str, tuple[float, float]],
) -> None:
    frontier = pareto_frontier(pts)
    assert frontier  # some point is always best
    for name in frontier:
        assert not any(dominates(other, pts[name]) for other in pts.values())
    for name in set(pts) - set(frontier):
        assert any(dominates(pts[f], pts[name]) for f in frontier)
    assert [pts[n][0] for n in frontier] == sorted(pts[n][0] for n in frontier)


# ------------------------------------------------------------------------ redaction and cleaning

SECRETS = [
    "sk-ant-api03-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7b",
    "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6",
    "AKIA" + "IOSFODNN7EXAMPLE",
    "AIza" + "SyA-Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0e",
    "xoxb-" + "123456789012-1234567890123-Ab3dE6gH9jK2mN5pQ8sT1vW4",
    "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P",
]
prose = st.text(alphabet=string.ascii_letters + string.digits + " .,;:()\n", max_size=80)


@FAST
@given(st.sampled_from(SECRETS), prose, prose)
def test_a_secret_is_removed_wherever_it_sits_in_the_text(
    secret: str, before: str, after: str
) -> None:
    result = redact_secrets(f"{before} {secret} {after}", entropy=EntropyConfig(enabled=False))
    assert secret not in result.text
    assert result.redaction_count >= 1


@FAST
@given(st.text(max_size=200))
def test_redaction_is_idempotent(text: str) -> None:
    once = redact_secrets(text)
    twice = redact_secrets(once.text)
    assert twice.text == once.text and twice.redaction_count == 0


@FAST
@given(prose)
def test_ordinary_prose_is_left_alone(text: str) -> None:
    assert redact_secrets(text, entropy=EntropyConfig(enabled=False)).text == text


FORBIDDEN = set(range(0x00, 0x20)) - {0x09, 0x0A}
FORBIDDEN |= set(range(0x7F, 0xA0)) | set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
# Fragments a hostile model could emit, mixed with ordinary text so that most examples carry some.
DANGEROUS = [
    "\x1b[31m",
    "\x1b[2K\x1b[1A",
    "\x1b]0;title\x07",
    "\x1b]8;;http://x.test\x1b\\",
    "\x1bPdcs\x1b\\",
    "\x1bc",
    "\x1b",
    "\r",
    "\r\n",
    "\x00",
    "\x07",
    "\x9b",
    chr(0x202E),
    chr(0x2066),
    chr(0x2069),
]
terminalish = st.lists(
    st.one_of(st.text(max_size=10), st.sampled_from(DANGEROUS)), max_size=14
).map("".join)


@FAST
@given(terminalish)
def test_cleaned_text_has_no_control_characters_and_cleaning_is_idempotent(text: str) -> None:
    cleaned = strip_control(text)
    assert not any(ord(c) in FORBIDDEN for c in cleaned)
    assert strip_control(cleaned) == cleaned


@FAST
@given(st.text(alphabet=st.characters(blacklist_categories=("Cc", "Cf", "Cs")), max_size=120))
def test_text_without_controls_is_untouched(text: str) -> None:
    assert strip_control(text) == text


SEQUENCES = [
    "\x1b[31m",
    "\x1b[0m",
    "\x1b[2K",
    "\x1b[1;1H",
    "\x1b[?25l",
    "\x1b]0;title\x07",
    "\x1b]8;;http://x.test\x1b\\",
    "\x1bPq\x1b\\",
    "\x1bc",
]
plain = st.text(alphabet=string.ascii_letters + string.digits + " \n", max_size=12)


@FAST
@given(st.lists(st.tuples(plain, st.sampled_from(SEQUENCES)), max_size=8), plain)
def test_escape_sequences_vanish_without_taking_any_text_with_them(
    parts: list[tuple[str, str]], tail: str
) -> None:
    dirty = "".join(text + sequence for text, sequence in parts) + tail
    assert strip_control(dirty) == "".join(text for text, _ in parts) + tail
