# Contributing

```console
uv sync --all-extras
uv run pre-commit install
```

That is the whole setup. `pre-commit install` wires up both stages at once:
formatting, linting, types, security and the lockfile check on every commit,
and the test suite on push.

`--all-extras` matters: without it FastAPI is missing and the tests covering
FastAPI route handling skip in silence.

To run things by hand, or to see what the hooks will do before committing:

```console
uv run pre-commit run --all-files              # everything the commit hook runs
uv run pre-commit run --all-files --hook-stage pre-push   # the above, plus pytest
```

The individual commands, if you want one of them on its own:

```console
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check src/asgi_profiler
uv run bandit -q -r src/asgi_profiler
```

Two things worth knowing about the hooks:

- The linters are `repo: local` and go through `uv run`, so they are the
  versions in `uv.lock` — the same ones CI uses. The upstream `ruff-pre-commit`
  mirror pins its own version, which drifts from the lockfile and lets a commit
  pass locally and fail in CI on identical code.
- `ruff format` also formats the Python inside fenced blocks in the markdown
  files, so a README snippet can fail the format check. The hook fixes it in
  place rather than just reporting it.

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

1. `uv version --bump patch` (or `minor` / `major`). Nothing to mirror:
   `__version__` is read from the installed distribution.
2. Date the new section in `CHANGELOG.md`.
3. Tag `X.Y.Z` (no `v` prefix) and push. `release.yml` re-runs the suite on every supported
   Python, builds with `uv build --no-sources`, verifies the tag matches the
   packaged version, and publishes with `uv publish` over Trusted Publishing.
