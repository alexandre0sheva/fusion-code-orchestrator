"""Tests for secret redaction."""

from fusion.security.redaction import redact_secrets


def test_redacts_api_key_assignment() -> None:
    text = "api_key=sk-abcdefghijklmnopqrstuvwxyz1234567890"
    result = redact_secrets(text)
    assert "[REDACTED]" in result.text
    assert "sk-" not in result.text
    assert result.redaction_count >= 1


def test_redacts_github_token() -> None:
    token = "ghp_" + "a" * 36
    result = redact_secrets(f"token={token}")
    assert "[REDACTED]" in result.text
    assert token not in result.text


def test_redacts_env_line() -> None:
    text = "DATABASE_PASSWORD=supersecret123\nOTHER=value"
    result = redact_secrets(text)
    assert "supersecret123" not in result.text
    assert "[REDACTED]" in result.text


def test_redacts_aws_key() -> None:
    text = "key=AKIAIOSFODNN7EXAMPLE"
    result = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text


def test_redacts_jwt() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    result = redact_secrets(f"Authorization: Bearer {jwt}")
    assert jwt not in result.text


def test_preserves_safe_content() -> None:
    text = "def hello():\n    return 'world'"
    result = redact_secrets(text)
    assert result.text == text
    assert result.redaction_count == 0
