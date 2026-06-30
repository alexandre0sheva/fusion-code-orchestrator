# Opus Answer: Redis Caching for Fusion Run History

## Recommendation

**Do not add Redis. Fix the SQLite access layer first, and add an in-process cache only if profiling still shows residual latency.**

For a single-user, local, non-multi-tenant MCP server, Redis is overkill — and more importantly, it papers over two real root-cause defects in the current code without fixing them.

---

## When Redis Helps vs. Overkill Here

Redis earns its keep when you have: multiple processes/hosts sharing state, high read concurrency that contends with writes, cross-request session/queue semantics, or a working set that exceeds local memory but benefits from sub-millisecond network-cached access. **None of those apply here** — this is one process, one user, reads served from a local file. A Redis round-trip (network + serialize/deserialize JSON blobs) can actually be *slower* than a tuned local SQLite read, while adding a daemon to install, run, monitor, and keep in sync.

The "read-heavy" pressure is almost certainly self-inflicted, not a SQLite limitation:

- **`migrate()` runs on every `get_connection()` call** (sqlite.py:17) — every read re-opens a connection and re-runs migrations.
- **`list_runs()` is an N+1 pattern** (run_store.py:175-183): selects run_ids, then calls `get_run()` per id; each `get_run` opens its own connection, re-runs migrations, and issues two more queries. A 20-row list = 21+ connections and 21+ migration passes.

Caching with Redis would hide this behind a cache layer instead of removing O(N) connection/migration overhead.

---

## Consistency / Invalidation Risks

- **Redis:** introduces a classic cache-coherence problem. Every write path (`create_run`, `complete_run`) needs explicit invalidation; a missed invalidation serves stale run history. Two stores can diverge on crash (SQLite write succeeds, Redis invalidation fails).
- **In-process LRU (safer fallback):** same invalidation requirement but trivially correct — clear/update within the same process on writes. No serialization, no second source of truth, no network partition.
- **Write cadence is low** (once per orchestration run), so cache churn is minimal — exactly why an external cache isn't justified.

---

## Operational Complexity

- **Redis:** new external daemon, install/version management, connection config, new failure mode (Redis down → degrade or fail), serialization overhead, and it breaks the "clone and run locally" simplicity of a single-file SQLite tool.
- **SQLite fixes + optional in-process cache:** fully internal to `src/fusion/storage/`, no new dependencies, no daemon, fully reversible.

---

## Recommendation and Trade-offs

Fix SQLite first; defer/skip caching:

1. Move `migrate()` out of `get_connection()` to a one-time startup hook.
2. Reuse a single (or thread-local) connection instead of connect-per-call; set `check_same_thread=False` if needed.
3. Enable `PRAGMA journal_mode=WAL` and `synchronous=NORMAL` — concurrent reads without writer contention. Trade-off: adds `.wal`/`.shm` sidecar files.
4. Rewrite `list_runs()` to fetch in one query (JOIN or `WHERE run_id IN (...)`), turning O(N) round-trips into O(1).
5. **Only then** profile. If reads are still slow, add a `functools.lru_cache` / dict keyed by `run_id`, invalidated on writes. Likely unnecessary after steps 1–4.

**Revisit Redis only if** the product becomes multi-tenant, goes multi-process/multi-host, or run history outgrows local file + memory — at which point Redis becomes a genuine architectural need.

---

## Confidence: ~0.92
