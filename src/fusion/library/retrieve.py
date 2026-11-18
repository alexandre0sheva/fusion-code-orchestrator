"""Find the lessons that fit a new request and render them, bounded, for the panel prompt.

Matching is lexical and free (no model call): a lesson qualifies when its cue or text shares
words with the request, and ranks by that overlap (the cue counts double) times its confidence.
The rendered block calls them hypotheses to re-check, never facts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, Field

from fusion.library.store import Confidence, Lesson, LibraryStore
from fusion.routing.budget import CHARS_PER_TOKEN

DEFAULT_MAX_TOKENS = 300
_WEIGHT: dict[Confidence, float] = {"high": 1.0, "medium": 0.7, "low": 0.4}
_STOP = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "when", "what", "which", "how",
        "why", "should", "would", "could", "can", "are", "was", "were", "has", "have", "had", "not",
        "but", "you", "your", "our", "its", "use", "using", "add", "make", "get", "set", "than",
        "then", "there", "their", "them", "they", "does", "did", "any", "all", "one", "two", "new",
        "old", "out",
    }
)
HEADER = (
    "Lessons from earlier runs on this project. They are hypotheses, not facts: re-check each "
    "against the input above and ignore any that does not apply."
)


class LessonBlock(BaseModel):
    """Lessons rendered for a panel prompt, and the ids of those that made it in."""

    text: str = ""
    ids: list[str] = Field(default_factory=list)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", text.lower()) if w not in _STOP}


def _score(lesson: Lesson, query: set[str]) -> float:
    overlap = 2 * len(query & _words(lesson.cue)) + len(query & _words(lesson.lesson))
    return overlap * _WEIGHT[lesson.confidence]


def retrieve(
    project_key: str,
    task_type: str,
    query: str,
    k: int = 3,
    *,
    store: LibraryStore,
) -> list[Lesson]:
    """Up to ``k`` lessons of this project and task type that share words with ``query``, best
    first (newer first on a tie)."""
    words = _words(query)
    if not words or k <= 0:
        return []
    scored = [
        (score, i, lesson)
        for i, lesson in enumerate(store.lessons(project_key, task_type))
        if (score := _score(lesson, words)) > 0
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))  # lessons() is newest first
    return [lesson for _, _, lesson in scored[:k]]


def _line(lesson: Lesson) -> str:
    runs = len(lesson.evidence)
    return (
        f"- [{lesson.id}] When {lesson.cue}: {lesson.lesson} "
        f"({lesson.verdict}, {lesson.confidence} confidence, {runs} run{'s' * (runs != 1)})"
    )


def render_lessons(lessons: Sequence[Lesson], max_tokens: int = DEFAULT_MAX_TOKENS) -> LessonBlock:
    """The lessons as one block of at most ``max_tokens``: whole lines while they fit; the first
    line is shortened when even it alone does not."""
    if not lessons:
        return LessonBlock()
    limit = int(max_tokens * CHARS_PER_TOKEN)
    text, ids = HEADER, list[str]()
    for lesson in lessons:
        line = _line(lesson)
        candidate = f"{text}\n{line}"
        if len(candidate) > limit:
            if ids:
                break
            room = limit - len(text) - 2
            if room < 40:  # not even a useful fragment fits
                return LessonBlock()
            candidate = f"{text}\n{line[: room - 1].rstrip()}…"
        text = candidate
        ids.append(lesson.id)
    return LessonBlock(text=text, ids=ids)
