# How it works

The non-obvious choices, and why they are the way they are.

## Pure ASGI middleware, not `BaseHTTPMiddleware`

`BaseHTTPMiddleware` runs the downstream application in a separate task, and
contextvars set there do not propagate back. Since SQL capture works by
putting a list in a contextvar and letting the engine listeners append to it,
that would lose every query. The middleware is written directly against the
ASGI interface instead.

## A mutable list in the contextvar, not a value

SQLAlchemy's synchronous work often runs in a worker thread — anyio copies the
context into it. A copied context means a *rebound* value is invisible to the
original task, but a *mutated* object is shared. So the contextvar holds a
list and the listeners append to it.

## Listeners on the `Engine` class, not an engine

Events are attached to SQLAlchemy's `Engine` class, so every engine in the
process is covered without the application handing one over. That is why
SQLModel needs no special support, and why async engines work: `AsyncEngine`
drives a sync `Engine` underneath, and that is where the events live.

Three listeners: `before_cursor_execute`, `after_cursor_execute`, and
`handle_error`. The third matters — a statement that raises never reaches
`after_cursor_execute`, so without it a failure is invisible *and* its start
time is stranded on the connection forever.

## Walking across the greenlet boundary

Under an async engine, SQLAlchemy runs the DBAPI call inside a greenlet it
spawned. That greenlet's stack is five frames of SQLAlchemy and nothing else —
the application frame that issued the query lives in the *parent* greenlet,
which `walk_stack` cannot see.

Without crossing that boundary, per-query stacks are silently empty for every
async user, which is most of them. So the walk follows greenlet parents.

## Early-exit stack walking

`traceback.extract_stack()` formats the entire stack before you look at it —
about 220 µs at 120 frames. The walk here stops as soon as it has
`stack_depth` application frames, and skips library frames as it goes, which
costs about 9 µs. Twenty times cheaper, on every statement.

## Route patterns

`/users/1` and `/users/2` must aggregate as `/users/{user_id}`, or the summary
is a list of individual URLs instead of endpoints.

FastAPI puts the matched route on `scope["route"]`. Plain Starlette does not,
so the router is replayed against the scope to find which route matched —
taking the first full match, exactly as `Router.app` does, so the profiler
names the route that actually ran.

Replaying is done against a copy of the original `root_path`: the router
mutates it in place while descending into mounts, so reusing the live scope
produces the wrong pattern under a `Mount`.

## Writes off the request path

See [Storage](guide/storage.md) — `SQLiteStorage` enqueues to a background
writer rather than committing on the event loop, which took the p99 from
roughly 8.7 ms to about 100 µs.

## The profiler never breaks the application

A failure while recording is logged and swallowed. A profiler is not
load-bearing: if it cannot write a profile, the request still gets served.

The one thing it will not do quietly is under-report. A dropped batch is
logged as a warning, because a diagnostic tool that silently records less than
it should is the failure nobody would think to check for.
