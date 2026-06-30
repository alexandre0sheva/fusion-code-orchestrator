# Fusion Answer: Redis Caching for Fusion Run History

## Recommendation

**Use an in-process memory cache (LRU cache in Node.js)** — not Redis.

---

## When Redis Helps vs. Overkill

Redis would offer durable caching and future multi-tenant scalability, but it introduces a separate daemon, network overhead (even on localhost), and operational complexity that is incompatible with the single-user local deployment constraint. For a local MCP server with a single user, Redis is overkill.

An in-process LRU cache provides sub-millisecond cache hits with no external process dependency. SQLite-only remains a valid baseline — it may already be sufficient — and should be the fallback if profiling shows read latency is not a real problem.

---

## Consistency / Invalidation Risks

- Cache invalidation bugs can cause stale reads if write paths do not consistently invalidate cache entries.
- **Mitigation:** Route all SQLite access through a single storage abstraction layer (`src/fusion/storage/`). If writes are scattered across the codebase, invalidation coverage is harder to guarantee.
- Cold cache on every process restart reduces effectiveness if the server restarts frequently — acceptable for a local deployment.

---

## Operational Complexity

| Option | Operational Cost |
|---|---|
| Redis | Separate daemon to install/run/monitor; cross-process consistency surface; localhost network round-trip |
| In-process LRU | Single npm dependency (`lru-cache`); lives entirely within the process; no infra to operate |
| SQLite only | Zero additional complexity |

---

## Trade-offs

| | In-process LRU | Redis | SQLite only |
|---|---|---|---|
| Latency | Sub-ms | ~1ms (localhost) | Depends on query/index |
| Operational cost | Very low | High (separate process) | None |
| Consistency risk | Moderate (invalidation) | High (cross-process) | None |
| Memory footprint | Bounded by LRU config | External | None |
| Reversibility | High | Medium | — |

---

## Migration Plan

1. **Profile first** — confirm SQLite read latency is actually a problem before adding any cache.
2. Centralize all SQLite access behind `src/fusion/storage/index.js` (prerequisite for safe invalidation).
3. Add `lru-cache` (npm) with explicit `maxSize` / `ttl` bounds.
4. Wrap read operations: check cache → SQLite on miss → populate cache.
5. Invalidate on all writes (insert, update, delete) within the storage module.
6. Monitor memory usage and cache hit rate; tune limits.

---

## Confidence: 0.88
