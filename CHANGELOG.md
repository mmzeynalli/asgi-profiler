<!-- markdownlint-disable MD024 -->

# Changelog

All notable changes to `asgi-profiler` are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/) and this project
follows [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-09-22

Findings, not just numbers. The profiler stops reporting metrics and starts
reporting problems.

### Added

- **Detectors.** Every recorded request is now run through a set of detectors
  after the response has gone out, and what they conclude appears as
  **Findings** on the detail page, as a badge in the request list, and under
  `problems` in the JSON. Three ship in this release:
    - **N+1 query** — repeated identical statements that share an application
      frame. Reports the line that issued them and the statement that ran
      before the loop began. Repetition without a shared call site is not
      reported: a serialiser and a permission check asking the same question
      are not a loop.
    - **Blocking database call** — a synchronous driver call made *on the
      event loop*. A sync `Session` inside an `async def` endpoint freezes
      every other request in flight, raises nothing, and shows up as latency
      on endpoints that have nothing to do with it. Told apart from a `def`
      endpoint (worker thread) and from an async engine (SQLAlchemy's greenlet
      yields), both of which are fine and neither of which is reported.
    - **Slow query** — one statement over `slow_query_issue_ms`, reported once
      per distinct statement however often it ran.
- **Stable fingerprints.** Every finding carries one — `1-n_plus_one_db-<hash>`
  — identifying the *problem* rather than the request, so the same N+1 on four
  hundred requests is one identity. CI can now assert that no new problem
  appeared, not merely that a count stayed under a number.
- **Query start times.** `Query.started_ms` is the offset from the start of
  the request, so overlap, gaps and concurrency are all visible. Durations in
  the detectors are interval unions rather than sums: twenty queries running
  concurrently under `asyncio.gather` are not twenty queries' worth of waiting.
- **`Query.blocking`**, a `Blocking` tile on the detail page, and
  `blocking_count` in the JSON.
- **Statement fingerprints.** `asgi_profiler.sql_hash` and `normalise_sql`
  give a statement a stable identity across executions.
- A **"With findings only"** filter on the request list.
- New options: `detectors`, `n_plus_one_count`, `n_plus_one_ms`,
  `slow_query_issue_ms`, `blocking_query_ms`.
- New exports: `Problem`, `detect`, `DetectorSettings`, `DETECTOR_TYPES`,
  `sql_hash`, `normalise_sql`.
- A [Findings](https://asgi-profiler.netlify.app/guide/detectors/)
  documentation page.

### Changed

- **Statements are grouped by a normalised fingerprint, not by literal text.**
  `IN (1, 2)` and `IN (1, 2, 3)` are one statement, as are `SAVEPOINT sa_1`
  and `SAVEPOINT sa_2`, and any statement differing only in a quoted string,
  a number or a boolean. Identifiers are left alone — double-quoted names
  survive, and `users_2024` is not `users_%s`. This affects the duplicate
  count, the per-request grouping and the statements page, all of which
  previously fragmented on exactly the cases that matter.
- The request list no longer labels any repetition `N+1 ×n`. A request with a
  finding shows what was found; repetition below the thresholds shows
  `repeats ×n`, which is what it is.
- `Profile.finalise()` no longer fills `problems` — the middleware does,
  after it, because detectors need configured thresholds and a dataclass
  should not reach for configuration.

### Migration

- **The SQLite schema is version 2.** An existing capture file is refused with
  the message it already had; re-capture with this version. Two new columns on
  `profiles` (`problems`, `problem_count`, `blocking_count`) and one on
  `statements` (`sql_hash`).
- Nothing in the Python API was removed or renamed.

### Notes on the thresholds

`n_plus_one_ms` and `blocking_query_ms` default to **zero**, which is a
deliberate departure from the production-tuned equivalents in tools like
Sentry. In development the table has twelve rows, so five hundred repeated
queries return in four milliseconds — a duration floor tuned for production
data filters out precisely the findings there is still time to act on. Raise
them when profiling against a production-sized database.

## [0.2.0] - 2026-09-13

Choosing what gets profiled, and somewhere else to look at it.

### Added

- `include_regex` and `exclude_regex` — record only, or never, the paths they
  match. Matched with `search`, so `"/health"` also catches `/health/db`;
  anchor with `^`/`$` for the strict reading.
- `@profiler_include` and `@profiler_exclude` — per-endpoint overrides,
  decisive in both directions and outranking every pattern. Work above or
  below the route decorator. An excluded request is skipped before routing,
  so a health check costs nothing.
- `Profiler.exclude(*paths)` — add exclusions after `install()`, for
  integrations that only learn what to exclude later.
- `mount_path=None` — record without mounting the viewer, for when the pages
  are served somewhere else.
- **SQLAdmin integration** (`asgi-profiler[sqladmin]`). `register(admin,
  profiler)` adds the profiler as a view inside an existing admin, rendered in
  SQLAdmin's own layout. Every route goes through SQLAdmin's authentication,
  which is the first authentication the viewer has ever had. The admin's own
  requests are excluded by default (`profile_admin=True` to keep them), or
  opening the profiler would record the profiler.
- A **Clear** button on the SQLAdmin view, behind a confirmation dialog and
  `POST`-only so a prefetch cannot wipe the history.
- A documentation site: <https://asgi-profiler.netlify.app/>

### Fixed

- `mount_path=""` excluded every path in the application, so the profiler
  recorded nothing and looked broken rather than misconfigured. It now raises
  and names `mount_path=None` as the thing that was probably meant.

### Known limitations

- `@profiler_include` cannot be honoured before routing, because Starlette
  only sets `scope["endpoint"]` during it. The middleware therefore stops
  skipping excluded requests early as soon as one exists anywhere in the
  process. This changes when the decision is made, never what is recorded.

## [0.1.0] - 2026-09-11

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
