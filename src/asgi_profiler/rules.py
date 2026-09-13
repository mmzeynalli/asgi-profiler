"""Deciding which requests get recorded.

Four mechanisms, in one place so the precedence between them is written down
once rather than inferred from the order of `if` statements in the middleware:

1. The viewer's own mount is never recorded. Not overridable -- a profiler that
   records itself fills its history with pages you opened to read the history.
2. `@profiler_include` / `@profiler_exclude` on the endpoint. Decisive. A
   decorator on the function is the most specific statement of intent
   available, so it beats every pattern.
3. `exclude_paths` or `exclude_regex` matching -> not recorded.
4. `include_regex` set and not matching -> not recorded.
5. Otherwise recorded.

Steps 3 and 4 only need the path, which the middleware has before routing, so
an excluded route can be skipped without allocating a profile or capturing a
single query. Step 2 needs `scope["endpoint"]`, which Starlette only sets
*during* routing -- so a decorator can only be honoured after the fact.

That tension is what `any_includes()` is for. If nothing in the process is
decorated with `@profiler_include`, no late decision can ever flip a `False`
back to `True`, and the cheap pre-routing skip is safe. The moment one exists,
the middleware stops skipping early and decides afterwards instead. You pay
for the feature only if you use it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["profiler_exclude", "profiler_include"]

F = TypeVar("F", bound=Callable[..., Any])

#: Set on the endpoint function by the decorators. `True` forces recording,
#: `False` forbids it, absent means "no opinion, fall through to the patterns".
MARKER = "_asgi_profiler_record"

#: How many endpoints in this process carry `@profiler_include`.
_include_count = 0


def profiler_include(func: F) -> F:
    """Always record this endpoint, whatever the patterns say.

    For carving a route back in when `include_regex` or `exclude_regex` would
    otherwise drop it:

        @app.get("/health/deep")
        @profiler_include
        def deep_health():
            ...

    Works in either decorator order -- above or below the route decorator --
    because the route registers the same function object either way.
    """
    global _include_count
    if getattr(func, MARKER, None) is not True:
        _include_count += 1
    setattr(func, MARKER, True)
    return func


def profiler_exclude(func: F) -> F:
    """Never record this endpoint, whatever the patterns say.

        @app.get("/health")
        @profiler_exclude
        def health():
            ...

    With the default `include_regex` (everything), decorating a handful of
    routes with this is the whole configuration most applications need.
    """
    setattr(func, MARKER, False)
    return func


def any_includes() -> bool:
    """Has anything in this process been marked `@profiler_include`?

    Lets the middleware keep skipping excluded requests before routing in the
    common case where nothing can override that decision later.
    """
    return _include_count > 0


def endpoint_decision(scope: Any) -> bool | None:
    """The decorator's verdict for the endpoint that handled this request.

    `None` means the endpoint had no decorator, or routing never reached one
    (a 404, or a mounted sub-application that does not set `endpoint`).
    """
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return None
    decision = getattr(endpoint, MARKER, None)
    return decision if isinstance(decision, bool) else None


def compile_pattern(pattern: str | None, field: str) -> re.Pattern[str] | None:
    """Compile a user-supplied pattern, or explain why it will not compile.

    `re.error: nothing to repeat at position 0` is what Python says about a
    bare `"*"`, and it tells someone who was thinking in glob patterns nothing
    useful. This says the thing they need to hear instead.
    """
    if pattern is None:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        hint = ""
        if pattern in {"*", "**", "*.*"}:
            hint = (
                ' It looks like a glob. These are regular expressions, so "*" '
                'on its own is a syntax error -- use ".*" to match everything, '
                f"or leave {field} as None, which already means everything."
            )
        raise ValueError(
            f"{field}={pattern!r} is not a valid regular expression ({exc}).{hint}"
        ) from exc


def path_allows(
    path: str,
    *,
    excludes: tuple[str, ...],
    include_re: re.Pattern[str] | None,
    exclude_re: re.Pattern[str] | None,
) -> bool:
    """Steps 3 and 4: everything decidable from the path alone.

    Patterns are matched with `search`, not `fullmatch`, so `"/health"` catches
    `/health/db` the way people expect a filter to behave. Anchor with `^` and
    `$` when you want the strict reading.
    """
    if _prefix_match(path, excludes):
        return False
    if exclude_re is not None and exclude_re.search(path):
        return False
    return not (include_re is not None and not include_re.search(path))


def _prefix_match(path: str, prefixes: tuple[str, ...]) -> bool:
    """Match on segment boundaries.

    A bare `startswith` would make an excluded `/profiler` swallow the
    application's own `/profiler-admin`, silently and with no error -- which
    reads to the user as "the profiler is broken".
    """
    for prefix in prefixes:
        trimmed = prefix.rstrip("/")
        if not trimmed:
            return True  # excluded at the root
        if path == trimmed or path.startswith(trimmed + "/"):
            return True
    return False
