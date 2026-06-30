# Task

Should we add Redis caching in front of SQLite for fusion run history?

## Context
- Current storage: SQLite in src/fusion/storage/
- Read-heavy: runs list/show
- Single-user local MCP server, not multi-tenant yet

## What a good answer must cover
- When Redis helps vs overkill
- Consistency / invalidation risks
- Operational complexity
- A recommendation with trade-offs