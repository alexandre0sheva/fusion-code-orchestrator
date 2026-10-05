"""Running a judge calibration against a benchmark environment (``fusion bench calibrate-judge``).

The statistics live in ``scoring/calibration.py``; this adds what a live run needs around them: the
catalog's prices for a forecast, the live-spend cap checked before the first call, and the money
actually spent appended to the spend ledger even when the run is cut short.
"""

from __future__ import annotations

import asyncio

from fusion.bench.runner import BenchEnv
from fusion.bench.scoring import ScoreEnv, ScoringError
from fusion.bench.scoring.calibration import (
    CalibrationCase,
    CalibrationReport,
    calibrate,
    calibration_calls,
)
from fusion.bench.spec import BenchTask
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.routing.budget import forecast_calls

__all__ = ["SPEND_TASK", "default_mock_judges", "forecast_usd", "run_calibration"]

SPEND_TASK = "calibration"  # the ``task`` field of the spend-ledger entries it writes


def default_mock_judges(env: BenchEnv, count: int = 3) -> list[str]:
    """One catalog model from each of the first ``count`` providers, for a keyless dry run."""
    chosen: dict[str, str] = {}
    for alias, entry in sorted(env.registry.models.items()):
        chosen.setdefault(entry.provider, alias)
    return list(chosen.values())[:count]


def forecast_usd(
    env: BenchEnv, tasks: list[BenchTask], cases: list[CalibrationCase], judges: list[str]
) -> float:
    """What the calibration is expected to cost at the catalog's prices."""
    calls = calibration_calls(tasks, cases, judges)
    return forecast_calls(calls, env.registry.models, env.pricing).usd


async def run_calibration(
    env: BenchEnv,
    tasks: list[BenchTask],
    cases: list[CalibrationCase],
    judges: list[str],
    *,
    max_usd: float | None = None,
    seed: int = 0,
    concurrency: int = 8,
    mock: bool = False,
) -> CalibrationReport:
    """Calibrate ``judges`` on ``cases``. A live run (``mock=False``) refuses to start when its
    forecast does not fit ``max_usd`` and the live-spend cap, and records what it spent."""
    unknown = [j for j in judges if j not in env.registry.models]
    if unknown:
        msg = f"judge models not in the catalog: {', '.join(unknown)}"
        raise ScoringError(msg)
    spend = None if mock else env.spend
    if spend is not None:
        if max_usd is None:
            msg = "a live calibration needs --max-usd"
            raise ScoringError(msg)
        estimate = forecast_usd(env, tasks, cases, judges)
        if estimate > max_usd:
            msg = f"the calibration is forecast at ${estimate:.4f}, over --max-usd ${max_usd:.2f}"
            raise ScoringError(msg)
        spend.check(estimate, "judge calibration")
    ledger = RunLedger(asyncio.get_running_loop().time)
    gateway = CallGateway(
        ledger=ledger,
        models=env.registry.models,
        providers=env.providers,
        pricing=env.pricing,
        truncate_prompts=False,
        temperature=0.0,
        seed=seed,
    )
    score_env = ScoreEnv(gateway=gateway, judge_models=judges)
    try:
        return await calibrate(tasks, cases, score_env, judges, concurrency=concurrency, mock=mock)
    finally:
        if spend is not None:
            spend.append(
                ledger.total_cost().usd,
                task=SPEND_TASK,
                purpose=f"judges {','.join(judges)} on {len(cases)} cases",
            )
