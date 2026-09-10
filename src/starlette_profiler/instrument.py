"""SQL capture.

Listeners are attached to SQLAlchemy's `Engine` **class**, so every engine in
the process is covered without the application handing us one. That is what
makes SQLModel work for free: `sqlmodel.create_engine` returns a SQLAlchemy
`Engine`, and `sqlmodel.Session` subclasses `sqlalchemy.orm.Session`, so the
same cursor events fire. Async engines are covered too -- `AsyncEngine` drives
a sync `Engine` underneath, and that is where the events live.
"""

from __future__ import annotations

import contextlib
import contextvars
import time
import traceback
from collections.abc import Iterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

from .models import MAX_SQL_CHARS, Query

try:  # SQLAlchemy pulls greenlet in for its asyncio support
    import greenlet
except ImportError:  # pragma: no cover - sync-only install
    greenlet = None  # type: ignore[assignment]

#: The list being filled for the in-flight request.
#:
#: It has to be a *mutable* object rather than a value: SQLAlchemy's sync work
#: often runs in a worker thread (anyio copies the context into it), so appends
#: made there must land in an object the request task already holds.
current_queries: contextvars.ContextVar[list[Query] | None] = contextvars.ContextVar(
    "starlette_profiler_queries", default=None
)

_IGNORED_FRAME_PARTS = (
    "/sqlalchemy/",
    "\\sqlalchemy\\",
    "/sqlmodel/",
    "\\sqlmodel\\",
    "/starlette_profiler/",
    "\\starlette_profiler\\",
    "/anyio/",
    "\\anyio\\",
    "/asyncio/",
    "\\asyncio\\",
    "/greenlet/",
    "\\greenlet\\",
    "/gevent/",
    "\\gevent\\",
    "/concurrent/futures/",
    "\\concurrent\\futures\\",
    "/threading.py",
    "\\threading.py",
)

#: Live capture settings, read by the listeners at event time.
#:
#: A dict rather than closure arguments so that a later `install()` can change
#: them. The listeners are attached to the `Engine` class exactly once, but the
#: settings they honour must stay reconfigurable -- otherwise the first
#: `install()` in a process would silently dictate `capture_stacks` for every
#: application in it, including across tests.
_settings: dict[str, Any] = {"capture_stacks": True, "stack_depth": 8}

_installed = False
_listeners: list[tuple[str, Any]] = []

#: Cap on how far up the stack to walk before giving up on finding
#: application frames. Guards against pathological recursion.
_MAX_STACK_WALK = 250


def install(*, capture_stacks: bool = True, stack_depth: int = 8) -> None:
    """Attach the cursor listeners. Safe to call more than once.

    Calling it again updates `capture_stacks` and `stack_depth` in place; the
    listeners themselves are only attached the first time.
    """
    global _installed
    _settings["capture_stacks"] = capture_stacks
    _settings["stack_depth"] = stack_depth
    if _installed:
        return

    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        # A stack: a single connection can nest executions.
        conn.info.setdefault("_profiler_started", []).append(time.perf_counter())

    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        started = conn.info.get("_profiler_started")
        if not started:
            return
        _record(statement, parameters, (time.perf_counter() - started.pop()) * 1000)

    def _error(context) -> None:  # noqa: ANN001
        """A statement that raised never reaches `after_cursor_execute`.

        Without this the failure is invisible in the UI *and* its start time
        is stranded on the connection forever, which on a pooled connection
        that sees repeated errors is an unbounded list.
        """
        conn = getattr(context, "connection", None)
        if conn is None or context.statement is None:
            return  # a connect-level failure: no cursor execution to close out
        started = conn.info.get("_profiler_started")
        if not started:
            return
        _record(
            context.statement,
            context.parameters,
            (time.perf_counter() - started.pop()) * 1000,
            error=_format_error(context.original_exception),
        )

    for name, listener in (
        ("before_cursor_execute", _before),
        ("after_cursor_execute", _after),
        ("handle_error", _error),
    ):
        event.listen(Engine, name, listener)
        _listeners.append((name, listener))

    _installed = True


def uninstall() -> None:
    """Detach the listeners. Mainly for test isolation."""
    global _installed
    for name, listener in _listeners:
        with contextlib.suppress(Exception):  # already gone
            event.remove(Engine, name, listener)
    _listeners.clear()
    _installed = False


def _record(
    statement: Any, parameters: Any, elapsed_ms: float, error: str | None = None
) -> None:
    queries = current_queries.get()
    if queries is None:
        return  # outside any request: startup, migrations, a shell

    queries.append(
        Query(
            sql=_normalise(statement),
            params=_format_params(parameters),
            duration_ms=elapsed_ms,
            stack=(
                _capture_stack(_settings["stack_depth"])
                if _settings["capture_stacks"]
                else []
            ),
            error=error,
        )
    )


def _normalise(statement: Any) -> str:
    text = " ".join(str(statement).split())
    # `max_requests` bounds how many requests are kept, not how big they are.
    # An ORM bulk insert is a single statement tens of kilobytes long, and one
    # endpoint doing that can hold hundreds of megabytes in the ring buffer.
    if len(text) > MAX_SQL_CHARS:
        return text[:MAX_SQL_CHARS] + f" ... [truncated, {len(text)} chars]"
    return text


def _format_params(parameters: Any) -> str:
    try:
        text = repr(parameters)
    except Exception:  # a parameter with a hostile __repr__
        return "<unrepresentable>"
    return text if len(text) <= 500 else text[:500] + " ..."


def _format_error(exc: BaseException | None) -> str:
    if exc is None:
        return "error"
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 500 else text[:500] + " ..."


def _walk_all_frames() -> Iterator[tuple[str, int, str]]:
    """Every frame from here outwards, crossing greenlet boundaries.

    Under an async engine, SQLAlchemy runs the DBAPI call inside a greenlet it
    spawned. That greenlet's stack is five frames of SQLAlchemy and nothing
    else -- the application frame that issued the query lives in the *parent*
    greenlet, which `walk_stack` cannot see. Without this, per-query stacks are
    silently empty for every async user, which is most of them.
    """
    for here, lineno in traceback.walk_stack(None):
        yield here.f_code.co_filename, lineno, here.f_code.co_name

    if greenlet is None:
        return
    current = getattr(greenlet.getcurrent(), "parent", None)
    while current is not None:
        frame: Any = getattr(current, "gr_frame", None)
        while frame is not None:
            yield frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name
            frame = frame.f_back
        current = getattr(current, "parent", None)


def _capture_stack(depth: int) -> list[str]:
    """Application frames that led to this query, innermost last.

    This is the thing that makes an N+1 actionable: it names the line in *your*
    code that triggered it.

    Walks outward from the caller and stops as soon as `depth` application
    frames have been found. `traceback.extract_stack()` would be the obvious
    call, but it unwinds the *entire* stack and does a `linecache` source
    lookup per frame, then throws almost all of it away -- which in a real
    async request stack of 100+ frames costs an order of magnitude more per
    query than this does.
    """
    frames: list[str] = []
    for seen, (filename, lineno, name) in enumerate(_walk_all_frames()):
        if seen >= _MAX_STACK_WALK:
            break
        if any(part in filename for part in _IGNORED_FRAME_PARTS):
            continue
        frames.append(f"{filename}:{lineno} in {name}")
        if len(frames) >= depth:
            break
    frames.reverse()  # innermost last, matching a traceback
    return frames
