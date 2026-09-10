# Changelog

All notable changes are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First release.

Silk-style request and SQL profiling for Starlette and FastAPI, with
SQLAlchemy and SQLModel. `install(app)` is the whole integration.

### Recording

- Pure-ASGI middleware records every HTTP request: method, path, matched route
  pattern, status, wall time, and the SQL it ran. `BaseHTTPMiddleware` would
  run the app in a separate task and break the contextvar that query
  attribution depends on.
- SQL is captured through SQLAlchemy's `before_cursor_execute`,
  `after_cursor_execute` and `handle_error` events attached to the `Engine`
  **class**, so every engine in the process is covered and SQLModel needs no
  special support. Async engines included. Statements that raise are recorded
  with their error.
- Each statement carries the application frames that issued it, walked outward
  from the query and across the greenlet boundary that SQLAlchemy's async
  support puts in the way.
- Attribution holds under concurrency: the suite fires concurrent requests
  with different query counts and asserts none of them bleed.
- Route patterns come from the framework where it offers one and are recovered
  by replaying the router where it does not, so `/users/1` and `/users/2`
  aggregate as `/users/{user_id}` even under a mount.

### Viewing

- **Requests** — filterable and paginated, with `N+1 xN` and `SQL error`
  badges.
- **Request detail** — timing breakdown, then the statements, with repeated
  ones collapsed into a single row carrying a `xN` badge, the spread of their
  timings, a sample of the parameters, and one copy of the stack. A 500-row
  N+1 is one line to read rather than 500 to scroll.
- **Summary** — grouped by route, with p50/p95/p99 rather than an average.
- **Statements** — every statement aggregated across all requests, answering
  what costs you application-wide rather than on one endpoint.
- JSON for every page, so a test can fail a build when an endpoint regresses
  to an N+1.
- `X-Profiler-Id` on every response, linking a slow response to its trace.
- The viewer mounts anywhere and works behind a proxy prefix. It has no
  authentication of its own; pass `authorize=` (sync or async).

### Storage

- `MemoryStorage` — a bounded ring buffer, the zero-config default.
- `SQLiteStorage` — survives restarts and is shared by every worker. Writes go
  through a background thread so no `fsync` or lock wait lands on the event
  loop; reads flush first.
- `Storage` is a Protocol and `BaseStorage` supplies searching, paging,
  summarising and statement aggregation, so a custom backend needs five
  methods.
- `python -m starlette_profiler profiler.db` browses a capture file offline,
  read-only.

### Known limits

- No Python-side profiling yet: when an endpoint is slow and the SQL is not,
  "time in Python" is a single number with nothing behind it.
- Request and response bodies are never recorded.
- WebSockets are not recorded.
- The `Engine`-class listeners are process-wide, so the profiler cannot be
  scoped to one engine and the most recent `install()` decides
  `capture_stacks`.
- `max_requests` bounds the number of requests kept, not their total size.
