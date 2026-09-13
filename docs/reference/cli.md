# CLI

A capture written to SQLite can be browsed without booting the application
that produced it — record in staging, read the file on your laptop.

```console
asgi-profiler profiler.db
```

```
1,284 requests in profiler.db
viewer on http://127.0.0.1:8080/
```

Equivalent to `python -m asgi_profiler profiler.db`.

## Options

| Option | Default | |
| --- | --- | --- |
| `database` | — | Path to a capture (required) |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8080` | Port |
| `--page-size` | `50` | Rows per page |

## Requires uvicorn

Serving needs a server, which is not a dependency of the library:

```console
pip install uvicorn
```

The capture is validated *before* the server import, so a bad file is reported
as a bad file rather than as a missing uvicorn.

## Read-only

The file is opened read-only. Opening a capture to look at it can never
rewrite or truncate it, and an incompatible schema is reported as
`IncompatibleCapture` rather than being migrated underneath you.
