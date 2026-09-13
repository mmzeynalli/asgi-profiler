"""Configuration."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from starlette.requests import Request

from .rules import compile_pattern

#: Header names never stored, in lower case.
DEFAULT_REDACTED_HEADERS = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
)


@dataclass
class ProfilerConfig:
    """Options for :func:`asgi_profiler.install`.

    Args:
        mount_path: where the viewer is mounted. Every link the viewer emits is
            relative to the mount, so this can be anything. `None` records
            without mounting the viewer at all -- for when the pages are served
            somewhere else, such as inside a SQLAdmin instance.
        max_requests: how many requests to keep.
        exclude_paths: paths that are never recorded, matched on segment
            boundaries -- `/health` excludes `/health` and `/health/db` but not
            `/healthcheck`. The viewer's own mount is added automatically.
        include_regex: only record paths this matches. `None`, the default,
            records everything. Matched with `search`, so anchor with `^` for
            the strict reading. Note these are regular expressions, not globs:
            a bare `"*"` is a syntax error, and `".*"` is how you spell
            "everything" if you would rather be explicit than pass `None`.
        exclude_regex: never record paths this matches. Applied before
            `include_regex`. `None`, the default, excludes nothing.

            Both are overridden per route by `@profiler_include` and
            `@profiler_exclude`, which are decisive in either direction.
        capture_stacks: record the application frames behind each query. This
            is what makes an N+1 actionable, at some cost per query.
        stack_depth: how many frames to keep.
        slow_request_ms / slow_query_ms: thresholds for highlighting.
        capture_headers: store request/response headers.
        redacted_headers: header names replaced with "<redacted>".
        response_header: name of a response header carrying the profile id, so
            you can jump from a slow response straight to its trace. Set to
            `None` to add nothing.
        page_size: rows per page in the viewer.
        statement_limit: how many rows the cross-request statement view shows.
        authorize: called for every viewer request. Return False to deny.
            **There is no default authentication.** Guard the viewer yourself,
            or keep it off outside development.
    """

    mount_path: str | None = "/profiler"
    max_requests: int = 500
    exclude_paths: Sequence[str] = ("/favicon.ico",)
    include_regex: str | None = None
    exclude_regex: str | None = None
    capture_stacks: bool = True
    stack_depth: int = 8
    slow_request_ms: float = 500.0
    slow_query_ms: float = 50.0
    capture_headers: bool = True
    redacted_headers: Sequence[str] = DEFAULT_REDACTED_HEADERS
    response_header: str | None = "x-profiler-id"
    page_size: int = 50
    statement_limit: int = 100
    authorize: Callable[[Request], bool] | None = None

    def compiled_patterns(self) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
        """`(include, exclude)`, compiled. Invalid patterns raise here."""
        return (
            compile_pattern(self.include_regex, "include_regex"),
            compile_pattern(self.exclude_regex, "exclude_regex"),
        )

    def build_excludes(self) -> tuple[str, ...]:
        paths = list(self.exclude_paths)
        if self.mount_path is not None:
            # An empty `mount_path` would rstrip to "" and then be read as "/",
            # which excludes the entire application -- the profiler would
            # record nothing at all and look broken rather than misconfigured.
            mount = self.mount_path.rstrip("/")
            if not mount:
                raise ValueError(
                    'mount_path="" would exclude every path in the application. '
                    "Use mount_path=None to skip mounting the viewer."
                )
            paths.insert(0, mount)
        return tuple(dict.fromkeys(paths))
