# Storage

Two backends ship. Both satisfy the same `Storage` protocol, so the viewer and
the middleware do not care which one you use.

## MemoryStorage

The default. A bounded ring buffer, newest first, thread-safe.

```python
from asgi_profiler import MemoryStorage, install

install(app, storage=MemoryStorage(max_requests=1000))
```

Per-process: with multiple `uvicorn` workers each holds its own history, and
everything is lost on restart. Fine for local development, which is most of
the time.

## SQLiteStorage

Survives a restart and is shared by every worker in a multi-process
deployment.

```python
from asgi_profiler import SQLiteStorage, install

install(app, storage=SQLiteStorage("profiler.db"))
```

### Writes happen off the request path

A synchronous SQLite insert on the event loop is usually fine and occasionally
catastrophic — a commit that lands on a checkpoint, or a lock wait that
`sqlite3` implements as a blocking sleep. Measured against a synchronous
store, that tail was a p99 of roughly 8.7 ms per request.

So `SQLiteStorage` hands the profile to a background writer thread and
returns. Enqueueing costs about a microsecond, and the p99 drops to around
100 µs. The writer coalesces profiles that arrive close together into one
transaction rather than paying for a transaction per request.

Pass `background=False` to write inline instead — mostly useful in tests,
where determinism beats latency.

### Reads never see a stale page

Any read flushes pending writes first, so a request you just made is in the
list when you open the viewer.

### Retention

The cap is exact and trimming costs the overshoot rather than the whole
window, so a 20 000-request cap is no more expensive per insert than a 500
one.

## Writing your own

`Storage` is a Protocol, so a Redis or Postgres backend can be dropped in
without touching the middleware or the viewer. Subclass `BaseStorage` and
implement the five core methods — `add`, `get`, `list`, `count`, `clear` —
and searching, paging, summarising and statement aggregation come for free:

```python
from asgi_profiler import BaseStorage


class RedisStorage(BaseStorage):
    def add(self, profile): ...
    def get(self, profile_id): ...
    def list(self, *, limit=None, offset=0): ...
    def count(self): ...
    def clear(self): ...
```

Override `search`, `summarise` or `statements` where your backend can do them
better than Python can.
