"""Cost, usage, fanout, and MCP envelope tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from fusion.config.loader import FanoutConfig, ModelEntry, PricingConfig, PricingEntry
from fusion.evals.engine import EvalEngine
from fusion.evals.schemas import ContextEvalResult, FinalEvalResult, ModelResponseEval
from fusion.mcp_server.schemas import ReviewDiffInput
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.fanout import fanout_to_panel
from fusion.orchestration.pipelines import CodeReviewInput, create_pipelines
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.storage.run_store import RunStore
from fusion.telemetry.cost import PricingRegistry, UsageSummary, compare_to_baseline


class DelayedProvider(ModelProvider):
    """Provider with per-model delays for fanout tests."""

    name = "mock"

    def __init__(self, delays: dict[str, float], failures: set[str] | None = None) -> None:
        self._delays = delays
        self._failures = failures or set()
        self.seen_prompts: list[str] = []

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.seen_prompts.append(request.user_prompt)
        delay = self._delays.get(request.model_id, 0.0)
        await asyncio.sleep(delay)
        if request.model_id in self._failures:
            return ModelResponse(
                provider=self.name,
                model=request.model_id,
                error="configured failure",
                error_type="ConfiguredFailure",
                latency_ms=delay * 1000,
            )
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=f"response from {request.model_id}",
            input_tokens=100,
            output_tokens=20,
            latency_ms=delay * 1000,
        )


def _registry_models() -> dict[str, ModelEntry]:
    registry = ModelRegistry()
    return {
        name: registry.models[name]
        for name in ["mock-fast", "mock-security", "mock-weak", "mock-judge"]
    }


@pytest.mark.asyncio
async def test_parallel_fanout_wall_time_is_not_sum() -> None:
    provider = MockProvider(latency_ms=1000)
    started = time.perf_counter()
    result = await fanout_to_panel(
        panel_models=["mock-fast", "mock-security", "mock-weak"],
        registry_models=_registry_models(),
        providers={"mock": provider},
        task_type=TaskType.CODE_REVIEW,
        primary_content="diff --git a/a.py b/a.py\n+print('x')",
        config=FanoutConfig(
            max_concurrency=3,
            per_model_timeout_seconds=3,
            global_timeout_seconds=4,
            min_successful_responses=2,
        ),
    )
    elapsed = time.perf_counter() - started
    assert result.success_count == 3
    assert result.quorum_met
    assert elapsed < 2.0
    assert result.panel_wall_latency_ms < 2000
    assert result.total_model_call_latency_ms >= 2900


@pytest.mark.asyncio
async def test_timeout_partial_results_preserved() -> None:
    provider = DelayedProvider(
        {"mock-fast": 0.01, "mock-security": 0.01, "mock-weak": 0.20}
    )
    result = await fanout_to_panel(
        panel_models=["mock-fast", "mock-security", "mock-weak"],
        registry_models=_registry_models(),
        providers={"mock": provider},
        task_type=TaskType.CODE_REVIEW,
        primary_content="diff --git a/a.py b/a.py\n+print('x')",
        config=FanoutConfig(
            max_concurrency=3,
            per_model_timeout_seconds=0.05,
            global_timeout_seconds=1,
            min_successful_responses=2,
        ),
    )
    assert result.success_count == 2
    assert result.failed_count == 1
    assert result.quorum_met
    assert any(call.status == "timeout" for call in result.calls)


@pytest.mark.asyncio
async def test_below_quorum_returns_diagnostic_pipeline_result(tmp_path) -> None:
    provider = DelayedProvider({"mock-fast": 0.2, "mock-security": 0.2, "mock-weak": 0.2})
    pipes = create_pipelines(providers={"mock": provider}, db_path=str(tmp_path / "runs.db"))
    pipe = pipes["code_review"]
    pipe._routing.budgets.fanout = FanoutConfig(
        max_concurrency=3,
        per_model_timeout_seconds=0.01,
        global_timeout_seconds=0.05,
        min_successful_responses=2,
    )
    result = await pipe.review(
        CodeReviewInput(diff="diff --git a/a.py b/a.py\n+print('x')")
    )
    assert "quorum" in result.summary.lower()
    assert result.usage is not None
    assert result.usage.failed_model_calls >= 2
    assert result.cost_comparison is not None
    assert result.warnings


def test_cost_calculation_and_baseline_comparison() -> None:
    pricing = PricingRegistry(
        PricingConfig(
            pricing={
                "test.model": PricingEntry(
                    provider="test",
                    model_id="model",
                    alias="test.model",
                    input_price_per_1m_tokens=10,
                    output_price_per_1m_tokens=20,
                    is_estimate=False,
                ),
                "anthropic.claude-opus-4-8": PricingEntry(
                    provider="anthropic",
                    model_id="claude-opus-4-8",
                    alias="anthropic.claude-opus-4-8",
                    input_price_per_1m_tokens=15,
                    output_price_per_1m_tokens=75,
                    is_estimate=True,
                ),
            }
        )
    )
    response = ModelResponse(
        provider="test",
        model="model",
        text="ok",
        input_tokens=1000,
        output_tokens=500,
    )
    cost = pricing.estimate_response_cost(response)
    assert cost.known
    assert cost.amount_usd == pytest.approx(0.02)

    usage = UsageSummary(
        total_input_tokens=1000,
        total_output_tokens=500,
        total_tokens=1500,
        fusion_wall_latency_ms=1000,
    )
    comparison = compare_to_baseline(
        usage=usage,
        fusion_total_cost_usd=cost.amount_usd,
        fusion_cost_known=True,
        pricing=pricing,
    )
    assert comparison.baseline_estimated_cost_usd == pytest.approx(0.0525)
    assert comparison.fusion_is_cheaper is True
    assert comparison.savings_percent is not None


def test_unknown_pricing_stays_unknown() -> None:
    pricing = PricingRegistry(PricingConfig(pricing={}))
    response = ModelResponse(
        provider="unknown",
        model="model",
        text="ok",
        input_tokens=100,
        output_tokens=100,
    )
    cost = pricing.estimate_response_cost(response)
    assert cost.amount_usd is None
    assert not cost.known


@pytest.mark.asyncio
async def test_mcp_output_includes_markdown_usage_and_comparison(tmp_path) -> None:
    tools = FusionTools(db_path=str(tmp_path / "runs.db"), use_mock=True)
    output = await tools.fusion_review_diff(
        ReviewDiffInput(diff="diff --git a/a.py b/a.py\n+print('x')")
    )
    assert "display_markdown" in output
    assert "Cost & usage" in output["display_markdown"]
    assert "usage" in output and output["usage"]["per_model"]
    assert "cost_comparison" in output
    assert output["cost_comparison"]["baseline_name"] == "Opus 4.8"
    assert "result" in output


@pytest.mark.asyncio
async def test_redaction_before_provider_calls(tmp_path) -> None:
    provider = DelayedProvider({"mock-fast": 0.0, "mock-security": 0.0, "mock-weak": 0.0})
    pipes = create_pipelines(providers={"mock": provider}, db_path=str(tmp_path / "runs.db"))
    secret = "API_KEY=sk-testsecret12345678901234567890"
    await pipes["code_review"].review(
        CodeReviewInput(diff=f"diff --git a/a.py b/a.py\n+{secret}")
    )
    assert provider.seen_prompts
    assert all("sk-testsecret" not in prompt for prompt in provider.seen_prompts)


def _answer_eval(model: str, score: float, unsupported: float = 0.0) -> ModelResponseEval:
    return ModelResponseEval(
        model_name=model,
        specificity=score,
        groundedness=score,
        actionability=score,
        correctness_likelihood=score,
        risk_awareness=score,
        unsupported_claims=unsupported,
        codebase_awareness=score,
        novelty=score,
        overall_score=score,
    )


def _final_eval(score: float, residual_risk: float = 0.1) -> FinalEvalResult:
    return FinalEvalResult(
        final_answer_quality=score,
        claude_code_usefulness=score,
        implementation_readiness=score,
        test_plan_quality=score,
        residual_risk=residual_risk,
        confidence=score,
        overall_score=score,
    )


def test_confidence_aggregation_penalizes_context_failures_and_unsupported_claims() -> None:
    engine = EvalEngine(registry=ModelRegistry(), provider_resolver={}, use_llm_judge=False)
    strong = engine.build_pipeline_evals(
        context=ContextEvalResult(sufficient=True, score=0.9),
        per_answer=[_answer_eval("a", 0.9), _answer_eval("b", 0.9)],
        disagreement={"disagreement_score": 0.05},
        final=_final_eval(0.9),
    )
    weak = engine.build_pipeline_evals(
        context=ContextEvalResult(sufficient=False, score=0.2),
        per_answer=[_answer_eval("a", 0.7, unsupported=0.8)],
        disagreement={"disagreement_score": 0.6},
        final=_final_eval(0.6, residual_risk=0.8),
        warnings=["Panel model b timeout: timed out"],
    )
    assert strong["aggregate_score"] > weak["aggregate_score"]
    assert weak["dimension_scores"]["aggregate_provider_success_rate"] < 1.0


@pytest.mark.asyncio
async def test_run_storage_persists_usage_and_cost_comparison(tmp_path) -> None:
    db_path = str(tmp_path / "runs.db")
    tools = FusionTools(db_path=db_path, use_mock=True)
    output = await tools.fusion_review_diff(
        ReviewDiffInput(diff="diff --git a/a.py b/a.py\n+print('x')")
    )
    record = RunStore(db_path=db_path).get_run(output["run_id"])
    assert record is not None
    assert record.output_data is not None
    assert "usage" in record.output_data
    assert "cost_comparison" in record.output_data
