# Contributing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test,dev]" fastapi
pytest
ruff check . && ruff format --check .
```

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

## Releasing

1. Update `CHANGELOG.md` and the version in `pyproject.toml` and
   `src/starlette_profiler/__init__.py`.
2. `python -m build && twine check dist/*`
3. Tag `vX.Y.Z` and push.
