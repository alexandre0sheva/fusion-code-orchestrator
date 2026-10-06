"""Every prompt that leaves the machine is redacted at the call gateway, and so are stored errors.

The first redaction happens when a run opens (``RedactStage``). That is not enough on its own:
refinement, judge, synthesis and shadow prompts embed *model output* and are built later, and a
model can echo a secret it was shown or invent a secret-shaped string. The gateway is the one place
every call passes through, so it is where the guarantee lives.
"""

from __future__ import annotations

from datetime import date

import pytest

from fusion.config.catalog import Catalog, ModelEntry, PriceSchedule
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.factory import Settings, build_pipeline
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.providers.base import Message, ModelProvider, ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskType
from fusion.telemetry.cost import PricingRegistry

# Built from pieces so a secret scanner does not mistake the fixture for a leak.
SECRET = "sk-" + "proj-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"
GITHUB = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"


def _catalog() -> Catalog:
    price = PriceSchedule(
        input_per_1m=1.0,
        output_per_1m=2.0,
        verified_on=date(2026, 10, 5),
        source_url="https://example.test/p",
    )
    entry = ModelEntry(alias="big", provider="fake", model_id="big-1", prices=[price])
    return Catalog(models={"big": entry})


class Recorder(ModelProvider):
    """Remembers every request it was sent; answers with fixed text or a fixed error."""

    name = "fake"

    def __init__(self, *, text: str = "ok", error: str | None = None) -> None:
        self.requests: list[ModelRequest] = []
        self.text = text
        self.error = error

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.error:
            return ModelResponse(
                provider="fake", model=request.model_id, error=self.error, error_type="Server"
            )
        return ModelResponse(
            provider="fake", model=request.model_id, text=self.text, input_tokens=5, output_tokens=5
        )


def make_gateway(
    provider: ModelProvider, *, redact: bool | None = None
) -> tuple[CallGateway, RunLedger, list[str]]:
    catalog = _catalog()
    ledger = RunLedger()
    warnings: list[str] = []
    options = {} if redact is None else {"redact": redact}
    gateway = CallGateway(
        ledger=ledger,
        models=catalog.models,
        providers={"fake": provider},
        pricing=PricingRegistry(catalog),
        warnings=warnings,
        **options,  # type: ignore[arg-type]
    )
    return gateway, ledger, warnings


# ------------------------------------------------------------------------------------ the gateway


async def test_gateway_redacts_the_user_prompt_the_system_prompt_and_messages() -> None:
    provider = Recorder()
    gateway, _, warnings = make_gateway(provider)
    request = ModelRequest(
        model_id="big-1",
        system_prompt=f"You review code. Our key is {SECRET}.",
        user_prompt=f"Why does this fail?\napi_key = '{GITHUB}'",
    )
    await gateway.call(stage="panel", alias="big", request=request)
    (sent,) = provider.requests
    assert SECRET not in sent.system_prompt and GITHUB not in sent.user_prompt
    assert "Why does this fail?" in sent.user_prompt
    assert any("2 secret" in w for w in warnings)
    assert not any(SECRET in w or GITHUB in w for w in warnings)


async def test_gateway_redacts_chat_messages() -> None:
    provider = Recorder()
    gateway, _, _ = make_gateway(provider)
    request = ModelRequest(
        model_id="big-1",
        messages=[
            Message(role="system", content="be brief"),
            Message(role="user", content=f"token: {GITHUB}"),
        ],
    )
    await gateway.call(stage="judge", alias="big", request=request)
    assert all(GITHUB not in m.content for m in provider.requests[0].messages)
    assert provider.requests[0].messages[0].content == "be brief"


async def test_gateway_leaves_a_clean_prompt_untouched_and_says_nothing() -> None:
    provider = Recorder()
    gateway, _, warnings = make_gateway(provider)
    request = ModelRequest(model_id="big-1", user_prompt="def f():\n    return 1\n")
    await gateway.call(stage="panel", alias="big", request=request)
    assert provider.requests[0].user_prompt == request.user_prompt
    assert warnings == []


async def test_gateway_can_be_told_not_to_redact_for_benchmark_tasks_about_secrets() -> None:
    provider = Recorder()
    gateway, _, warnings = make_gateway(provider, redact=False)
    request = ModelRequest(model_id="big-1", user_prompt=f"Is this key leaked? {SECRET}")
    await gateway.call(stage="panel", alias="big", request=request)
    assert provider.requests[0].user_prompt == request.user_prompt
    assert warnings == []


async def test_gateway_redacts_by_default() -> None:
    provider = Recorder()
    gateway, _, _ = make_gateway(provider)  # no redact argument: fail safe
    await gateway.call(
        stage="panel",
        alias="big",
        request=ModelRequest(model_id="big-1", user_prompt=f"key {SECRET}"),
    )
    assert SECRET not in provider.requests[0].user_prompt


async def test_stored_errors_are_redacted_even_when_prompt_redaction_is_off() -> None:
    # A provider's 400 often quotes the request back.
    provider = Recorder(error=f"invalid request near 'password = {GITHUB}'")
    gateway, ledger, _ = make_gateway(provider, redact=False)
    response = await gateway.call(
        stage="panel", alias="big", request=ModelRequest(model_id="big-1", user_prompt="hi")
    )
    (record,) = ledger.records
    assert response.error is not None and GITHUB not in response.error
    assert record.error is not None and GITHUB not in record.error
    assert "invalid request" in record.error


# ------------------------------------------------------------------------------------- the pipeline


class Echo(MockProvider):
    """A mock whose panel answers repeat a secret (a model echoing what it saw, or inventing one),
    and which remembers each request by the stage that sent it."""

    def __init__(self) -> None:
        super().__init__(latency_ms=1.0)
        self.seen: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.seen.append(request)
        response = await super().complete(request)
        if request.metadata.get("role") in {None, "panel"} and "judge" not in str(request.metadata):
            response = response.model_copy(
                update={"text": f"{response.text}\n\nNote: the key is {SECRET}"}
            )
        return response

    def is_available(self) -> bool:
        return True


def _everything_sent(provider: Echo) -> str:
    parts: list[str] = []
    for request in provider.seen:
        parts.append(request.system_prompt)
        parts.append(request.user_prompt)
        parts.extend(m.content for m in request.messages)
    return "\n".join(parts)


@pytest.mark.parametrize("budget", [BudgetLevel.MEDIUM, BudgetLevel.HIGH])
async def test_no_prompt_of_a_whole_run_carries_a_secret(
    tmp_path: pytest.TempPathFactory, budget: BudgetLevel, three_model_default: None
) -> None:
    provider = Echo()
    pipeline = build_pipeline(
        Settings(db_path=str(tmp_path / "r.db"), use_mock=True), providers={"mock": provider}
    )
    ctx = PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content=f"diff: +client = Client('{SECRET}')\n+    return eval(user_input)",
        context="Security sensitive change with plenty of surrounding context for the panel.",
        budget=budget,
        shadow_baseline=True,
    )
    result = await pipeline.run(ctx)
    sent = _everything_sent(provider)
    assert len(provider.seen) >= 4, "the run should have reached refine, judge or synthesis"
    assert SECRET not in sent  # the task, and the echo in the panel's answers
    assert result.run_id


# ------------------------------------------------------------------------------- what is stored


def _stored_text(db_path: str) -> str:
    """Every row of the run database as text, so a leak cannot hide in any column."""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return "\n".join(conn.iterdump())


def _secret_task() -> PipelineContext:
    return PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content=f"diff: +client = Client('{SECRET}')\n+    return eval(user_input)",
        context=f"Deployed with token {GITHUB} in the environment; plenty of context follows.",
        file_snippets=[f"API_KEY={GITHUB}\nDEBUG=false"],
        metadata={"note": f"owner pasted {SECRET}"},
        budget=BudgetLevel.MEDIUM,
    )


async def test_the_run_database_holds_no_secret_by_default(
    tmp_path: pytest.TempPathFactory, three_model_default: None
) -> None:
    db = str(tmp_path / "runs.db")
    pipeline = build_pipeline(Settings(db_path=db, use_mock=True))
    await pipeline.run(_secret_task())
    stored = _stored_text(db)
    assert "eval(user_input)" in stored, "the run itself should have been stored"
    assert SECRET not in stored and GITHUB not in stored


async def test_the_run_database_keeps_the_raw_input_only_when_the_owner_asks(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, three_model_default: None
) -> None:
    monkeypatch.setenv("FUSION_LOG_RAW_PROMPTS", "true")
    db = str(tmp_path / "runs.db")
    pipeline = build_pipeline(Settings(db_path=db, use_mock=True))
    await pipeline.run(_secret_task())
    import sqlite3

    with sqlite3.connect(db) as conn:
        query = "SELECT input_json, sanitized_input_json FROM runs"
        raw, sanitized = conn.execute(query).fetchone()
    assert SECRET in raw
    assert SECRET not in sanitized and GITHUB not in sanitized


# ------------------------------------------------------- shadow calls made without a gateway


async def test_a_shadow_comparison_run_without_a_gateway_still_redacts() -> None:
    from fusion.benchmark.shadow import run_shadow_comparison
    from fusion.config.loader import BaselineEntry

    provider = Recorder(text='{"winner": "tie"}')
    provider.name = "mock"  # type: ignore[misc]
    models = {
        "judge": ModelEntry(
            alias="judge",
            provider="mock",
            model_id="judge-1",
            prices=_catalog().models["big"].prices,
        )
    }
    await run_shadow_comparison(
        task_prompt=f"review this: api_key = '{GITHUB}'",
        system_prompt=f"you are a reviewer; our key is {SECRET}",
        fusion_answer=f"the key {GITHUB} should move to a vault",
        fusion_cost_usd=0.01,
        fusion_latency_ms=10.0,
        registry_models=models,
        providers={"mock": provider},
        judge_model_alias="judge",
        baseline=BaselineEntry(name="b", provider="mock", model="b", model_id="b-1"),
    )
    assert len(provider.requests) == 2  # the baseline call and the judge call
    for request in provider.requests:
        sent = request.system_prompt + request.user_prompt
        assert SECRET not in sent and GITHUB not in sent
