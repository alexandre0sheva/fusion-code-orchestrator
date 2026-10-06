"""Tests for secret redaction: provider key shapes, key blocks, .env files and the entropy rule.

Credentials are assembled at run time from pieces so that secret scanners looking at the repository
do not mistake a fixture for a leak.
"""

from __future__ import annotations

import pytest

from fusion.security.redaction import EntropyConfig, redact_secrets

OFF = EntropyConfig(enabled=False)
# 40 characters, 4 character classes, no recognisable prefix: what a generated secret looks like.
RANDOM_TOKEN = "q7Zk" + "Xp2mVb9" + "LrT4wYc8" + "NdJ5sHf3" + "GaE6uKo1" + "Iy"


def kinds(text: str, *, entropy: EntropyConfig = OFF) -> list[str]:
    return redact_secrets(text, entropy=entropy).redacted_types


# ------------------------------------------------------------------------------------- key shapes


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


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("openai project key", "sk-proj-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kM9n" + "_x-Y"),
        ("openai service account", "sk-svcacct-" + "Zq8Wx3Vc7Bn1Mk5Lj9Hg2Fd6Sa4Pl0Oi"),
        ("openai legacy key", "sk-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7b"),
        ("anthropic key", "sk-ant-api03-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kM9n-AA"),
        ("google api key", "AIza" + "SyA-Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0e"),
        ("google oauth token", "ya29." + "a0AfH6SMBx3Vc7Bn1Mk5Lj9Hg2Fd6Sa4Pl0Oi8U"),
        ("github classic", "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"),
        ("github oauth", "gho_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"),
        ("github app", "ghs_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"),
        ("github fine grained", "github_pat_" + "11ABCDEFG0Ab3dE6gH9jK2_mN5pQ8sT1vW4yZ7bC0eF3h"),
        ("aws access key", "AKIA" + "IOSFODNN7EXAMPLE"),
        ("aws temporary key", "ASIA" + "IOSFODNN7EXAMPLE"),
        ("slack bot token", "xoxb-" + "123456789012-1234567890123-Ab3dE6gH9jK2mN5pQ8sT1vW4"),
        ("stripe live key", "sk_" + "live_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4"),
        ("npm token", "npm_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"),
    ],
)
def test_redacts_provider_key_shapes(label: str, secret: str) -> None:
    result = redact_secrets(f"client = Client('{secret}')\n", entropy=OFF)
    assert secret not in result.text, label
    assert result.redaction_count == 1, label


def test_redacts_aws_secret_access_key_assignment() -> None:
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
    text = f"aws_secret_access_key = {secret}\nregion = us-east-1\n"
    result = redact_secrets(text, entropy=OFF)
    assert secret not in result.text
    assert "region = us-east-1" in result.text


def test_redacts_bearer_token_in_a_curl_command() -> None:
    result = redact_secrets("curl -H 'Authorization: Bearer abc123DEF456ghi789' https://x.test")
    assert "abc123DEF456ghi789" not in result.text
    assert "https://x.test" in result.text


def test_redacts_credentials_inside_a_connection_string() -> None:
    text = "DATABASE_URL=postgres://app:hunter2hunter2@db.internal:5432/app"
    result = redact_secrets(text, entropy=OFF)
    assert "hunter2hunter2" not in result.text
    text = "engine = create_engine('mysql+pymysql://root:p4ssw0rd!@10.0.0.5/shop')"
    result = redact_secrets(text, entropy=OFF)
    assert "p4ssw0rd" not in result.text
    assert "10.0.0.5/shop" in result.text


def test_leaves_a_url_without_credentials_alone() -> None:
    text = "see https://example.com/docs and ssh://git@github.com/org/repo.git"
    assert redact_secrets(text, entropy=OFF).text == text


# ------------------------------------------------------------------------------------- key blocks


def _pem(kind: str) -> str:
    body = "\n".join(["MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"] * 3)
    return f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----"


@pytest.mark.parametrize(
    "kind",
    [
        "RSA PRIVATE KEY",
        "PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
    ],
)
def test_redacts_a_whole_private_key_block(kind: str) -> None:
    text = f"before\n{_pem(kind)}\nafter"
    result = redact_secrets(text, entropy=OFF)
    assert "MIIEow" not in result.text
    assert "PRIVATE KEY" not in result.text  # the END line goes with the block
    assert result.text.startswith("before\n")
    assert result.text.endswith("\nafter")
    assert result.redaction_count == 1


def test_redacts_a_pgp_private_key_block() -> None:
    text = _pem("PGP PRIVATE KEY BLOCK")
    assert "MIIEow" not in redact_secrets(text, entropy=OFF).text


def test_redacts_a_private_key_cut_off_before_its_end_line() -> None:
    # A diff or a truncated paste can lose the footer; the body must still go.
    text = _pem("RSA PRIVATE KEY").rsplit("\n", 1)[0] + "\n\nThe rest of the prompt."
    result = redact_secrets(text, entropy=OFF)
    assert "MIIEow" not in result.text
    assert result.text.endswith("The rest of the prompt.")


def test_a_mention_of_the_pem_header_does_not_swallow_the_prompt() -> None:
    text = "The file starts with -----BEGIN PRIVATE KEY----- and a long base64 body.\n" + "x" * 50
    assert redact_secrets(text, entropy=OFF).text.endswith("x" * 50)


# ------------------------------------------------------------------------------------- .env files

DOTENV = """\
# Production settings
DEBUG=false
PORT=8080
export STRIPE_SECRET_KEY="{stripe}"
DATABASE_PASSWORD='correct-horse-battery'
GITHUB_TOKEN={github}
SMTP_PASSWD = hunter2hunter2
LOG_LEVEL=info
"""


def test_dotenv_secrets_are_redacted_and_settings_are_kept() -> None:
    stripe = "sk_" + "live_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4"
    github = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"
    result = redact_secrets(DOTENV.format(stripe=stripe, github=github), entropy=OFF)
    for leaked in (stripe, github, "correct-horse-battery", "hunter2hunter2"):
        assert leaked not in result.text
    for kept in ("DEBUG=false", "PORT=8080", "LOG_LEVEL=info", "# Production settings"):
        assert kept in result.text
    assert "STRIPE_SECRET_KEY=" in result.text  # the name stays: it tells the model what it is
    assert result.redaction_count == 4


def test_one_secret_is_counted_once() -> None:
    result = redact_secrets("DATABASE_PASSWORD=supersecret123", entropy=OFF)
    assert result.redaction_count == 1
    assert result.text == "DATABASE_PASSWORD=[REDACTED]"
    again = redact_secrets(result.text, entropy=OFF)
    assert again.text == result.text
    assert again.redaction_count == 0


# ----------------------------------------------------------------------------------- entropy rule


def test_entropy_rule_catches_a_secret_with_no_known_shape() -> None:
    text = f'headers = {{"X-Signature": "{RANDOM_TOKEN}"}}'
    result = redact_secrets(text, entropy=EntropyConfig())
    assert RANDOM_TOKEN not in result.text
    assert "high_entropy" in result.redacted_types
    assert result.text.startswith('headers = {"X-Signature": "')


def test_entropy_rule_can_be_turned_off() -> None:
    text = f'headers = {{"X-Signature": "{RANDOM_TOKEN}"}}'
    assert redact_secrets(text, entropy=OFF).text == text


def test_entropy_threshold_is_configurable() -> None:
    text = f'sig = "{RANDOM_TOKEN}"'
    strict = EntropyConfig(threshold=7.9)
    assert redact_secrets(text, entropy=strict).text == text


def test_entropy_min_length_is_configurable() -> None:
    short = "q7ZkXp2mVb9LrT4wYc8N"  # 20 characters
    assert redact_secrets(f'sig = "{short}"', entropy=EntropyConfig()).text == f'sig = "{short}"'
    loose = EntropyConfig(min_length=16, threshold=4.0)
    assert short not in redact_secrets(f'sig = "{short}"', entropy=loose).text


@pytest.mark.parametrize(
    "benign",
    [
        "commit 9fceb02d0ae598e95dc970b74767f19372d61af8",  # git sha
        "sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "id = '550e8400-e29b-41d4-a716-446655440000'",  # uuid
        "from fusion.orchestration.stages import RedactStageWithAReallyLongNameForTesting",
        "name = 'a_very_long_snake_case_identifier_that_goes_on_and_on'",
        "/usr/local/lib/python3.12/site-packages/some_package/another_module/file.py",
        "integrity sha512-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kM9nPqRsTuVwXyZ01234567==",
    ],
)
def test_entropy_rule_leaves_ordinary_code_alone(benign: str) -> None:
    result = redact_secrets(benign, entropy=EntropyConfig())
    assert result.text == benign
    assert result.redaction_count == 0


def test_entropy_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    text = f'sig = "{RANDOM_TOKEN}"'
    monkeypatch.setenv("FUSION_REDACT_ENTROPY", "off")
    assert redact_secrets(text).text == text
    monkeypatch.setenv("FUSION_REDACT_ENTROPY", "on")
    assert RANDOM_TOKEN not in redact_secrets(text).text
    monkeypatch.setenv("FUSION_REDACT_ENTROPY_THRESHOLD", "7.9")
    assert redact_secrets(text).text == text


def test_a_bad_entropy_setting_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_REDACT_ENTROPY_THRESHOLD", "not-a-number")
    assert EntropyConfig.from_env() == EntropyConfig()


# ------------------------------------------------------------------------------- realistic input


def test_a_realistic_diff_keeps_its_code_and_loses_its_secret() -> None:
    key = "AIza" + "SyA-Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0e"
    diff = f"""\
diff --git a/app/client.py b/app/client.py
--- a/app/client.py
+++ b/app/client.py
@@ -10,7 +10,7 @@ import httpx
 def make_client(timeout: float = 10.0) -> httpx.Client:
-    return httpx.Client(timeout=timeout)
+    return httpx.Client(timeout=timeout, params={{"key": "{key}"}})
"""
    result = redact_secrets(diff, entropy=EntropyConfig())
    assert key not in result.text
    assert "def make_client(timeout: float = 10.0) -> httpx.Client:" in result.text
    assert result.redaction_count == 1


# ------------------------------------------------------------------------------- nested records


def test_redact_value_cleans_every_string_in_a_nested_record() -> None:
    from fusion.security.redaction import redact_value

    token = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"
    value = {"a": f"token {token}", "n": 3, "list": [f"x={token}", {"deep": token}], "ok": "fine"}
    cleaned = redact_value(value)
    assert token not in str(cleaned)
    assert cleaned["n"] == 3 and cleaned["ok"] == "fine"
    assert redact_value("plain") == "plain" and redact_value(None) is None
