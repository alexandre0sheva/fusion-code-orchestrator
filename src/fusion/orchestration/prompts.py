"""Prompt templates for orchestration tasks."""

from __future__ import annotations

import json

from fusion.routing.classifier import TaskType, canonical_task_key

_STRUCTURED_OUTPUT_RULES = """
Rules for your response:
- Use ONLY the context provided below; do not invent files, APIs, or behaviors not in context.
- State assumptions explicitly when information is missing.
- Separate facts (from context) from recommendations (your judgment).
- Identify uncertainty and caveats; do not overstate confidence.
- For coding tasks, include a test strategy and risk notes.
- Return valid JSON matching the requested schema exactly.
"""

_ROLE_PROMPTS: dict[str, str] = {
    "coding_reviewer": (
        "You are a senior software engineer performing a thorough code review. "
        "Identify bugs, maintainability issues, and missing tests. Be specific and actionable."
    ),
    "security_reviewer": (
        "You are a security-focused code reviewer. Identify vulnerabilities, "
        "injection risks, auth flaws, secret exposure, and unsafe defaults."
    ),
    "performance_reviewer": (
        "You are a performance engineer reviewing code changes. "
        "Identify bottlenecks, N+1 queries, memory leaks, and scalability concerns."
    ),
    "debugging_hypothesis": (
        "You are an expert debugger. Generate ranked hypotheses for the root cause "
        "with verification steps and a minimal fix strategy."
    ),
    "architecture_advisor": (
        "You are a software architect. Evaluate trade-offs, recommend an approach, "
        "and document rejected alternatives with risks and reversibility."
    ),
    "implementation_planner": (
        "You are a technical lead creating an implementation plan. "
        "Break work into phases with affected modules, API changes, and tests to add."
    ),
    "judge": (
        "You are an evaluation judge. Score responses on 0.0-1.0 dimensions. "
        "Return only valid JSON with scores and brief reasons."
    ),
    "synthesizer": (
        "You are a synthesis expert. Merge panel responses into a unified, "
        "action-ready recommendation. Preserve caveats and do not add unsupported claims."
    ),
}

_TASK_SYSTEM_PROMPTS: dict[str, str] = {
    "code_review": _ROLE_PROMPTS["coding_reviewer"],
    "debugging": _ROLE_PROMPTS["debugging_hypothesis"],
    "architecture_decision": _ROLE_PROMPTS["architecture_advisor"],
    "implementation_plan": _ROLE_PROMPTS["implementation_planner"],
    "answer_eval": _ROLE_PROMPTS["judge"],
    "default": (
        "You are a senior software engineer answering as a practical coding model. "
        "Give Claude Code an answer it can directly use to inspect, edit, test, or decide."
    ),
}

_SYNTHESIS_SCHEMAS: dict[str, dict[str, str]] = {
    "code_review": {
        "summary": "string — executive summary",
        "critical_findings": "list[string]",
        "recommended_changes": "list[string]",
        "false_positive_risks": "list[string]",
        "test_plan": "list[string]",
        "consensus": "list[string]",
        "disagreements": "list[string]",
        "unique_insights": "list[string]",
        "confidence": "float 0-1",
    },
    "debugging": {
        "most_likely_causes": "list[string]",
        "ranked_hypotheses": "list[{hypothesis, confidence, evidence}]",
        "verification_steps": "list[string]",
        "minimal_fix_strategy": "string",
        "what_not_to_do": "list[string]",
        "confidence": "float 0-1",
    },
    "architecture_decision": {
        "recommended_option": "string",
        "tradeoffs": "list[string]",
        "rejected_options": "list[string]",
        "risks": "list[string]",
        "reversibility": "string",
        "migration_plan": "list[string]",
        "test_strategy": "list[string]",
        "confidence": "float 0-1",
    },
    "implementation_plan": {
        "implementation_sequence": "list[string]",
        "affected_modules": "list[string]",
        "data_model_changes": "list[string]",
        "api_changes": "list[string]",
        "ui_changes": "list[string]",
        "tests_to_add": "list[string]",
        "risks": "list[string]",
        "open_questions": "list[string]",
        "confidence": "float 0-1",
    },
    "answer_eval": {
        "score": "float 0-1",
        "strengths": "list[string]",
        "weaknesses": "list[string]",
        "unsupported_claims": "list[string]",
        "missing_points": "list[string]",
        "safer_answer": "string",
        "confidence": "float 0-1",
    },
    "default": {
        "answer": "string — direct answer for Claude Code to use",
        "summary": "string — one sentence summary",
        "suggested_actions": "list[string]",
        "tests_to_run": "list[string]",
        "risks": "list[string]",
        "assumptions": "list[string]",
        "confidence": "float 0-1",
    },
}


def get_role_prompt(role: str) -> str:
    """Return the system prompt for a panel role."""
    base = _ROLE_PROMPTS.get(role, _ROLE_PROMPTS["coding_reviewer"])
    return f"{base}\n{_STRUCTURED_OUTPUT_RULES}"


def get_system_prompt(task_type: TaskType, *, role: str | None = None) -> str:
    """Return the system prompt for a task type or explicit role."""
    if role and role in _ROLE_PROMPTS:
        return get_role_prompt(role)
    key = canonical_task_key(task_type)
    base = _TASK_SYSTEM_PROMPTS.get(key, _TASK_SYSTEM_PROMPTS["default"])
    return f"{base}\n{_STRUCTURED_OUTPUT_RULES}"


def build_user_prompt(
    *,
    task_type: TaskType,
    primary_content: str,
    context: str = "",
    file_snippets: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> str:
    """Build the user prompt from input components."""
    parts = [f"## Task: {canonical_task_key(task_type)}\n", primary_content]
    if context:
        parts.append(f"\n## Additional Context\n{context}")
    files = changed_files or []
    if files:
        parts.append("\n## Changed Files\n" + "\n".join(f"- {f}" for f in files))
    snippets = file_snippets or []
    if snippets:
        parts.append("\n## File Snippets")
        for i, snippet in enumerate(snippets, 1):
            parts.append(f"\n### Snippet {i}\n{snippet}")
    parts.append(
        "\n## Output Format\nRespond with structured analysis. "
        "Include test strategy and risk notes for coding tasks."
    )
    return "\n".join(parts)


def build_synthesis_prompt(
    *,
    task_type: TaskType,
    panel_responses: list[tuple[str, str]],
    disagreement_analysis: dict[str, object],
    original_task: str = "",
) -> str:
    """Build prompt for synthesizing panel responses into structured JSON."""
    key = canonical_task_key(task_type)
    schema = _SYNTHESIS_SCHEMAS.get(key, {"summary": "string", "confidence": "float 0-1"})
    parts = [
        f"Synthesize the following {key} panel responses into a single JSON object.\n",
        f"Disagreement analysis: {json.dumps(disagreement_analysis, default=str)[:2000]}\n",
    ]
    if original_task:
        parts.append(f"\n## Original Task\n{original_task[:3000]}\n")
    for model_name, content in panel_responses:
        parts.append(f"\n## Response from {model_name}\n{content[:4000]}")
    parts.append(f"\n## Required JSON Schema\n{json.dumps(schema, indent=2)}")
    parts.append(_STRUCTURED_OUTPUT_RULES)
    parts.append("\nReturn ONLY valid JSON matching the schema above.")
    return "\n".join(parts)


def build_refinement_prompt(
    *,
    task_type: TaskType,
    original_task: str,
    own_answer: str,
    peer_answers: list[tuple[str, str]],
) -> str:
    """Build the mixture-of-agents refinement prompt for one panel model.

    Peer answers are anonymized as "Response A", "Response B", ... so the model
    judges content, not reputation.
    """
    key = canonical_task_key(task_type)
    parts = [
        f"You previously answered a {key} task. Below are anonymized answers from "
        "other expert models to the same task, together with your own answer.",
        "Critique all answers, adopt correct points you missed, and discard mistakes. "
        "Then produce a single improved final answer.",
        f"\n## Original Task\n{original_task[:6000]}",
        f"\n## Your Answer\n{own_answer[:4000]}",
    ]
    for label, content in peer_answers:
        parts.append(f"\n## Response {label}\n{content[:4000]}")
    parts.append(
        "\n## Refinement Instructions\n"
        "- Keep everything correct from your answer; integrate insights you missed.\n"
        "- Drop claims a peer convincingly contradicts unless you have strong evidence.\n"
        "- Do not mention the other responses or this refinement process.\n"
        "- Return the complete improved answer in the same structured format as before."
    )
    parts.append(_STRUCTURED_OUTPUT_RULES)
    return "\n".join(parts)


def build_judge_prompt(
    *,
    response_content: str,
    task_type: str,
    context: str = "",
) -> str:
    """Build prompt for LLM judge evaluation."""
    return (
        f"Evaluate this {task_type} response on a 0.0-1.0 scale for each dimension.\n"
        "Return JSON with keys: specificity, groundedness, actionability, "
        "correctness_likelihood, risk_awareness, unsupported_claims (lower=better), "
        "codebase_awareness, novelty, overall_score, notes.\n\n"
        f"Context:\n{context[:2000]}\n\nResponse:\n{response_content[:4000]}"
    )
