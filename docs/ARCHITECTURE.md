# Architecture

Fusion Code Orchestrator is a Python MCP server that gives Claude Code model-like
multi-model workflows for coding tasks. It calls providers directly through adapters and
does not use OpenRouter.

## Components

### MCP server

`src/fusion/mcp_server/server.py` registers the public tools:

- `fusion_review_diff`
- `fusion_ask`
- `fusion_debug_error`
- `fusion_decide_architecture`
- `fusion_plan_feature`
- `fusion_eval_answer`
- `fusion_compare_claude_runs`
- `fusion_compare_implement`

`src/fusion/mcp_server/tools.py` converts MCP input schemas into orchestration pipeline
inputs and returns Pydantic output models as JSON dictionaries.

### Providers

Providers implement `ModelProvider` in `src/fusion/providers/base.py`.

Adapters normalize provider-specific responses into `ModelResponse`, including:

- provider and model IDs;
- text and parsed JSON;
- input/output/cached/reasoning tokens when available;
- actual or estimated cost when available;
- latency and structured errors.

Cloud providers are Anthropic, OpenAI, and Google. Ollama and LM Studio support local-only
routes. `MockProvider` supports deterministic offline tests and development.

### Router

`src/fusion/routing/policy.py` classifies task type, complexity, and risk, then selects:

- panel models;
- judge model;
- synthesizer model;
- budget tier and routing warnings.

Model metadata lives in `src/fusion/config/default_models.yaml`. Task policies and fanout
settings live in `src/fusion/config/routing_policies.yaml`.

### Fanout

`src/fusion/orchestration/fanout.py` starts panel calls concurrently with `asyncio`.

It tracks:

- per-model timeout;
- global panel timeout;
- concurrency limit;
- minimum successful responses;
- partial results;
- structured failure status;
- panel wall latency, max model latency, and summed model-call latency.

Synthesis runs after fanout and disagreement analysis. The MCP request is still a normal
blocking request from Claude Code's perspective.

### Evals

`src/fusion/evals/engine.py` coordinates hybrid evals:

- deterministic checks always run;
- LLM judge runs when a configured judge model is available;
- heuristic fallback runs when judge calls fail;
- final aggregate confidence combines context sufficiency, consensus, answer quality,
  final quality, provider success rate, unsupported-claim penalty, and residual risk.

Eval data affects warnings, confidence, and the final MCP output.

### Synthesis

Panel outputs are scored and checked for disagreement. The synthesizer prompt receives:

- original task;
- panel responses;
- disagreement analysis;
- requested JSON schema.

Raw panel outputs are not included in MCP responses unless `include_raw_outputs=true`.

### Telemetry

`src/fusion/telemetry/cost.py` owns usage and baseline comparison schemas:

- `ModelUsage`
- `UsageSummary`
- `CostComparison`
- `PricingRegistry`

Pricing is loaded from `src/fusion/config/pricing.yaml`. Baseline comparison is loaded from
`src/fusion/config/baseline.yaml`.

### Storage

`src/fusion/storage/run_store.py` stores each run in SQLite:

- run ID and timestamp;
- original and sanitized inputs;
- routing;
- trace;
- panel, synthesis, and final outputs;
- evals;
- usage summary;
- cost comparison;
- warnings and errors.

CLI inspection commands:

```bash
uv run fusion runs list
uv run fusion runs show RUN_ID
uv run fusion runs costs
uv run fusion runs compare-baseline RUN_ID
uv run fusion runs export --format jsonl
```

## Safety boundaries

The orchestration MCP tools are side-effect free inside MCP: they do not execute shell
commands in user repositories or edit files themselves. That boundary should not block
Claude Code. Claude Code can use Fusion's answer like a cheaper model response, then edit,
run tests, and continue the normal coding workflow.

Agent benchmark mode is separate, opt-in, and guarded by `FUSION_AGENT_MODE=true` and
`FUSION_WORKSPACE_ROOT`.
