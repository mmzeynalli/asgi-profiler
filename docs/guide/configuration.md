# Configuration

Every option is a field on `ProfilerConfig`, and every one can be passed to
`install()` as a keyword:

```python
install(app, mount_path="/_perf", capture_stacks=False)
```

Or built up as an object and reused:

```python
from asgi_profiler import ProfilerConfig, install

config = ProfilerConfig(max_requests=2000, slow_query_ms=25)
install(app, config=config)
```

!!! note "`install()` never mutates the config you hand it"

    Keyword options are merged into a *copy*. A shared `ProfilerConfig` passed
    to two applications is not rewritten under you by the first one.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `mount_path` | `"/profiler"` | Where the viewer is mounted. `None` records without mounting it at all — see [SQLAdmin](../integrations/sqladmin.md). |
| `max_requests` | `500` | How many requests to retain. |
| `exclude_paths` | `("/favicon.ico",)` | Paths never recorded. The viewer's own mount is added automatically. |
| `include_regex` | `None` | Record only matching paths. `None` is everything. See [Filtering](filtering.md). |
| `exclude_regex` | `None` | Never record matching paths. Applied before `include_regex`. |
| `capture_stacks` | `True` | Record the application frames behind each query. |
| `stack_depth` | `8` | How many frames to keep. |
| `slow_request_ms` | `500.0` | Threshold for highlighting a request. |
| `slow_query_ms` | `50.0` | Threshold for highlighting a statement. |
| `capture_headers` | `True` | Store request and response headers. |
| `redacted_headers` | see below | Header names stored as `<redacted>`. |
| `response_header` | `"x-profiler-id"` | Response header carrying the profile id. `None` adds nothing. |
| `page_size` | `50` | Rows per page in the viewer. |
| `statement_limit` | `100` | Rows on the statements page. |
| `authorize` | `None` | Called for every viewer request. See [Security](security.md). |
| `detectors` | `None` | Which detectors to run, by name. `None` is all of them, `()` is none. See [Findings](detectors.md). |
| `n_plus_one_count` | `5` | How many identical statements make an N+1. |
| `n_plus_one_ms` | `0.0` | Non-overlapping time they must occupy. Zero on purpose — see [Findings](detectors.md#why-two-thresholds-default-to-zero). |
| `slow_query_issue_ms` | `100.0` | A statement at or over this is reported as a problem. |
| `blocking_query_ms` | `0.0` | Loop-blocking time before a request is reported. Zero on purpose. |

!!! note "`slow_query_ms` and `slow_query_issue_ms` are different options"

    `slow_query_ms` colours a row in the viewer. `slow_query_issue_ms`
    decides whether something is reported as a finding. Cosmetics and
    conclusions are deliberately separate settings.

## Exclusions match on segment boundaries

`exclude_paths=("/health",)` excludes `/health` and `/health/db`, but **not**
`/healthcheck`. A bare prefix match would make the default `/profiler` mount
swallow an application's own `/profiler-admin`, silently.

For anything beyond a literal prefix, use `include_regex` / `exclude_regex` or
the `@profiler_exclude` / `@profiler_include` decorators — see
[Filtering](filtering.md), which also documents how the four mechanisms rank
against each other.

Exclusions can also be added after `install()`:

```python
profiler = install(app)
profiler.exclude("/internal", "/metrics")
```

## Redacted headers

These are never stored:

`authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`,
`x-auth-token`

Pass your own `redacted_headers` to replace the list, or
`capture_headers=False` to store none at all.

## The cost of stacks

`capture_stacks` is the most expensive option, because it walks the Python
stack on every statement. It is also what makes an N+1 actionable rather than
merely visible: without it you learn that a query ran twelve times, not which
line ran it.

The walk stops as soon as it has `stack_depth` application frames, so the cost
is roughly 9 µs per query rather than the 220 µs a naive
`traceback.extract_stack()` costs. Turn it off if you are profiling a very
chatty endpoint and only care about counts.
