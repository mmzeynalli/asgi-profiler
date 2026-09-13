# API reference

## `install`

```python
install(app, config=None, storage=None, **options) -> Profiler
```

Adds the middleware and, unless `mount_path` is `None`, mounts the viewer.
Returns a [`Profiler`](#profiler) handle.

Raises `RuntimeError` if the app already has the profiler installed —
installing twice would add a second middleware and a second mount, recording
every request twice into two separate stores. Raises `TypeError` on an unknown
option.

## `Profiler`

The handle returned by `install()`.

| Member | Returns | Notes |
| --- | --- | --- |
| `.storage` | `Storage` | The backend in use |
| `.config` | `ProfilerConfig` | The resolved config |
| `.profiles` | `list[Profile]` | Every retained profile |
| `.search(filters=None, **kw)` | `Page` | One page, filtered by the backend |
| `.slowest(count=10)` | `list[Profile]` | Slowest first |
| `.summary()` | `list[PathSummary]` | Per-route rows |
| `.statements(limit=100)` | `list[StatementSummary]` | Across all requests |
| `.exclude(*paths)` | `None` | Stop recording those paths, after install |
| `.clear()` | `None` | Discard the history |
| `.close()` | `None` | Release the backend |

!!! warning "`.profiles` hydrates everything"

    On a large `SQLiteStorage` that is every statement of every request.
    Prefer `.slowest()` or `.search()` when the history is big.

## `profiler_exclude` / `profiler_include`

Decorators for one endpoint, decisive in both directions and outranking every
pattern. Work above or below the route decorator. See
[Filtering](../guide/filtering.md).

```python
from asgi_profiler import profiler_exclude, profiler_include
```

## `Profile`

One recorded request.

**Fields:** `id`, `method`, `path`, `route`, `query_string`, `status_code`,
`duration_ms`, `recorded_at`, `request_headers`, `response_headers`, `client`,
`queries`, `query_count`, `query_ms`, `duplicate_count`, `error_count`

**Properties:** `full_path`, `group`, `python_ms`, `query_groups`,
`status_class`

`group` is the route pattern — `/users/{user_id}` — and is what the summary
aggregates on. `full_path` includes the query string.

## `Query`

One statement: `sql`, `params`, `duration_ms`, `stack`, `error`.

SQL longer than 4000 characters is truncated with a marker, because a bulk
insert is a single statement tens of kilobytes long and `max_requests` bounds
how many requests are kept, not how big they are.

## `QueryGroup`

Statements with identical SQL, grouped within one request. Produced by
`group_queries(profile.queries)`.

**Fields:** `sql`, `count`, `total_ms`, `max_ms`, `first`, `first_failure`,
`params`, `distinct_params`, `failures`, `positions`, `call_sites`

**Properties:** `avg_ms`, `error`, `failed`, `is_duplicate`, `operation`,
`stack`

!!! note "`call_sites` is a number, not a list"

    It counts *distinct* stacks. More than one means the same SQL is issued
    from several places, so the single `stack` only explains some of the
    executions. `stack` is the frame list.

## `PathSummary`

A summary row: `path`, `method`, `count`, `total_ms`, `max_ms`,
`total_queries`, `total_duplicates`, `total_errors`, `p50_ms`, `p95_ms`,
`p99_ms`, plus `avg_ms` and `avg_queries`.

## `Filters`

The viewer's filter state, understood by both backends.

`q`, `method`, `status` (`"ok"` / `"warn"` / `"err"`), `route`, `min_ms`,
`only_duplicates`, `only_errors`, `order` (`"recent"` / `"slowest"` /
`"queries"` / `"sql"`).

## `Storage`

A Protocol: `add`, `get`, `list`, `count`, `clear`, `search`, `summarise`,
`statements`. Subclass `BaseStorage` to get the last three for free. See
[Storage](../guide/storage.md).

## Lower-level pieces

`ProfilerMiddleware`, `build_viewer`, `install_sql_hooks`, `route_pattern`,
`group_queries` and `aggregate_statements` are exported for anyone assembling
the parts by hand rather than calling `install()`.
