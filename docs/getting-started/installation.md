# Installation

```console
pip install asgi-profiler
```

Requires Python 3.10 or newer.

## Dependencies

Three, all of which a Starlette application using SQLAlchemy already has:

| Package           | Why                                     |
| ----------------- | --------------------------------------- |
| `starlette>=0.35` | The middleware and the viewer           |
| `sqlalchemy>=2.0` | The engine events SQL capture hangs off |
| `jinja2>=3.0`     | The viewer's templates                  |

!!! note "Why `starlette>=0.35` specifically"

    0.35 is where `scope["path"]` stopped being stripped of the mount prefix.
    Below it the profiler records the wrong route for every mounted
    application — silently, by writing bad data rather than raising. The
    floor was set by bisecting: 0.30 through 0.34 fail the suite, 0.35 passes
    it.

## Optional extras

```console
pip install "asgi-profiler[sqladmin]"
```

Adds [SQLAdmin](../integrations/sqladmin.md) so the profiler can be registered
as a view inside an existing admin instead of serving its own page.

## Verifying the install

```console
python -c "import asgi_profiler; print(asgi_profiler.__version__)"
```
