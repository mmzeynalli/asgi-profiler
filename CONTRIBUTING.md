# Contributing

```console
uv sync --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check src/asgi_profiler
uv run bandit -q -r src/asgi_profiler
```

`--all-extras` matters: without it FastAPI is missing and the tests covering
FastAPI route handling skip in silence.

`ruff format` also formats the Python inside fenced blocks in the markdown
files, so a README snippet can fail the format check.

## Ground rules

- **Every bug fix gets a regression test that fails without the fix.**
  `tests/test_regressions.py` is organised that way; keep it that way.
- **Behaviour that differs between storage backends is a bug.**
  `tests/test_storage_and_viewer.py` parametrises the same assertions over
  `MemoryStorage` and `SQLiteStorage`. A new backend should join that fixture.
- **Comments explain *why*, not *what*.** The non-obvious choices in this
  codebase — pure ASGI over `BaseHTTPMiddleware`, a mutable contextvar, class-
  level engine listeners, walking the stack by hand — are all load-bearing, and
  each is commented where it lives. If you change one of them, change the
  comment in the same commit.
- Keep the profiler's own overhead in mind: anything added to the per-query
  path is paid for on every statement your users run.
- Branch names should be `feat/`, `chore/`, `fix/`
