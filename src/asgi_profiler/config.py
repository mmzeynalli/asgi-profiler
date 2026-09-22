"""Configuration."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from starlette.requests import Request

from .detectors import (
    BLOCKING_MS,
    N_PLUS_ONE_COUNT,
    N_PLUS_ONE_MS,
    SLOW_QUERY_MS,
    TYPES,
    Settings,
)
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
        slow_request_ms / slow_query_ms: thresholds for *highlighting* a row
            in the viewer. Cosmetic, and separate from the detector
            thresholds below, which decide whether something is reported as a
            problem.
        detectors: which detectors to run, by name. `None`, the default, runs
            all of them; `()` runs none. Names are in
            `asgi_profiler.detectors.TYPES`. An unknown name is ignored, so a
            detector removed in a later release cannot break your config.
        n_plus_one_count: how many identical statements make an N+1.
        n_plus_one_ms: how much non-overlapping time they must occupy before
            the repetition is worth reporting. Zero by default -- a
            development database answers in microseconds what production
            answers in milliseconds, so a duration floor here hides the
            N+1s that have not shipped yet. Raise it when profiling against
            production-sized data.
        slow_query_issue_ms: a single statement at or over this is reported as
            a problem. Higher than `slow_query_ms`, which only colours a row.
        blocking_query_ms: how much loop-blocking time a request needs
            before it is reported. Zero by default, for the same reason
            `n_plus_one_ms` is: the mistake is architectural, and waiting for
            it to be slow enough means finding it in production.
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
    detectors: Sequence[str] | None = None
    n_plus_one_count: int = N_PLUS_ONE_COUNT
    n_plus_one_ms: float = N_PLUS_ONE_MS
    slow_query_issue_ms: float = SLOW_QUERY_MS
    blocking_query_ms: float = BLOCKING_MS
    authorize: Callable[[Request], bool] | None = None

    def detector_settings(self) -> Settings:
        """The thresholds, in the form the detectors take.

        Built per request rather than cached, because it is a five-field
        frozen dataclass and because caching it would make a threshold changed
        after `install()` silently not apply -- the kind of bug that costs an
        afternoon to find and nothing to avoid.
        """
        return Settings(
            enabled=self.detectors,
            n_plus_one_count=self.n_plus_one_count,
            n_plus_one_ms=self.n_plus_one_ms,
            slow_query_ms=self.slow_query_issue_ms,
            blocking_ms=self.blocking_query_ms,
            stacks=self.capture_stacks,
        )

    def unknown_detectors(self) -> tuple[str, ...]:
        """Configured names no detector answers to. For a warning, not an error."""
        if self.detectors is None:
            return ()
        return tuple(name for name in self.detectors if name not in TYPES)

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
