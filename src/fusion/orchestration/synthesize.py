"""Synthesize panel responses into a final recommendation."""

from __future__ import annotations

from fusion.config.loader import ModelEntry
from fusion.orchestration.claims import AgreementReport, ClaimCluster, cluster_line, top_clusters
from fusion.orchestration.ledger import CallGateway, standalone_gateway
from fusion.orchestration.prompts import build_synthesis_prompt, get_role_prompt
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError
from fusion.routing.classifier import TaskType


def build_digest(
    panel_responses: list[tuple[str, str]],
    clusters: list[ClaimCluster],
    report: AgreementReport,
) -> ModelResponse:
    """The panel's answers and claim clusters as one document, for Claude Code to aggregate."""
    lines = [
        f"## Panel digest: {len(panel_responses)} answers, no synthesis model was called",
        "",
        "Read the answers below, keep what several of them agree on, and check the points "
        "where they differ before relying on them.",
    ]
    if report.low_information:
        lines.append("\nOnly one answer arrived, so there is no agreement to measure.")
    else:
        lines.append(f"\nAgreement {report.score:.2f} across {report.n_models} models.")
    sections = (
        ("Shared by most models", report.consensus),
        ("Severity disputed", report.contradicted),
        ("Raised by one model", report.unique),
    )
    by_id = {c.id: c for c in clusters}
    for title, ids in sections:
        if not ids:
            continue
        lines.extend(["", f"### {title}"])
        for cluster in top_clusters((by_id[i] for i in ids), limit=1000):
            lines.append(f"- {cluster_line(cluster)}")
    for model_name, content in panel_responses:
        lines.extend(["", f"### Answer from {model_name}", content.strip()])
    return ModelResponse(provider="fusion", model="digest", text="\n".join(lines))


async def synthesize_responses(
    *,
    synthesizer_model: str,
    registry_models: dict[str, ModelEntry],
    providers: dict[str, ModelProvider],
    task_type: TaskType,
    panel_responses: list[tuple[str, str]],
    disagreement_analysis: dict[str, object],
    original_task: str = "",
    gateway: CallGateway | None = None,
    clusters: list[ClaimCluster] | None = None,
) -> ModelResponse:
    """Call synthesizer model to merge panel responses into structured JSON."""
    entry = registry_models[synthesizer_model]
    gateway = gateway or standalone_gateway(registry_models, providers)

    user_prompt = build_synthesis_prompt(
        task_type=task_type,
        panel_responses=panel_responses,
        disagreement_analysis=disagreement_analysis,
        original_task=original_task,
        clusters=clusters,
    )
    request = ModelRequest(
        model_id=entry.model_id,
        system_prompt=get_role_prompt("synthesizer"),
        user_prompt=user_prompt,
        max_tokens=entry.max_tokens,
        json_mode=entry.supports_json,
        metadata={
            "task_type": task_type.value,
            "role": "synthesizer",
            "personality": "synthesizer",
        },
    )
    response = await gateway.call(stage="synthesis", alias=synthesizer_model, request=request)
    if response.error and response.error_type != "MissingProvider":
        raise ProviderError(response.error)
    return response
