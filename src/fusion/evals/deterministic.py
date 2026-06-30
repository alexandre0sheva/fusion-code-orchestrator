"""Deterministic evaluation checks."""

from __future__ import annotations

import json
import re


def check_response_completeness(content: str, min_length: int = 50) -> tuple[bool, list[str]]:
    """Check that a response meets minimum completeness requirements."""
    issues: list[str] = []
    if len(content.strip()) < min_length:
        issues.append(f"Response too short ({len(content)} chars, min {min_length})")
    if not content.strip():
        issues.append("Response is empty")
    return len(issues) == 0, issues


def check_output_length(
    content: str,
    *,
    min_length: int = 50,
    max_length: int = 50_000,
) -> tuple[bool, list[str]]:
    """Check output length is within configured range."""
    issues: list[str] = []
    length = len(content.strip())
    if length < min_length:
        issues.append(f"Output too short ({length} chars, min {min_length})")
    if length > max_length:
        issues.append(f"Output too long ({length} chars, max {max_length})")
    return len(issues) == 0, issues


def check_no_secret_leakage(content: str) -> tuple[bool, list[str]]:
    """Ensure response does not contain obvious secret patterns."""
    issues: list[str] = []
    patterns = [
        (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
        (r"\bghp_[a-zA-Z0-9]{36,}\b", "GitHub token"),
        (r"-----BEGIN.*PRIVATE KEY-----", "Private key"),
        (r"\bsk-[a-zA-Z0-9]{20,}\b", "API secret key"),
    ]
    for pattern, name in patterns:
        if re.search(pattern, content):
            issues.append(f"Possible secret leak: {name}")
    return len(issues) == 0, issues


def check_no_dangerous_shell_commands(content: str) -> tuple[bool, list[str]]:
    """Flag dangerous shell command patterns in recommendations."""
    issues: list[str] = []
    dangerous = [
        (r"rm\s+-rf\s+/", "Recursive delete from root"),
        (r"curl\s+[^\n|]*\|\s*(?:ba)?sh", "Pipe curl to shell"),
        (r"wget\s+[^\n|]*\|\s*(?:ba)?sh", "Pipe wget to shell"),
        (r"chmod\s+777", "Overly permissive chmod"),
        (r"DROP\s+DATABASE", "DROP DATABASE command"),
        (r";\s*sudo\s+", "Chained sudo command"),
    ]
    for pattern, name in dangerous:
        if re.search(pattern, content, re.IGNORECASE):
            issues.append(f"Dangerous command pattern: {name}")
    return len(issues) == 0, issues


def check_unsupported_file_references(
    content: str,
    known_files: list[str] | None,
) -> tuple[bool, list[str]]:
    """Flag file path references not present in the provided file list."""
    if not known_files:
        return True, []
    issues: list[str] = []
    known = {f.lower() for f in known_files}
    path_pattern = r"(?:[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|yaml|yml|json|md|sql))"
    for match in re.findall(path_pattern, content):
        normalized = match.lstrip("./")
        if normalized.lower() not in known and "/" in normalized:
            issues.append(f"Unsupported file reference: {match}")
    return len(issues) == 0, issues


def check_includes_test_plan(
    content: str, *, is_coding_task: bool = False
) -> tuple[bool, list[str]]:
    """Check coding outputs mention tests."""
    if not is_coding_task:
        return True, []
    if re.search(r"\btest", content, re.IGNORECASE):
        return True, []
    return False, ["Missing test plan or test mentions for coding output"]


def check_includes_uncertainty(content: str) -> tuple[bool, list[str]]:
    """Check output includes uncertainty or caveats."""
    markers = [
        "uncertain",
        "assumption",
        "caveat",
        "may",
        "might",
        "risk",
        "confidence",
        "if ",
        "depends",
    ]
    lower = content.lower()
    if any(m in lower for m in markers):
        return True, []
    return False, ["Missing uncertainty markers or caveats"]


def check_structured_sections(
    content: str, required_markers: list[str] | None = None
) -> tuple[bool, list[str]]:
    """Check for expected markdown section markers."""
    markers = required_markers or ["##", "**"]
    issues: list[str] = []
    if not any(m in content for m in markers):
        issues.append("Missing expected structural markers in response")
    return len(issues) == 0, issues


def check_json_parseable(content: str) -> tuple[bool, list[str]]:
    """Check if content contains valid JSON (for judge responses)."""
    issues: list[str] = []
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            json.loads(content[start:end])
        else:
            issues.append("No JSON object found in content")
    except json.JSONDecodeError as exc:
        issues.append(f"Invalid JSON: {exc}")
    return len(issues) == 0, issues


def check_schema_completeness(
    data: dict[str, object],
    required_keys: list[str],
) -> tuple[bool, list[str]]:
    """Check a parsed JSON object has required keys."""
    issues: list[str] = []
    for key in required_keys:
        if key not in data or data[key] in (None, "", []):
            issues.append(f"Missing or empty required field: {key}")
    return len(issues) == 0, issues


def run_deterministic_checks(
    content: str,
    *,
    is_judge: bool = False,
    is_coding_task: bool = False,
    require_uncertainty: bool = False,
    min_length: int = 50,
    max_length: int = 50_000,
    known_files: list[str] | None = None,
    required_json_keys: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Run all applicable deterministic checks."""
    all_issues: list[str] = []
    passed = True

    checks: list[tuple[tuple[bool, list[str]], ...]] = [
        (
            check_output_length(content, min_length=min_length, max_length=max_length),
            check_no_secret_leakage(content),
            check_no_dangerous_shell_commands(content),
        ),
    ]

    for group in checks:
        for ok, issues in group:
            if not ok:
                passed = False
                all_issues.extend(issues)

    if known_files:
        ok, issues = check_unsupported_file_references(content, known_files)
        if not ok:
            passed = False
            all_issues.extend(issues)

    if is_judge:
        ok, issues = check_json_parseable(content)
        if not ok:
            passed = False
            all_issues.extend(issues)
        if required_json_keys:
            try:
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    parsed = json.loads(content[start:end])
                    if isinstance(parsed, dict):
                        ok, issues = check_schema_completeness(parsed, required_json_keys)
                        if not ok:
                            passed = False
                            all_issues.extend(issues)
            except json.JSONDecodeError:
                pass
    else:
        ok, issues = check_structured_sections(content)
        if not ok:
            passed = False
            all_issues.extend(issues)
        if is_coding_task:
            ok, issues = check_includes_test_plan(content, is_coding_task=True)
            if not ok:
                passed = False
                all_issues.extend(issues)
        if require_uncertainty:
            ok, issues = check_includes_uncertainty(content)
            if not ok:
                passed = False
                all_issues.extend(issues)

    return passed, all_issues
