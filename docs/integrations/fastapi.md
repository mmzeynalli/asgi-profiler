# FastAPI

FastAPI is a Starlette application, so everything works the same way. Two
things are worth knowing.

## Route patterns come for free

FastAPI puts the matched route on `scope["route"]`, so `/users/1` and
`/users/2` group as `/users/{user_id}` without the profiler having to work
for it. On plain Starlette the router is replayed to recover the same thing.

Typed converters are preserved: `/files/{path:path}` groups as written.

## Mounted sub-applications

Route patterns stay correct under mounts, including the prefix:

```python
api = FastAPI()
app.mount("/api", api)
```

A request to `/api/users/1` records as `/api/users/{user_id}`.

!!! note "`root_path` is handled"

    Behind a proxy that sets `root_path`, the recorded path is the one your
    application sees, not the doubled `/api/api/...` that a naive
    implementation produces.

## Dependencies and background tasks

Queries issued inside a dependency are attributed to the request that ran it,
because the capture follows the contextvar rather than the call site.

Queries issued from a `BackgroundTask` after the response has been sent are
**not** recorded against that request — the profile is closed by then.
