"""Multi-turn coding agent loop using JSON actions."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from fusion.agent.prompts import AGENT_SYSTEM_PROMPT
from fusion.agent.schemas import AgentRunResult, AgentUsage
from fusion.agent.tools import WorkspaceToolExecutor
from fusion.agent.workspace import WorkspaceGuard
from fusion.providers.base import ModelProvider, ModelRequest
from fusion.routing.model_registry import ModelRegistry
from fusion.security.policy import SecurityPolicy
from fusion.telemetry.cost import compute_cost


def _extract_action(text: str) -> dict[str, Any] | None:
    """Parse a JSON action object from model output."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def run_coding_agent(
    *,
    task: str,
    workspace: WorkspaceGuard,
    model_alias: str,
    registry: ModelRegistry,
    providers: dict[str, ModelProvider],
    security: SecurityPolicy | None = None,
    extra_system: str = "",
    max_steps: int = 25,
) -> AgentRunResult:
    """Run a tool-using coding agent until done or step limit."""
    policy = security or SecurityPolicy.from_env()
    if not policy.allow_file_writes:
        return AgentRunResult(
            summary="Agent mode disabled",
            usage=AgentUsage(model_alias=model_alias, model_id="", provider=""),
            error="Set FUSION_AGENT_MODE=true to enable file writes and shell commands",
        )

    entry = registry.get(model_alias)
    provider = providers.get(entry.provider)
    if provider is None:
        return AgentRunResult(
            summary="Provider unavailable",
            usage=AgentUsage(
                model_alias=model_alias,
                model_id=entry.model_id,
                provider=entry.provider,
            ),
            error=f"No provider for {entry.provider}",
        )

    executor = WorkspaceToolExecutor(
        guard=workspace,
        allow_shell=policy.allow_shell_execution,
    )
    system = AGENT_SYSTEM_PROMPT + extra_system
    conversation = f"Task:\n{task}\n\nRespond with your first JSON action."

    usage = AgentUsage(
        model_alias=model_alias,
        model_id=entry.model_id,
        provider=entry.provider,
    )
    steps_log: list[str] = []
    summary = ""

    for step in range(max_steps):
        request = ModelRequest(
            model_id=entry.model_id,
            system_prompt=system,
            user_prompt=conversation,
            max_tokens=entry.max_tokens,
            temperature=0.2,
            metadata={"role": "agent"},
        )
        start = time.perf_counter()
        response = await provider.safe_complete(request)
        usage.latency_ms += (time.perf_counter() - start) * 1000
        usage.llm_calls += 1
        usage.input_tokens += response.input_tokens or 0
        usage.output_tokens += response.output_tokens or 0
        usage.cost_usd += compute_cost(response, entry)

        if response.error:
            return AgentRunResult(
                summary=summary or "Agent failed",
                files_changed=sorted(executor.files_changed),
                usage=usage,
                steps_log=steps_log,
                error=response.error,
            )

        action = _extract_action(response.text)
        if action is None:
            steps_log.append(f"step {step + 1}: invalid JSON action")
            conversation = (
                "Invalid response. Return ONE JSON action object.\n\n"
                f"Your response:\n{response.text}"
            )
            continue

        action_name = str(action.get("action", "")).lower()
        steps_log.append(f"step {step + 1}: {action_name}")
        usage.tool_calls += 1
        usage.agent_steps = step + 1

        if action_name == "done":
            summary = str(action.get("summary", "Done"))
            break

        observation = executor.execute(action)
        conversation = (
            f"Observation:\n{observation}\n\nContinue the task. Respond with the next JSON action."
        )
    else:
        summary = summary or "Agent stopped at max steps without calling done."

    usage.agent_steps = len(steps_log)
    return AgentRunResult(
        summary=summary,
        files_changed=sorted(executor.files_changed),
        usage=usage,
        steps_log=steps_log,
    )
