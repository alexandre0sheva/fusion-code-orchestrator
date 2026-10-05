"""Characterization tests: pipeline outputs in mock mode must not drift during refactors."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from _golden import check_golden
from fusion.orchestration.pipelines import PipelineContext, Settings, build_pipelines
from fusion.orchestration.schemas import (
    AnswerEvalInput,
    ArchitectureDecisionInput,
    CodeReviewInput,
    DebugInput,
    FusionAskInput,
    ImplementationPlanInput,
)
from fusion.providers.mock import MockProvider
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskType

DIFF = (
    "diff --git a/app/db.py b/app/db.py\n--- a/app/db.py\n+++ b/app/db.py\n"
    "@@ -1,3 +1,4 @@\n"
    '+query = f"SELECT * FROM users WHERE id = {user_id}"\n+cursor.execute(query)\n'
)


@pytest.fixture
def pipes(tmp_path: Path) -> Any:
    return build_pipelines(
        Settings(db_path=str(tmp_path / "g.db")), {"mock": MockProvider(latency_ms=1.0)}
    )


async def test_golden_code_review(pipes: Any) -> None:
    out = await pipes["code_review"].review(
        CodeReviewInput(diff=DIFF, changed_files=["app/db.py"], goals="find injection bugs")
    )
    check_golden("code_review", out.model_dump(mode="json"))


async def test_golden_ask(pipes: Any) -> None:
    out = await pipes["ask"].ask(
        FusionAskInput(prompt="How should I retry a failed HTTP call in httpx?", context="python")
    )
    check_golden("ask", out.model_dump(mode="json"))


async def test_golden_debug(pipes: Any) -> None:
    out = await pipes["debug"].debug(
        DebugInput(
            error_message="KeyError: 'user_id' in handler",
            logs="Traceback (most recent call last):\n  File app.py, line 3\nKeyError: 'user_id'",
            code_context="def handler(req): return req['user_id']",
        )
    )
    check_golden("debug", out.model_dump(mode="json"))


async def test_golden_architecture(pipes: Any) -> None:
    out = await pipes["architecture"].decide(
        ArchitectureDecisionInput(
            decision_question="Redis or in-memory cache?",
            options=["Redis", "In-memory"],
            constraints="single region, 10k RPS",
        )
    )
    check_golden("architecture", out.model_dump(mode="json"))


async def test_golden_plan(pipes: Any) -> None:
    out = await pipes["plan"].plan(
        ImplementationPlanInput(
            feature_request="Add OAuth2 login with Google and GitHub",
            constraints="no new dependencies beyond authlib",
        )
    )
    check_golden("plan", out.model_dump(mode="json"))


async def test_golden_answer_eval(pipes: Any) -> None:
    out = await pipes["answer_eval"].evaluate(
        AnswerEvalInput(
            question="What does asyncio.gather do?",
            answer="It runs awaitables concurrently and returns their results in order.",
        )
    )
    check_golden("answer_eval", out.model_dump(mode="json"))


async def test_golden_high_budget_refinement_and_shadow(
    pipes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(random, "random", lambda: 0.1)  # shadow shuffles answer order
    out = await pipes["ask"].ask(
        FusionAskInput(
            prompt="Explain the trade-offs of optimistic locking",
            budget=BudgetLevel.HIGH,
            shadow_baseline=True,
        )
    )
    check_golden("ask_high_shadow", out.model_dump(mode="json"))


async def test_golden_low_budget_single_model(pipes: Any) -> None:
    out = await pipes["ask"].ask(
        FusionAskInput(prompt="Name a good python http client", budget=BudgetLevel.LOW)
    )
    check_golden("ask_low", out.model_dump(mode="json"))


async def test_golden_insufficient_context(pipes: Any) -> None:
    result = await pipes["ask"].run(
        PipelineContext(task_type=TaskType.CODE_REVIEW, primary_content="x")
    )
    check_golden(
        "insufficient_context",
        {
            "final_answer": result.final_answer,
            "structured_output": result.structured_output,
            "disagreement": result.disagreement,
            "warnings": result.warnings,
            "panel": [p.model_name for p in result.panel_results],
            "steps": [s.step_name for s in result.trace.steps],
        },
    )
