"""Framework introspection: what path was this, and what route matched it.

Separate from the middleware on purpose. This is the most fragile code in the
package -- it reimplements the resolution `starlette.routing.Router.app` does,
and it reads scope keys whose presence differs between Starlette, FastAPI and
whatever mounts them. Keeping it here bounds the blast radius of the next
Starlette release to one file, instead of burying it in the module people open
to understand request capture.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.routing import Match
from starlette.types import Scope

logger = logging.getLogger("asgi_profiler")

__all__ = ["request_path", "route_path", "route_pattern"]


def request_path(scope: Scope) -> str:
    """The full path of the request, including any mount or proxy prefix.

    ASGI servers and Starlette disagree about whether `scope["path"]` already
    carries `root_path`. Modern Starlette leaves the full path in `path` and
    *adds* `root_path`; older servers strip it. Concatenating unconditionally
    turns `/api/users` into `/api/api/users` behind a mount -- which corrupts
    every recorded path and, worse, stops the viewer's own mount from being
    excluded, so the profiler starts profiling its own UI.
    """
    path = scope.get("path", "/")
    root = (scope.get("root_path") or "").rstrip("/")
    if not root:
        return path
    if path == root or path.startswith(root + "/"):
        return path  # already absolute
    return f"{root}{path}"


def route_path(scope: Scope) -> str:
    """The path *relative to the mount*, which is what routes are declared in.

    `exclude_paths` and `mount_path` are written against the application's own
    routing table, so they have to be matched against this rather than against
    the absolute path -- otherwise mounting the whole app under `/api` stops
    the viewer excluding itself.
    """
    path = request_path(scope)
    root = (scope.get("root_path") or "").rstrip("/")
    if root and (path == root or path.startswith(root + "/")):
        return path[len(root) :] or "/"
    return path


def _pattern_from_router(scope: Scope, root_path: str) -> str:
    """Ask the router which route matched, and take that route's own path.

    Exact, unlike reconstructing from `path_params`: it survives typed
    converters (`{uid:int}` matching `007`), values that collide with a
    literal segment (`/users/users`), and nested mounts, none of which the
    parameter values alone can tell you.

    `root_path` must be the value from *before* the request was routed. The
    router mutates the scope in place -- `Mount.matches` appends its prefix to
    `root_path` -- and `scope["router"]` is always the outermost router. Replay
    the match against the top-level routes with a mount-widened `root_path` and
    every route in the table is evaluated against the wrong path, which is how
    a request to `/api/u/7` comes back labelled with an unrelated top-level
    `/u/{name}`.
    """
    routes = getattr(scope.get("router"), "routes", None)
    if not routes:
        return ""
    return _walk_routes(routes, {**scope, "root_path": root_path}, "")


def _walk_routes(routes: Any, scope: Scope, prefix: str) -> str:
    """Mirror the router's own resolution: first full match wins.

    Taking the first match rather than the best one is deliberate -- it is
    exactly what `Router.app` does, so we name the route that actually ran.
    """
    partial = ""
    for route in routes:
        try:
            match, child = route.matches(scope)
        except Exception:  # pragma: no cover - a custom route that raises
            # Skipping is right -- naming a route must never break the request
            # being profiled -- but skipping in total silence is how a whole
            # router quietly reports as unmatched. Say so at debug level; this
            # costs nothing until it actually happens.
            logger.debug("route %r raised while matching", route, exc_info=True)
            continue
        if match is Match.NONE:
            continue

        path = getattr(route, "path", "")
        nested = getattr(route, "routes", None)
        if nested:  # a Mount or Host: descend with the child scope it hands us
            found = _walk_routes(nested, {**scope, **child}, prefix + path)
            if found:
                return found
            if match is Match.FULL:
                # Nothing inside matched -- a raw ASGI app behind the mount,
                # say StaticFiles. The mount path is the most specific pattern
                # available, and grouping every file under it is what we want.
                return prefix + path
            continue

        if match is Match.FULL:
            return prefix + path if path else ""
        if not partial and path:  # a method mismatch: a 405 still has a route
            partial = prefix + path
    return partial


def _pattern_from_params(scope: Scope) -> str:
    """Fallback: rewrite the parameter values out of the concrete path.

    Substitutes whole segments, left to right, consuming each only once -- a
    plain `str.replace` gets `/2/users/2` wrong and collapses two parameters
    that happen to share a value. Still approximate: a converted value whose
    `str()` differs from the text in the URL (`{uid:int}` matching `007`)
    cannot be located this way, which is why the router is asked first.
    """
    params = scope.get("path_params") or {}
    if not params:
        return ""

    segments = request_path(scope).split("/")
    consumed: set[int] = set()
    leftover: list[tuple[str, str]] = []
    for name, value in params.items():
        text = str(value)
        if not text:
            continue  # an empty value would match the '' before the leading /
        for index, segment in enumerate(segments):
            if index not in consumed and segment == text:
                segments[index] = f"{{{name}}}"
                consumed.add(index)
                break
        else:
            leftover.append((name, text))

    path = "/".join(segments)
    # A `path:`-style converter matches across segments, so fall back to a
    # substring rewrite for anything not placed above.
    for name, text in leftover:
        if text in path:
            path = path.replace(text, f"{{{name}}}", 1)
    return path


def route_pattern(scope: Scope, root_path: str = "") -> str:
    """The matched route pattern, e.g. ``/users/{user_id}``.

    Frameworks disagree about what they leave behind in the scope. FastAPI
    sets ``scope["route"]`` with a `path` / `path_format`; **plain Starlette
    sets no `route` key at all** -- only `endpoint`, `path_params` and the
    `router` itself. So take the pattern where it is offered, ask the router
    where it is not, and fall back to reconstructing it from the parameters.

    Called after the downstream app returns: the router mutates the scope dict
    in place, so by then it is populated.

    Whatever the source, the pattern is returned absolute -- prefixed to match
    `Profile.path`. A route declared inside a mounted sub-app knows itself only
    as `/dashboard/{tab}`, and two sub-apps mounted at different prefixes will
    happily declare the same relative path, so grouping on the bare pattern
    merges unrelated endpoints into one summary row.
    """
    route = scope.get("route")
    pattern = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(pattern, str) and pattern:
        # `root_path` has by now accumulated every mount prefix crossed.
        return (scope.get("root_path") or "").rstrip("/") + pattern

    if scope.get("endpoint") is not None and not scope.get("path_params"):
        # A concrete route with nothing to substitute: the path *is* the
        # pattern, and skipping the router walk keeps the common case free.
        # Requires an endpoint -- a raw ASGI mount and a 404 both arrive here
        # with no parameters, and neither should be taken at face value.
        return request_path(scope)

    found = _pattern_from_router(scope, root_path)
    if found:
        return root_path.rstrip("/") + found
    return _pattern_from_params(scope)
