"""Build pipelines and their dependencies from settings. The one place that knows about modes."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import TypeVar

from fusion.config.catalog import load_catalog
from fusion.config.env import is_test_mode
from fusion.config.loader import BaselineEntry
from fusion.evals.engine import EvalEngine
from fusion.orchestration.context import PipelineDeps
from fusion.orchestration.output import ResultPresenter
from fusion.orchestration.pipeline import BasePipeline
from fusion.orchestration.specialized import (
    AnswerEvalPipeline,
    ArchitectureDecisionPipeline,
    CodeReviewPipeline,
    DebugPipeline,
    FusionAskPipeline,
    ImplementationPlanPipeline,
    PipelineMap,
)
from fusion.providers.base import ModelProvider
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import RoutingPolicy, build_routing
from fusion.security.policy import SecurityPolicy
from fusion.storage.run_store import RunStore
from fusion.telemetry.cost import PricingRegistry

_PipelineT = TypeVar("_PipelineT", bound=BasePipeline)

ProviderSet = dict[str, ModelProvider]


@dataclass
class Settings:
    """How to build pipelines. ``use_mock=None`` follows ``FUSION_DEFAULT_PROVIDER=mock``."""

    use_mock: bool | None = None
    db_path: str | None = None
    use_llm_judge: bool = True
    run_store: RunStore | None = None
    pricing: PricingRegistry | None = None
    security: SecurityPolicy | None = None


def build_provider_registry(use_mock: bool = False) -> ProviderSet:
    """Build provider registry from environment."""
    from fusion.providers.anthropic import AnthropicProvider
    from fusion.providers.google import GoogleProvider
    from fusion.providers.limits import build_limiters
    from fusion.providers.lmstudio import LMStudioProvider
    from fusion.providers.mock import MockProvider
    from fusion.providers.ollama import OllamaProvider
    from fusion.providers.openai import OpenAIProvider

    limiters = build_limiters(load_catalog())
    providers: ProviderSet = {}
    if use_mock:
        providers["mock"] = MockProvider()

    provider_classes = (
        AnthropicProvider,
        OpenAIProvider,
        GoogleProvider,
        OllamaProvider,
        LMStudioProvider,
    )
    for cls in provider_classes:
        instance = cls(limiter=limiters.get(cls.name))
        if instance.is_available():
            providers[instance.name] = instance

    if not use_mock and not any(name in providers for name in ("anthropic", "openai", "google")):
        msg = (
            "No cloud providers configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "and/or GOOGLE_API_KEY in .env, or run with --mock for offline mode."
        )
        raise RuntimeError(msg)

    return providers


def _mock_baseline(providers: ProviderSet) -> BaselineEntry | None:
    if "mock" not in providers:
        return None
    return BaselineEntry(
        name="Mock Baseline", provider="mock", model="mock-fast", model_id="mock-fast"
    )


def build_deps(settings: Settings, providers: ProviderSet | None = None) -> PipelineDeps:
    """Wire registry, routing, evals, storage and pricing for one mode."""
    use_mock = is_test_mode(settings.use_mock)
    resolved = providers if providers is not None else build_provider_registry(use_mock=use_mock)
    registry = ModelRegistry.for_mode(use_mock=use_mock)
    routing: RoutingPolicy = build_routing(use_mock=use_mock)
    eval_engine = EvalEngine(
        registry=registry, provider_resolver=resolved, use_llm_judge=settings.use_llm_judge
    )
    run_store = settings.run_store or RunStore(db_path=settings.db_path)
    pricing = settings.pricing or PricingRegistry()
    return PipelineDeps(
        registry=registry,
        routing=routing,
        providers=resolved,
        eval_engine=eval_engine,
        run_store=run_store,
        pricing=pricing,
        security=settings.security or SecurityPolicy.from_env(),
        presenter=ResultPresenter(eval_engine=eval_engine, run_store=run_store, pricing=pricing),
        shadow_baseline=_mock_baseline(resolved) if use_mock else None,
    )


def _pipeline(cls: type[_PipelineT], deps: PipelineDeps) -> _PipelineT:
    return cls(
        registry=deps.registry,
        routing=deps.routing,
        providers=deps.providers,
        eval_engine=deps.eval_engine,
        run_store=deps.run_store,
        security_policy=deps.security,
        pricing=deps.pricing,
        shadow_baseline=deps.shadow_baseline,
    )


def build_pipeline(settings: Settings, providers: ProviderSet | None = None) -> BasePipeline:
    """One generic pipeline."""
    return _pipeline(BasePipeline, build_deps(settings, providers))


def build_pipelines(settings: Settings, providers: ProviderSet | None = None) -> PipelineMap:
    """All specialized pipelines, sharing one set of dependencies."""
    deps = build_deps(settings, providers)
    return {
        "ask": _pipeline(FusionAskPipeline, deps),
        "code_review": _pipeline(CodeReviewPipeline, deps),
        "debug": _pipeline(DebugPipeline, deps),
        "architecture": _pipeline(ArchitectureDecisionPipeline, deps),
        "plan": _pipeline(ImplementationPlanPipeline, deps),
        "answer_eval": _pipeline(AnswerEvalPipeline, deps),
    }


def create_pipeline(
    *,
    providers: ProviderSet | None = None,
    db_path: str | None = None,
    use_llm_judge: bool = True,
    use_mock: bool | None = None,
    run_store: RunStore | None = None,
) -> BasePipeline:
    """Deprecated alias of ``build_pipeline``."""
    _deprecated("create_pipeline", "build_pipeline")
    settings = Settings(use_mock, db_path, use_llm_judge, run_store)
    return build_pipeline(settings, providers)


def create_pipelines(
    *,
    providers: ProviderSet | None = None,
    db_path: str | None = None,
    use_llm_judge: bool = True,
    use_mock: bool | None = None,
    run_store: RunStore | None = None,
) -> PipelineMap:
    """Deprecated alias of ``build_pipelines``."""
    _deprecated("create_pipelines", "build_pipelines")
    settings = Settings(use_mock, db_path, use_llm_judge, run_store)
    return build_pipelines(settings, providers)


def _deprecated(old: str, new: str) -> None:
    warnings.warn(
        f"{old}() is deprecated; use {new}(Settings(...), providers) instead",
        DeprecationWarning,
        stacklevel=3,
    )


__all__ = [
    "ProviderSet",
    "Settings",
    "build_deps",
    "build_pipeline",
    "build_pipelines",
    "build_provider_registry",
    "create_pipeline",
    "create_pipelines",
]
