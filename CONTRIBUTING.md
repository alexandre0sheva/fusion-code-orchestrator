# Contributing

Thanks for helping improve Fusion Code Orchestrator. This file is canonical for how to contribute:
setup, the quality gate, tests, the docs contract below, the changelog rule and how to publish a
release.

## Development setup

```bash
git clone https://github.com/alexandre0sheva/fusion-code-orchestrator.git
cd fusion-code-orchestrator
uv sync --all-groups
cp .env.example .env          # only needed for live runs
```

## Dependencies

`uv.lock` is committed and CI installs with `--locked`. To update, run `uv lock --upgrade`, then
`uv sync --all-groups`, the quality gate below, and `uv run pytest -W error` (our code should not
emit warnings). Keep the minimum versions in `pyproject.toml` at versions that were actually tested.

Dependabot proposes updates weekly (`.github/dependabot.yml`). CI audits the locked set with
`pip-audit`; to run the same check locally:

```bash
uv export --locked --no-emit-project --all-groups -o /tmp/requirements-audit.txt
uvx pip-audit@2.10.1 --disable-pip -r /tmp/requirements-audit.txt
```

Every GitHub Action in `.github/workflows/` is pinned to a full commit SHA with its version in a
comment (`uses: owner/repo@<sha> # v1.2.3`); `tests/test_ci_config.py` fails on a tag or branch.
To add or bump one, resolve the SHA of the release tag (`git ls-remote https://github.com/owner/repo
refs/tags/v1.2.3`, and the `^{}` line if the tag is annotated) rather than copying a moving tag.

## Quality gate

All three must pass before a change is ready (CI runs the same commands):

```bash
uv run pytest -q
uv run ruff check src tests evals
uv run mypy
```

CI also enforces a **coverage gate**: at least 85% line coverage on each of `src/fusion/orchestration`,
`src/fusion/providers` and `src/fusion/bench`. To check it locally:

```bash
uv run pytest -q --cov=fusion --cov-report=
for p in orchestration providers bench; do
  uv run coverage report --include="src/fusion/$p/*" --fail-under=85 | tail -1
done
```

## Tests

- Tests run offline. `tests/conftest.py` forces the mock provider, removes provider keys and points
  user config, user data and project config at an empty temp tree (the `fusion_home` fixture), so a
  developer's real `~/.config/fusion` or `./.fusion` never leaks in.
- Use `MockProvider` or `httpx.MockTransport`; never require real keys in a test.
- A test that calls a real provider must be marked `@pytest.mark.live`. It is skipped by default
  and runs with `uv run pytest -m live`. Live tests cost money; run them only deliberately.
- Add or update tests for routing, fan-out, costs, evals, MCP output and storage changes.
- **Property tests** (`tests/test_properties.py`, Hypothesis) state what must hold for any input:
  claim clustering and agreement, cost arithmetic, the benchmark statistics, redaction and output
  cleaning. When one fails, Hypothesis prints the smallest input; fix the code or, if the property
  was wrong, fix the property, and keep the failing example as an ordinary test.
- **Provider contract tests** replay wire fixtures from `tests/fixtures/providers/` through each
  adapter (`tests/test_providers_recorded.py`). The fixtures in the repository are written from
  each provider's API reference and say so; `evals/runners/record_provider_fixtures.py --max-usd N`
  records real exchanges (about a cent each, an invalid-key call is free) as `recorded_*.json`
  next to them. Review the `expect` block and the diff before committing, and never commit a
  recording with a secret in it (the recorder refuses, and a test checks).
- **Live smoke tests** (`tests/test_live_smoke.py`) call each real API once and check the response
  still has the shape the fixtures record. They cost money, skip when a key is missing, and go
  through the live-spend ledger: `uv run pytest -m live`.

Local mock workflow:

```bash
export FUSION_DEFAULT_PROVIDER=mock
uv run fusion config validate
uv run fusion ask "How should I retry a failed HTTP call?" --mock
```

## Pull request expectations

- Keep MCP orchestration tools side-effect free. Claude Code stays the executor for file edits,
  shell commands and tests.
- Never `print` to stdout in code reachable from `fusion mcp`; stdout carries JSON-RPC.
- Update the docs and the changelog (below) in the same change.
- Do not commit `.env`, API keys, local databases or machine-specific paths.
- Changes touching redaction, provider payloads, storage or command execution should include tests
  and a short explanation of the risk model in the PR description.

## Docs contract

Each fact lives in exactly one canonical file. Update that file and link to it; delete stale text
rather than copying paragraphs into a second place.

| Knowledge | Canonical file |
|-----------|----------------|
| What it is, quickstart, links | `README.md` (at most 200 lines; enforced by a test) |
| Internals, data flow, modules | `docs/ARCHITECTURE.md` |
| CLI commands and flags, exit codes, doctor, completion; config files, env vars, budgets, fan-out, refinement | `docs/CONFIGURATION.md` |
| Cost and pricing methodology | `docs/COSTS.md` |
| Benchmark methodology and how to run | `docs/BENCHMARKING.md` |
| Dataset provenance, validity limits, licence, guidelines for writing tasks | `evals/datasets/README.md` |
| Benchmark numbers | `docs/BENCHMARK_RESULTS.md` (generated by the benchmark study) |
| Claude Code / Cursor / Codex setup, MCP tool reference | `docs/INTEGRATIONS.md` |
| The manual Claude Code A/B runbook | `docs/CLAUDE_CODE_AB.md` |
| Security model | `SECURITY.md` |
| User-visible changes | `CHANGELOG.md` |
| How to contribute, the quality gate, the docs contract, releasing | `CONTRIBUTING.md` (agents: `CLAUDE.md` points here) |

`tests/test_docs_links.py` checks that relative links and anchors resolve and that the canonical
docs exist.

## Changelog

`CHANGELOG.md` follows Keep a Changelog. Add one line per user-visible change under
`## [Unreleased]`, in the matching heading (Added, Changed, Fixed, Removed, Security). Version
numbers in `pyproject.toml`, `plugin/.claude-plugin/plugin.json` and `uv.lock` change only in the
release commit; tests keep the three in sync.

## Publishing a release

Fusion is distributed as the GitHub repository: users install with
`uvx --from git+https://github.com/alexandre0sheva/fusion-code-orchestrator fusion ...`, pinned to a
release by appending `@vX.Y.Z` to the URL. It is not on PyPI.

1. On a clean `main`, run the quality gate (above), then `uv build` and
   `uv run python evals/runners/check_wheel.py dist/*.whl`. The checker compares the wheel with the
   source tree (configs, dashboard, integration templates, the benchmark dataset), the version and
   the `fusion` script. To try the wheel, run it from an empty directory with
   `uvx --no-cache --from dist/*.whl fusion version`; without `--no-cache`, uv reuses an earlier
   build of the same version.
2. In one commit: bump the version in `pyproject.toml` and `plugin/.claude-plugin/plugin.json`, run
   `uv lock`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` (and add an empty
   `## [Unreleased]` above it), and update the compare links at the bottom of `CHANGELOG.md`.
3. Tag and push: `git tag vX.Y.Z && git push origin main vX.Y.Z`.
4. In GitHub, run the **Release** workflow (`.github/workflows/release.yml`) with that tag. It checks
   that the tag matches the version, runs the quality gate, builds, checks and smoke-tests the
   package, and creates a GitHub Release with the wheel, the source distribution and the changelog
   section as notes. It runs only when started by hand.

**Publishing to PyPI later.** It is off because the owner has not decided to publish there; the
workflow carries the job, commented out. To switch it on: check that the project name is free on
PyPI, set up *trusted publishing* for it there by adding this repository as a publisher (owner, repository, workflow
`release.yml`, environment `pypi`; see <https://docs.pypi.org/trusted-publishers/>), create a GitHub
environment named `pypi` (optionally with required reviewers), and uncomment the `pypi` job at the
end of `release.yml`. No token is stored: the job authenticates with GitHub's OIDC identity. Try it
on TestPyPI first if you want a dry run, then update the install commands in `README.md` and
`docs/INTEGRATIONS.md` to the PyPI name.
