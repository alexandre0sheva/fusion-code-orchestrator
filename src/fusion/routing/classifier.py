"""Task classification for routing."""

from __future__ import annotations

from enum import StrEnum

# Maps MCP tool names and legacy aliases to canonical task types.
_TOOL_NAME_MAP: dict[str, str] = {
    "fusion_ask": "default",
    "fusion_review_diff": "code_review",
    "fusion_debug_error": "debugging",
    "fusion_decide_architecture": "architecture_decision",
    "fusion_plan_feature": "implementation_plan",
    "fusion_eval_answer": "answer_eval",
}

_TASK_ALIASES: dict[str, str] = {
    "architecture": "architecture_decision",
    "planning": "implementation_plan",
    "evaluation": "answer_eval",
}


class TaskType(StrEnum):
    """Supported orchestration task types."""

    CODE_REVIEW = "code_review"
    DEBUGGING = "debugging"
    ARCHITECTURE_DECISION = "architecture_decision"
    IMPLEMENTATION_PLAN = "implementation_plan"
    ANSWER_EVAL = "answer_eval"
    DEFAULT = "default"

    # Backward-compatible aliases
    ARCHITECTURE = "architecture"
    PLANNING = "planning"
    EVALUATION = "evaluation"


def normalize_task_type(value: str) -> TaskType:
    """Normalize explicit task type strings, tool names, and legacy aliases."""
    lowered = value.lower().strip()
    if lowered in _TOOL_NAME_MAP:
        lowered = _TOOL_NAME_MAP[lowered]
    if lowered in _TASK_ALIASES:
        lowered = _TASK_ALIASES[lowered]
    try:
        return TaskType(lowered)
    except ValueError:
        return TaskType.DEFAULT


def canonical_task_key(task_type: TaskType) -> str:
    """Return the routing policy key for a task type."""
    alias = _TASK_ALIASES.get(task_type.value)
    if alias:
        return alias
    if task_type.value in {t.value for t in TaskType} and task_type != TaskType.DEFAULT:
        return task_type.value
    return "default"


class TaskClassifier:
    """Classifies tasks based on explicit type, tool name, or content heuristics."""

    _KEYWORDS: dict[str, list[str]] = {
        "code_review": ["diff", "review", "pull request", "pr ", "code smell", "lint"],
        "debugging": ["error", "exception", "stack trace", "bug", "fail", "crash", "traceback"],
        "architecture_decision": [
            "architecture",
            "design",
            "trade-off",
            "decision",
            "pattern",
            "microservice",
        ],
        "implementation_plan": ["plan", "implement", "roadmap", "milestone", "feature", "rollout"],
        "answer_eval": ["evaluate", "score", "judge", "quality", "rubric"],
    }

    def classify(
        self,
        *,
        explicit_type: str | None = None,
        tool_name: str | None = None,
        content: str = "",
    ) -> TaskType:
        """Return the task type for routing."""
        if tool_name:
            return normalize_task_type(tool_name)
        if explicit_type:
            return normalize_task_type(explicit_type)

        lower = content.lower()
        scores: dict[str, int] = {}
        for task_key, keywords in self._KEYWORDS.items():
            scores[task_key] = sum(1 for kw in keywords if kw in lower)

        if not scores or max(scores.values()) == 0:
            return TaskType.DEFAULT

        best_key = max(scores, key=lambda k: scores[k])
        return normalize_task_type(best_key)

    def estimate_complexity(self, content: str) -> str:
        """Estimate task complexity from content size and structure."""
        length = len(content)
        snippet_count = content.count("```") // 2 + content.count("diff --git")
        if length > 8000 or snippet_count >= 3:
            return "high"
        if length > 2000 or snippet_count >= 1:
            return "medium"
        return "low"

    def estimate_risk(self, *, task_type: TaskType, content: str) -> str:
        """Estimate risk level from task type and content signals."""
        lower = content.lower()
        high_signals = [
            "security",
            "auth",
            "password",
            "secret",
            "sql injection",
            "production",
            "payment",
            "delete",
            "migration",
        ]
        medium_signals = ["database", "api", "concurrent", "race", "null", "exception"]

        if task_type == TaskType.CODE_REVIEW and any(s in lower for s in high_signals):
            return "high"
        if task_type == TaskType.DEBUGGING and "production" in lower:
            return "high"
        if any(s in lower for s in high_signals):
            return "high"
        if any(s in lower for s in medium_signals):
            return "medium"
        if task_type in {TaskType.ARCHITECTURE_DECISION, TaskType.IMPLEMENTATION_PLAN}:
            return "medium"
        return "low"
