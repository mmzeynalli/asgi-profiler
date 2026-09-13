# Quickstart

## FastAPI

```python
from fastapi import FastAPI
from asgi_profiler import install

app = FastAPI()
install(app)
```

Open `/profiler`. Every request your application has handled is there.

## Starlette

Identical — `install()` takes any Starlette application:

```python
from starlette.applications import Starlette
from asgi_profiler import install

app = Starlette(routes=[...])
install(app)
```

## SQLModel

Nothing extra to do. `sqlmodel.create_engine` returns a SQLAlchemy `Engine`
and `sqlmodel.Session` subclasses `sqlalchemy.orm.Session`, so the same cursor
events fire. Async engines work unchanged too.

## Call it before the app serves

`install()` adds middleware, and ASGI middleware cannot be added once the
application has started. Call it at import time, next to where you build the
app — not inside a startup handler.

## Keeping history across restarts

The default store is in-memory and per-process, so it empties on restart and
each `uvicorn` worker holds its own. For anything longer-lived:

```python
from asgi_profiler import SQLiteStorage, install

install(app, storage=SQLiteStorage("profiler.db"))
```

See [Storage](../guide/storage.md).

## Reading it outside the app

A capture written to SQLite can be browsed without booting the application
that produced it:

```console
asgi-profiler profiler.db
```

See the [CLI reference](../reference/cli.md).
