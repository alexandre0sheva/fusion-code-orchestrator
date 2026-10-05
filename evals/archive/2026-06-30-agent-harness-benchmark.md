## Opus vs Fusion — Implementation Benchmark

**Task:** Redis vs no-Redis caching decision for fusion run history (docs + README update)
**Date:** 2026-06-30

---

### Summary
| | Opus arm | Fusion arm |
|--|----------|------------|
| Summary | Agent stopped at max steps without calling done. | Agent stopped at max steps without calling done. |
| Files changed | _(none)_ | _(none)_ |
| Verify exit code | 0 | 0 |

> Both arms hit the 20-step cap and wrote no files. Verify passed only because the test suite
> does not assert that `docs/caching-decision.md` exists. **The task was not completed by either arm.**

---

### Cost (USD)
| | Opus | Fusion executor | Fusion orchestration | Fusion total |
|--|------|-----------------|----------------------|--------------|
| Cost | $0.2245 | $0.1878 | $0.0571 | $0.2449 |
| Cheaper arm | **Opus** by $0.0204 | | | |

---

### Tokens
| | Opus | Fusion executor | Fusion orchestration |
|--|------|-----------------|----------------------|
| Input | 12,774 | 60,031 | 3,679 |
| Output | 439 | 516 | 4,125 |
| LLM calls | 20 | 20 | 3 (panel ×2 + synthesis) |

> Fusion executor consumed ~4.7× more input tokens than Opus despite identical step counts —
> the orchestration plan added significant context to every executor turn.

---

### Latency (ms)
| | Opus | Fusion executor | Fusion orchestration | Fusion total |
|--|------|-----------------|----------------------|--------------|
| ms | 37,287 | 113,064 | 70,628 | 183,692 |
| Faster arm | **Opus** | | | Opus faster by 146,405 ms end-to-end (executor delta: 75,777 ms) |

---

### Verdict

**Which arm was cheaper and by how much?**
Opus was cheaper by **$0.0204** ($0.2245 vs $0.2449). Fusion's orchestration panel (Sonnet +
Gemini Flash + synthesis) added $0.0571 on top of the executor cost, and the inflated per-turn
context pushed the executor's input token count to ~60 k vs Opus's ~13 k.

**Which was faster and by how much?**
Opus was faster by **146,405 ms** end-to-end (37 s vs 184 s). The Fusion orchestration
round-trip — 44 s panel + 18 s judge + 13 s synthesis — added ~71 s of overhead before the
executor even started writing. Executor-to-executor latency delta was 75,777 ms.

**Did verify_command pass for each arm?**
Both returned exit code 0, but this is a **false pass**. `test_agent.py` and
`test_compare_implement.py` do not assert that the deliverable files exist; they only confirm
the existing codebase is intact.

**Which implementation would you ship, and why?**
Neither — both arms produced zero file changes. Recommended fixes:

1. **Raise `max_agent_steps` to 40–60.** Reading `task.md`, reasoning about the trade-off,
   and writing two files plus a README edit takes more than 20 turns.
2. For a straightforward write-docs task like this, a **single direct Sonnet call** would be
   cheaper, faster, and more reliable than a two-stage orchestration pipeline.
3. Fusion's multi-model panel (Sonnet + Gemini Flash + synthesis produced 4,125 output
   tokens of analysis) adds the most value for **ambiguous decisions** where model disagreement
   is signal. A "write a decision doc from an already-decided task file" prompt is too
   constrained to benefit from panel voting.

If both arms were given enough steps to complete, **Opus** remains the better baseline:
lower cost, far lower latency, and no orchestration overhead for tasks where the answer is
not uncertain enough to warrant a judge panel.

---

### Orchestration Detail (Fusion)

| Step | Model | Input | Output | Cost | Latency |
|------|-------|-------|--------|------|---------|
| panel:claude-sonnet | claude-sonnet-4-6 | 330 | 2,426 | $0.03738 | 44,111 ms |
| panel:gemini-flash | gemini-flash | 315 | 1,022 | $0.00044 | 17,484 ms |
| synthesis | claude-sonnet-4-6 | 3,034 | 677 | $0.01926 | 12,939 ms |

> Warning from harness: LLM judge quality check failed; scores may be unreliable.

---

### Harness notes
- Each arm ran in an isolated workspace copy — originals are unchanged.
- Opus arm uses direct Anthropic API, not Claude Code session billing.
- Fusion arm includes orchestration plan cost/latency plus executor agent cost/latency.
