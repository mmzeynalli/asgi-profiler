<!-- markdownlint-disable MD024 -->

# Changelog

All notable changes to `asgi-profiler` are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/) and this project
follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - 11.09.2026

First release. Request and SQL profiling for **ASGI** applications —
**Starlette**, **FastAPI** — with **SQLAlchemy** and **SQLModel**.
`install(app)` is the whole integration: no settings module, no database
table, no migration.

### Added

- `install(app)` — adds a pure-ASGI middleware and mounts the viewer. Returns a
  `Profiler` handle with `slowest()`, `search()`, `summary()`, `statements()`
  and `close()`.
- SQL capture through SQLAlchemy's `before_cursor_execute`,
  `after_cursor_execute` and `handle_error` events, attached to the `Engine`
  class — so every engine in the process is covered, SQLModel needs no special
  support, and async engines work unchanged.
- Per-query application stacks, walked across the greenlet boundary that
  SQLAlchemy's async support puts between the query and your code.
- Statements that raise are recorded with their error and filterable.
- Route-pattern grouping: `/users/1` and `/users/2` aggregate as
  `/users/{user_id}`, recovered by replaying the router where the framework
  does not expose it, and correct under mounts and typed converters.
- **Requests** page — filterable and paginated, with `N+1 ×n` and `SQL error`
  badges.
- **Request detail** page — repeated statements collapse into one row with a
  `×N` badge, the spread of their timings, a sample of the parameters, and one
  copy of the stack that issued them.
- **Summary** page — grouped by route, with p50/p95/p99 rather than an average.
- **Statements** page — every statement aggregated across all requests.
- JSON for every page (`/requests.json`, `/request/<id>.json`,
  `/summary.json`, `/statements.json`), so a test can fail a build when an
  endpoint regresses to an N+1.
- `X-Profiler-Id` response header, linking a slow response to its trace.
- `MemoryStorage` (default) and `SQLiteStorage`, the latter shared across
  `uvicorn` workers and written from a background thread so no `fsync` or lock
  wait lands on the event loop.
- `Storage` protocol plus a `BaseStorage` that supplies searching, paging,
  summarising and statement aggregation, so a custom backend needs five
  methods.
- `python -m asgi_profiler profiler.db` — browse a capture file offline,
  read-only, with no application.
- `authorize=` hook, sync or async. `POST /clear` rejects cross-site requests.
- `py.typed`, checked in CI with both `mypy` and `ty`, on Python 3.10–3.13.

### Known limitations

- No Python-side profiling yet: when an endpoint is slow and the SQL is not,
  "time in Python" is a single number with nothing behind it.
- Request and response bodies are never recorded.
- WebSockets are not recorded.
- The `Engine`-class listeners are process-wide, so the profiler cannot be
  scoped to one engine and the most recent `install()` decides
  `capture_stacks`.
- `max_requests` bounds the number of requests kept, not their total size.
