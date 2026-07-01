"""Mock provider for tests and offline development."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any, cast

from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse

MOCK_PERSONALITIES = frozenset(
    {
        "coding_reviewer",
        "coding_agent",
        "security_reviewer",
        "debugging_hypothesis",
        "architecture_advisor",
        "weak_model",
        "judge",
        "synthesizer",
    }
)


class MockProvider(ModelProvider):
    """Deterministic mock provider with configurable personalities."""

    name = "mock"

    def __init__(self, latency_ms: float = 10.0) -> None:
        self._latency_ms = latency_ms

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(self._latency_ms / 1000.0)
        personality = self._resolve_personality(request)
        text = self._generate_content(request, personality)
        parsed_json = self._maybe_parse_json(text, personality)
        prompt_text = request.user_prompt or " ".join(m.content for m in request.messages)
        system_text = request.system_prompt
        input_tokens = len(system_text.split()) + len(prompt_text.split())
        output_tokens = len(text.split())
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=text,
            parsed_json=parsed_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate_usd=0.0,
            latency_ms=self._latency_ms,
            finish_reason="stop",
            raw_response={"mock": True, "personality": personality},
        )

    def _resolve_personality(self, request: ModelRequest) -> str:
        explicit = request.metadata.get("personality") or request.metadata.get("role")
        if explicit in MOCK_PERSONALITIES:
            return str(explicit)
        if request.metadata.get("role") == "agent":
            return "coding_agent"
        model_id = request.model_id.lower()
        if "judge" in model_id:
            return "judge"
        if "synth" in model_id:
            return "synthesizer"
        if "security" in model_id:
            return "security_reviewer"
        if "weak" in model_id:
            return "weak_model"
        if "debug" in model_id:
            return "debugging_hypothesis"
        if "arch" in model_id:
            return "architecture_advisor"
        task = request.metadata.get("task_type", "general")
        task_map = {
            "code_review": "coding_reviewer",
            "debugging": "debugging_hypothesis",
            "architecture": "architecture_advisor",
            "architecture_decision": "architecture_advisor",
            "planning": "architecture_advisor",
            "implementation_plan": "architecture_advisor",
            "evaluation": "judge",
            "answer_eval": "judge",
        }
        return task_map.get(task, "coding_reviewer")

    def _seed(self, request: ModelRequest) -> str:
        prompt = request.user_prompt or " ".join(m.content for m in request.messages)
        return hashlib.sha256(prompt.encode()).hexdigest()[:8]

    def _generate_content(self, request: ModelRequest, personality: str) -> str:
        seed = self._seed(request)
        if personality == "synthesizer":
            return self._synthesizer(seed, request)
        generators: dict[str, Callable[[str], str]] = {
            "coding_reviewer": self._coding_reviewer,
            "coding_agent": self._coding_agent,
            "security_reviewer": self._security_reviewer,
            "debugging_hypothesis": self._debugging_hypothesis,
            "architecture_advisor": self._architecture_advisor,
            "weak_model": self._weak_model,
            "judge": self._judge,
        }
        generator = generators.get(personality, self._coding_reviewer)
        return generator(seed)

    def _maybe_parse_json(self, text: str, personality: str) -> dict[str, Any] | None:
        if personality not in {"judge", "synthesizer"}:
            return None
        try:
            return cast(dict[str, Any], json.loads(text))
        except json.JSONDecodeError:
            return None

    def _coding_agent(self, seed: str) -> str:
        return json.dumps(
            {
                "action": "done",
                "summary": f"Mock agent implemented task (seed:{seed})",
            }
        )

    def _coding_reviewer(self, seed: str) -> str:
        return (
            f"## Code Review (mock:{seed})\n\n"
            "**Findings:**\n"
            "1. Consider adding error handling around the database call.\n"
            "2. The function lacks type hints on return value.\n"
            "3. Missing unit test for edge case when input is empty.\n\n"
            "**Risk:** Medium — null pointer possible on line referenced in diff.\n"
            "**Recommendation:** Add guard clause and test coverage."
        )

    def _security_reviewer(self, seed: str) -> str:
        return (
            f"## Security Review (mock:{seed})\n\n"
            "**Findings:**\n"
            "1. Potential SQL injection via string concatenation.\n"
            "2. Missing input validation on user-controlled path.\n"
            "3. Secrets may be logged in debug output.\n\n"
            "**Risk:** High — exploitable in production without sanitization.\n"
            "**Recommendation:** Parameterize queries and redact sensitive fields."
        )

    def _debugging_hypothesis(self, seed: str) -> str:
        return (
            f"## Debug Analysis (mock:{seed})\n\n"
            "**Root cause hypothesis:** Connection pool exhaustion under load.\n"
            "**Evidence:** Stack trace shows timeout in pool.acquire().\n"
            "**Fix steps:**\n"
            "1. Increase pool size configuration.\n"
            "2. Add connection timeout logging.\n"
            "3. Verify connections are returned in finally blocks."
        )

    def _architecture_advisor(self, seed: str) -> str:
        return (
            f"## Architecture Decision (mock:{seed})\n\n"
            "**Recommendation:** Use event-driven pattern with message queue.\n"
            "**Rationale:** Decouples services, improves scalability.\n"
            "**Trade-offs:** Added operational complexity vs. better isolation.\n"
            "**Alternatives considered:** Direct HTTP calls, shared database."
        )

    def _weak_model(self, seed: str) -> str:
        return f"Looks fine to me. (mock:{seed})"

    def _judge(self, seed: str) -> str:
        return json.dumps(
            {
                "specificity": 0.8,
                "groundedness": 0.75,
                "actionability": 0.85,
                "correctness_likelihood": 0.7,
                "risk_awareness": 0.8,
                "unsupported_claims": 0.1,
                "codebase_awareness": 0.7,
                "novelty": 0.5,
                "overall_score": 0.76,
                "notes": f"mock judge {seed}",
            }
        )

    def _synthesizer(self, seed: str, request: ModelRequest) -> str:
        task = request.metadata.get("task_type", "code_review")
        task_map = {
            "code_review": {
                "summary": f"Panel consensus on code quality (mock:{seed})",
                "critical_findings": [
                    "Missing error handling around database call",
                    "Potential null pointer on diff line",
                ],
                "recommended_changes": [
                    "Add guard clause for empty input",
                    "Add type hints on return value",
                    "Add unit test for edge case",
                ],
                "false_positive_risks": ["Type hint suggestion may be stylistic only"],
                "test_plan": [
                    "Unit test empty input path",
                    "Integration test database error handling",
                ],
                "consensus": ["Add error handling"],
                "disagreements": ["Severity of null pointer risk"],
                "unique_insights": ["Security reviewer flagged SQL injection pattern"],
                "confidence": 0.72,
            },
            "debugging": {
                "most_likely_causes": ["Connection pool exhaustion under load"],
                "ranked_hypotheses": [
                    {
                        "hypothesis": "Pool size too small",
                        "confidence": 0.8,
                        "evidence": "timeout in acquire()",
                    },
                    {
                        "hypothesis": "Connection leak",
                        "confidence": 0.5,
                        "evidence": "missing finally block",
                    },
                ],
                "verification_steps": [
                    "Check pool metrics under load",
                    "Audit connection return paths",
                ],
                "minimal_fix_strategy": "Increase pool size and add connection timeout logging",
                "what_not_to_do": ["Do not restart production without checking pool metrics first"],
                "confidence": 0.68,
            },
            "architecture_decision": {
                "recommended_option": "Event-driven pattern with message queue",
                "tradeoffs": ["Added operational complexity", "Better service isolation"],
                "rejected_options": ["Direct HTTP calls — too tightly coupled"],
                "risks": ["Queue operational overhead", "Message ordering complexity"],
                "reversibility": "Medium — can migrate back with adapter layer",
                "migration_plan": [
                    "Phase 1: Add queue alongside existing HTTP",
                    "Phase 2: Migrate writers",
                ],
                "test_strategy": ["Contract tests between services", "Chaos test queue failures"],
                "confidence": 0.7,
            },
            "architecture": {
                "recommended_option": "Event-driven pattern with message queue",
                "tradeoffs": ["Added operational complexity"],
                "rejected_options": ["Direct HTTP calls"],
                "risks": ["Queue operational overhead"],
                "reversibility": "Medium",
                "migration_plan": ["Phase 1: Add queue"],
                "test_strategy": ["Contract tests"],
                "confidence": 0.7,
            },
            "implementation_plan": {
                "implementation_sequence": [
                    "Define data model changes",
                    "Implement API endpoints",
                    "Add UI components",
                    "Write tests",
                ],
                "affected_modules": ["api/routes", "models/user", "ui/settings"],
                "data_model_changes": ["Add oauth_token column to users table"],
                "api_changes": ["POST /auth/oauth/callback endpoint"],
                "ui_changes": ["OAuth login button on sign-in page"],
                "tests_to_add": ["OAuth callback integration test", "Token refresh unit test"],
                "risks": ["Token storage security", "Third-party OAuth downtime"],
                "open_questions": ["Which OAuth providers to support initially?"],
                "confidence": 0.75,
            },
            "planning": {
                "implementation_sequence": ["Define model", "Implement API", "Add tests"],
                "affected_modules": ["api", "models"],
                "data_model_changes": ["New oauth fields"],
                "api_changes": ["OAuth callback endpoint"],
                "ui_changes": ["Login button"],
                "tests_to_add": ["Integration test"],
                "risks": ["Token security"],
                "open_questions": ["Provider selection"],
                "confidence": 0.75,
            },
            "answer_eval": {
                "score": 0.76,
                "strengths": ["Clear structure", "Actionable recommendations"],
                "weaknesses": ["Missing edge case coverage"],
                "unsupported_claims": ["Claim about file auth.py not in context"],
                "missing_points": ["No rollback strategy mentioned"],
                "safer_answer": (
                    "Recommend adding tests before deploying; verify auth.py exists in repo"
                ),
                "confidence": 0.7,
            },
            "evaluation": {
                "score": 0.76,
                "strengths": ["Clear structure"],
                "weaknesses": ["Missing edge cases"],
                "unsupported_claims": [],
                "missing_points": ["No rollback strategy"],
                "safer_answer": "Add verification steps before deployment",
                "confidence": 0.7,
            },
        }
        payload = task_map.get(str(task), task_map["code_review"])
        return json.dumps(payload)

    @staticmethod
    def create_registry() -> dict[str, MockProvider]:
        return {"mock": MockProvider()}
