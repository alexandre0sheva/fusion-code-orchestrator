"""Compatibility surface for the orchestration engine.

The implementation lives in ``context`` (inputs and run state), ``stages`` (the pipeline stages),
``ledger`` (cost accounting), ``result``, ``output``, ``pipeline`` (the runner), ``specialized``
(task pipelines) and ``factory`` (construction). Import from those modules in new code.
"""

from __future__ import annotations

from fusion.orchestration.context import PipelineContext, PipelineDeps, RunState
from fusion.orchestration.factory import (
    ProviderSet,
    Settings,
    build_deps,
    build_pipeline,
    build_pipelines,
    build_provider_registry,
    create_pipeline,
    create_pipelines,
)
from fusion.orchestration.ledger import CallRecord, RunLedger
from fusion.orchestration.pipeline import BasePipeline
from fusion.orchestration.result import PanelResult, PipelineResult
from fusion.orchestration.schemas import (
    AnswerEvalInput,
    ArchitectureDecisionInput,
    CodeReviewInput,
    DebugInput,
    FusionAskInput,
    ImplementationPlanInput,
)
from fusion.orchestration.specialized import (
    AnswerEvalPipeline,
    ArchitectureDecisionPipeline,
    CodeReviewPipeline,
    DebugPipeline,
    FusionAskPipeline,
    ImplementationPlanPipeline,
    PipelineMap,
)

# Backward-compatible alias
OrchestrationPipeline = BasePipeline

__all__ = [
    "AnswerEvalInput",
    "AnswerEvalPipeline",
    "ArchitectureDecisionInput",
    "ArchitectureDecisionPipeline",
    "BasePipeline",
    "CallRecord",
    "CodeReviewInput",
    "CodeReviewPipeline",
    "DebugInput",
    "DebugPipeline",
    "FusionAskInput",
    "FusionAskPipeline",
    "ImplementationPlanInput",
    "ImplementationPlanPipeline",
    "OrchestrationPipeline",
    "PanelResult",
    "PipelineContext",
    "PipelineDeps",
    "PipelineMap",
    "PipelineResult",
    "ProviderSet",
    "RunLedger",
    "RunState",
    "Settings",
    "build_deps",
    "build_pipeline",
    "build_pipelines",
    "build_provider_registry",
    "create_pipeline",
    "create_pipelines",
]
