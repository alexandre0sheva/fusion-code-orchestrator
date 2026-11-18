"""Prompt templates for orchestration tasks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple

from fusion.config.prompt_pack import PROMPT_SPECS, active_pack
from fusion.routing.classifier import TaskType, canonical_task_key
from fusion.security.untrusted import UNTRUSTED_RULES, wrap_untrusted

if TYPE_CHECKING:
    from fusion.orchestration.approaches import Approach
    from fusion.orchestration.claims import ClaimCluster
    from fusion.orchestration.lenses import Lens

# The prompts tuning may edit live in the prompt pack (``config/prompts/*.md``, read through
# ``active_pack``); this module adds the structure around them.


def _rules() -> str:
    """The rules every system prompt ends with (pack prompt ``rules``)."""
    return "\n" + active_pack().render("rules", untrusted_rules=UNTRUSTED_RULES) + "\n"


def _claim_rules() -> str:
    """The panel's answer format (pack prompt ``claims``)."""
    return "\n" + active_pack().text("claims") + "\n"


# Task types whose main input is material to analyse (a diff, an error, an answer to grade), not
# the caller's own instruction. An ask, an architecture question or a feature request is the
# instruction itself and is left outside the delimiters.
_MATERIAL_TASKS = frozenset({"code_review", "debugging", "answer_eval"})

_ROLES = tuple(sorted(n.removeprefix("role.") for n in PROMPT_SPECS if n.startswith("role.")))
_DEFAULT_ROLE = "coding_reviewer"

# The system prompt of each task type: a role's prompt, or ``task.default``.
_TASK_SYSTEM_PROMPTS: dict[str, str] = {
    "code_review": "role.coding_reviewer",
    "debugging": "role.debugging_hypothesis",
    "architecture_decision": "role.architecture_advisor",
    "implementation_plan": "role.implementation_planner",
    "answer_eval": "role.judge",
    "default": "task.default",
}


def _role_text(role: str) -> str:
    return active_pack().text(f"role.{role if role in _ROLES else _DEFAULT_ROLE}")


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


def role_names() -> frozenset[str]:
    """Names accepted for a panel member's ``role``."""
    return frozenset(_ROLES)


def get_role_prompt(role: str) -> str:
    """Return the system prompt for a panel role."""
    return f"{_role_text(role)}\n{_rules()}"


def get_system_prompt(task_type: TaskType, *, role: str | None = None) -> str:
    """Return the system prompt for a task type or explicit role."""
    if role and role in _ROLES:
        return get_role_prompt(role)
    key = canonical_task_key(task_type)
    name = _TASK_SYSTEM_PROMPTS.get(key, _TASK_SYSTEM_PROMPTS["default"])
    return f"{active_pack().text(name)}\n{_rules()}"


class UserPrompt(NamedTuple):
    """A user prompt and how much of its start is shared with other calls.

    ``shared_prefix`` counts the characters at the start that depend only on the task type and the
    files (not on the request, the context or the member), so another call about the same files
    starts with the same text and a provider's prompt cache can serve it; 0 when no files were
    given.
    """

    text: str
    shared_prefix: int


def compose_user_prompt(
    *,
    task_type: TaskType,
    primary_content: str,
    context: str = "",
    file_snippets: list[str] | None = None,
    changed_files: list[str] | None = None,
    claims: bool = True,
    lessons: str = "",
) -> UserPrompt:
    """Build the user prompt from input components, stable material first.

    The files come first and the request near the end: long material before the question is what
    providers recommend, and a prefix that stays the same across calls is what their prompt
    caches reuse. ``claims`` asks for the panel's claim format; the shadow baseline gets the task
    alone, in free form, so its answer can be compared as plain text. ``lessons`` (the experience
    library's block, already bounded) goes after the context, outside the shared prefix.
    """
    key = canonical_task_key(task_type)
    material = key in _MATERIAL_TASKS
    primary = wrap_untrusted(primary_content) if material else primary_content
    head = [f"## Task: {key}\n"]
    snippets = file_snippets or []
    if snippets:
        head.append("## File Snippets")
        for i, snippet in enumerate(snippets, 1):
            head.append(f"\n### Snippet {i}\n{wrap_untrusted(snippet)}")
    rest: list[str] = []
    if context:
        rest.append(f"\n## Additional Context\n{wrap_untrusted(context)}")
    files = changed_files or []
    if files:
        listing = "\n".join(f"- {f}" for f in files)
        rest.append(f"\n## Changed Files\n{wrap_untrusted(listing)}")
    if lessons:
        rest.append(f"\n## Lessons From Earlier Runs\n{wrap_untrusted(lessons)}")
    rest.append(f"\n## {'Input' if material else 'Request'}\n{primary}")
    rest.append(
        _claim_rules()
        if claims
        else "\n## Output Format\nRespond with structured analysis. "
        "Include test strategy and risk notes for coding tasks."
    )
    prefix = "\n".join(head) + "\n"
    return UserPrompt(prefix + "\n".join(rest), len(prefix) if snippets else 0)


def build_user_prompt(
    *,
    task_type: TaskType,
    primary_content: str,
    context: str = "",
    file_snippets: list[str] | None = None,
    changed_files: list[str] | None = None,
    claims: bool = True,
) -> str:
    """The user prompt's text (see ``compose_user_prompt``)."""
    return compose_user_prompt(
        task_type=task_type,
        primary_content=primary_content,
        context=context,
        file_snippets=file_snippets,
        changed_files=changed_files,
        claims=claims,
    ).text


def role_instruction(role: str) -> str:
    """A panel member's role, appended after the shared prompt so every member sends the same
    system prompt and prompt prefix."""
    return f"\n\n## Your Role\n{_role_text(role)}"


def lens_instruction(lens: Lens, others: list[Lens] | None = None) -> str:
    """A panel member's lens, appended after the shared prompt like a role.

    ``others`` are the lenses the rest of the panel holds, named so the member knows what to
    leave to them (the ownership boundary).
    """
    lines = [
        f"\n\n## Your Lens: {lens.name}",
        "The panel splits this task by area: you cover one, other panelists cover the rest. You "
        "see the whole input, but report only what falls in your area.",
        lens.instruction.strip(),
        "Your area:",
        *(f"- {area}" for area in lens.owns),
    ]
    other_areas = [o for o in others or [] if o.name != lens.name]
    leave = [*lens.ignores, *(f"{', '.join(o.owns)} ({o.name} lens)" for o in other_areas)]
    if leave:
        lines.append("Leave to others, do not report:")
        lines.extend(f"- {item}" for item in leave)
    lines.append(
        "If nothing in your area needs reporting, set `none_found` to true and return an empty "
        "`claims` list. That is a complete answer: never invent a finding to fill the list."
    )
    return "\n".join(lines)


def approach_instruction(approach: Approach) -> str:
    """A panel member's approach, appended after the shared prompt like a role or a lens.

    Unlike a lens it covers nothing less than the whole task: the answers are rivals, and one of
    them will be picked as it stands.
    """
    return "\n".join(
        [
            f"\n\n## Your Approach: {approach.name}",
            "Other panelists answer the same task from other stances. Answer the WHOLE task: "
            "your answer must be a complete answer that stands alone, because one candidate "
            "answer will be picked as it is and the others discarded.",
            approach.instruction.strip(),
            "Stay within the supplied material; where it says nothing, say so instead of "
            "inventing specifics.",
        ]
    )


def self_check_instruction() -> str:
    """Rung 0 of strategy auto: one model answers alone, so it checks its own answer and its
    confidence decides whether a stronger setup is asked (``routing.escalation.triggers``)."""
    return "\n\n## Self-check\n" + active_pack().text("self_check")


_PATCH_FIELD = (
    "string — ONE patch for the whole change: a unified diff, or the complete new content of each "
    "changed file under `=== path ===` lines. Build it from the patches in the responses (take the "
    "best one, or combine them into one that applies to the original files); never several"
)


def build_synthesis_prompt(
    *,
    task_type: TaskType,
    panel_responses: list[tuple[str, str]],
    disagreement_analysis: dict[str, object],
    original_task: str = "",
    clusters: list[ClaimCluster] | None = None,
    patch: bool = False,
    lensed: bool = False,
) -> str:
    """Build prompt for synthesizing panel responses into structured JSON.

    ``clusters`` (the panel's claims grouped across models) tell the synthesizer which points are
    shared, which stand alone and which are disputed; the full answers follow for detail.
    ``lensed``: each response covered one area (its lens), so a point from one response is not
    suspect for that reason alone.
    """
    key = canonical_task_key(task_type)
    schema = _SYNTHESIS_SCHEMAS.get(key, {"summary": "string", "confidence": "float 0-1"})
    if patch:  # the task asks for a code change: the merged answer is one patch
        schema = {**schema, "patch": _PATCH_FIELD}
    parts = [f"Synthesize the following {key} panel responses into a single JSON object.\n"]
    if clusters:
        parts.append(_render_clusters(clusters))
    parts.append(f"Agreement summary: {json.dumps(disagreement_analysis, default=str)}\n")
    if original_task:
        parts.append(f"\n## Original Task\n{wrap_untrusted(original_task)}\n")
    for model_name, content in panel_responses:
        parts.append(f"\n## Response from {model_name}\n{wrap_untrusted(content)}")
    parts.append(f"\n## Required JSON Schema\n{json.dumps(schema, indent=2)}")
    parts.append(_rules())
    if lensed:
        parts.append(
            "\nEach response covered one area (its lens, named in its heading), so most points "
            "come from one response by design: add them together. Drop a point only when it is "
            "not supported by the original task or another response contradicts it; state "
            "disputed points as disputed. Return ONLY valid JSON matching the schema above."
        )
    else:
        parts.append(
            "\nFavor claims several models share; keep a point only one model raised when it is "
            "specific and supported, and say so; state disputed points as disputed. "
            "Return ONLY valid JSON matching the schema above."
        )
    return "\n".join(parts)


def _render_clusters(clusters: list[ClaimCluster]) -> str:
    lines = ["## Claim clusters (the same point, grouped across models)"]
    for cluster in clusters:
        where = f" {cluster.file}:{cluster.line}" if cluster.file and cluster.line else ""
        severity = f" {cluster.severity}" if cluster.severity else ""
        lines.append(
            f"- [{cluster.id}] {cluster.status}, {cluster.support} model(s) "
            f"({', '.join(cluster.models)}), {cluster.kind}{severity}{where}: {cluster.text}"
        )
    return "\n".join(lines[:1] + [wrap_untrusted("\n".join(lines[1:]))]) + "\n"


def build_refinement_prompt(
    *,
    task_type: TaskType,
    original_task: str,
    own_answer: str,
    peer_answers: list[tuple[str, str]],
    lens: Lens | None = None,
    approach: Approach | None = None,
) -> str:
    """Build the mixture-of-agents refinement prompt for one panel model.

    Peer answers are anonymized as "Response A", "Response B", ... so the model
    judges content, not reputation. A model with a ``lens`` revises within its area only; one
    with an ``approach`` keeps answering the whole task from that stance.
    """
    key = canonical_task_key(task_type)
    parts = [
        f"You previously answered a {key} task. Below are anonymized answers from "
        "other expert models to the same task, together with your own answer.",
        "Critique all answers, adopt correct points you missed, and discard mistakes. "
        "Then produce a single improved final answer.",
        f"\n## Original Task\n{wrap_untrusted(original_task)}",
        f"\n## Your Answer\n{wrap_untrusted(own_answer)}",
    ]
    for label, content in peer_answers:
        parts.append(f"\n## Response {label}\n{wrap_untrusted(content)}")
    if lens is not None:
        parts.append(
            f"\n## Your Lens: {lens.name}\nYou cover only this area: {'; '.join(lens.owns)}. "
            "Use the other responses to check and sharpen your points in this area; leave the "
            "other areas to them. If nothing in your area needs reporting, set `none_found` to "
            "true and return an empty `claims` list."
        )
    if approach is not None:
        parts.append(
            f"\n## Your Approach: {approach.name}\n{approach.instruction.strip()} Keep "
            "answering the whole task from this stance: adopt what the other responses got "
            "right, but do not turn your answer into theirs."
        )
    parts.append(
        "\n## Refinement Instructions\n"
        "- Keep everything correct from your answer; integrate insights you missed.\n"
        "- Drop claims a peer convincingly contradicts unless you have strong evidence.\n"
        "- Do not mention the other responses or this refinement process.\n"
        "- Return the complete improved answer as the same JSON object as before "
        "(summary and claims)."
    )
    parts.append(_rules())
    return "\n".join(parts)


UNION_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["confirmed", "rejected", "unsure"]},
                    "quote": {"type": ["string", "null"]},
                },
                "required": ["id", "verdict", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def build_union_check_prompt(
    *, task_type: TaskType, material: str, findings: list[tuple[str, str]]
) -> str:
    """Ask one cheap model whether each finding is supported by the input.

    ``findings`` are ``(id, line)`` pairs for the findings that cited no quote the union could
    check. A confirmation counts only with an exact quote, which is checked again by string match.
    """
    key = canonical_task_key(task_type)
    listing = "\n".join(f"- [{fid}] {line}" for fid, line in findings)
    return "\n".join(
        [
            f"Panelists reviewing a {key} task reported the findings below without quoting the "
            "input. Check each one against the input only.",
            UNTRUSTED_RULES,
            f"\n## Input\n{wrap_untrusted(material)}",
            f"\n## Findings to check\n{wrap_untrusted(listing)}",
            "\n## Output\nReturn JSON: {\"verdicts\": [{\"id\", \"verdict\", \"quote\"}]}, one per "
            "finding. `confirmed`: the input shows the problem; `quote` is the exact text, copied "
            "character for character, that shows it. `rejected`: the input contradicts it or it "
            "is about something the input does not contain. `unsure`: the input cannot settle "
            "it. Keep it short; do not explain.",
        ]
    )


SELECT_OUTPUT_LINES = 6


def select_schema(*, with_missing: bool) -> dict[str, object]:
    """The selector's JSON answer: why (a few lines), the chosen id, the runner-up and, for
    ``select+patch``, the points a candidate lacks."""
    properties: dict[str, object] = {
        "why": {"type": "string"},
        "acceptable": {"type": "boolean"},
        "choice": {"type": "string"},
        "runner_up": {"type": ["string", "null"]},
    }
    if with_missing:
        properties["missing"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"from": {"type": "string"}, "point": {"type": "string"}},
                "required": ["from", "point"],
                "additionalProperties": False,
            },
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def build_select_prompt(
    *,
    task_type: TaskType,
    material: str,
    candidates: list[tuple[str, str]],
    patch: bool = False,
    patch_points: int = 0,
) -> str:
    """Ask one cheap model to pick the best of several whole answers to one task.

    ``candidates`` are ``(id, text)`` pairs in the order to show them (anonymous: no model, vendor
    or approach appears). The criteria are applied in order, as in Harness²'s ``select()``; the
    answer is an id plus a few lines of why. ``patch_points`` > 0 also asks for up to that many
    short points that other candidates cover and the chosen one lacks (``select+patch``).
    """
    key = canonical_task_key(task_type)
    parts = [
        f"Several candidate answers to one {key} task follow. Each is a complete answer to the "
        "whole task. Pick the single best one. The candidates are anonymous and their order "
        "means nothing.",
        UNTRUSTED_RULES,
        f"\n## Task\n{wrap_untrusted(material)}",
    ]
    for cid, text in candidates:
        parts.append(f"\n## Candidate {cid}\n{wrap_untrusted(text)}")
    shape = (
        "one patch that applies to the files shown (a unified diff, or the full new content of "
        "each file); several patches, or a patch for files the task never names, do not conform"
        if patch
        else "the shape the task asks for (a plan, a decision with its reasons, an answer)"
    )
    parts.append("\n## How to decide\n" + active_pack().render("select", shape=shape))
    output = [
        "\n## Output",
        f"Return JSON: `why` (at most {SELECT_OUTPUT_LINES} short lines: the criterion that "
        "decided it, and what the runner-up lacked), `choice` (the id of the best candidate, "
        'for example "c2"), `runner_up` (the id of the second best, or null), and '
        "`acceptable`: false only when no candidate is good enough to hand over (none delivers "
        "what was asked, or every one rests on invented specifics); still name the least bad "
        "one as `choice`.",
    ]
    if patch_points > 0:
        output.append(
            f"Also `missing`: at most {patch_points} short points (one sentence each) that "
            "another candidate covers, that are supported by the task, and that the chosen "
            'candidate lacks, as {"from": id of that candidate, "point": the point}. Use an '
            "empty list when the chosen candidate misses nothing that matters. Do not rewrite "
            "the chosen answer."
        )
    parts.extend(output)
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
        f"{UNTRUSTED_RULES}\n\n"
        f"Context:\n{wrap_untrusted(context)}\n\nResponse:\n{wrap_untrusted(response_content)}"
    )
