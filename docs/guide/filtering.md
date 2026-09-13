# Choosing what gets profiled

By default everything is recorded except the viewer's own pages. Four
mechanisms narrow that down, and they are resolved in a fixed order.

## The common shape

Profile everything, carve out the noise:

```python
from asgi_profiler import install, profiler_exclude


@app.get("/health")
@profiler_exclude
def health():
    return {"status": "ok"}


install(app)
```

A health check hit every second would otherwise be most of your history. For
one or two routes, the decorator is the whole configuration you need.

## Patterns

```python
install(app, exclude_regex=r"^/(health|metrics)")
```

```python
install(app, include_regex=r"^/api/")
```

| Option | Default | Effect |
| --- | --- | --- |
| `include_regex` | `None` | Record only paths this matches. `None` means everything. |
| `exclude_regex` | `None` | Never record paths this matches. Applied first. |

!!! warning "These are regular expressions, not globs"

    `include_regex="*"` is not "match everything" — it is a syntax error.
    Python's `re` rejects a bare `*` with *nothing to repeat*.

    Spell everything as `".*"`, or just leave it as `None`, which already
    means that. Passing a glob raises a `ValueError` that says so rather than
    surfacing the raw `re.error`.

Patterns are matched with `search`, not `fullmatch`, so `"/health"` also
catches `/health/db` — which is what people expect a filter to do. Anchor it
when you want the strict reading:

```python
install(app, exclude_regex=r"^/health$")  # /health only, not /health/db
```

Matching is against the **request path**, not the route pattern — `/users/1`,
not `/users/{user_id}`. The path is known before routing, which is what lets
an excluded request cost nothing at all.

## Decorators

```python
from asgi_profiler import profiler_exclude, profiler_include
```

| Decorator | Effect |
| --- | --- |
| `@profiler_exclude` | Never record this endpoint |
| `@profiler_include` | Always record it, whatever the patterns say |

Both work above or below the route decorator — the route registers the same
function object either way:

```python
@app.get("/health")
@profiler_exclude
def health(): ...


@profiler_exclude  # equivalent
@app.get("/health")
def health(): ...
```

`@profiler_include` is for carving a route back in that a pattern dropped:

```python
install(app, include_regex=r"^/api/")


@app.get("/internal/report")
@profiler_include  # profiled even though it is not under /api/
def report(): ...
```

## Precedence

Most specific wins:

1. **The viewer's own mount** — never recorded, and nothing overrides it.
   A profiler that records itself fills its history with the pages you opened
   to read the history.
2. **A decorator on the endpoint** — decisive in both directions. It is the
   most specific statement of intent available, so it outranks every pattern.
3. **`exclude_paths` or `exclude_regex` matches** — not recorded.
4. **`include_regex` is set and does not match** — not recorded.
5. Otherwise, recorded.

So when `include_regex` and `exclude_regex` both match a path, the exclusion
wins; and a decorator beats both.

## What it costs

Steps 3 and 4 only need the path, which is available *before* routing. An
excluded request is skipped outright — no profile allocated, no queries
captured, no stacks walked. That matters, because the endpoints people
exclude are usually the ones being hit constantly.

Step 2 is different. Starlette only sets `scope["endpoint"]` *during* routing,
so a decorator can only be honoured after the response. To keep the cheap path
in the normal case, the profiler tracks whether **any** `@profiler_include`
exists in the process:

- **None anywhere** (the overwhelmingly common case) — no late decision can
  flip an exclusion back on, so excluded requests are skipped before routing.
- **At least one** — the shortcut is no longer sound, so every request is
  captured and the excluded ones are discarded at the end.

You pay for `@profiler_include` only if you use it. `@profiler_exclude` alone
never costs anything.

!!! note "It is process-wide, not per-application"

    One `@profiler_include` anywhere in the process turns the shortcut off for
    every application in it. This only changes *when* the decision is made,
    never *what* is recorded, so the behaviour is identical either way.

## Changing your mind later

The patterns are re-read when they change, so they can be adjusted after
`install()`:

```python
profiler = install(app)
profiler.config.exclude_regex = r"^/health"
profiler.exclude("/internal")  # adds to exclude_paths
```

This is how the [SQLAdmin integration](../integrations/sqladmin.md) keeps the
admin out of its own history: the admin does not exist yet when `install()`
runs.
