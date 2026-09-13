"""Choosing what gets profiled: the regexes and the two decorators.

The precedence is the interesting part. Four mechanisms can disagree about one
request, and "whichever `if` happens to run first" is not a specification, so
every combination is pinned here.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asgi_profiler import (
    ProfilerConfig,
    install,
    profiler_exclude,
    profiler_include,
)


async def plain(request):
    return PlainTextResponse("ok")


@profiler_exclude
async def excluded(request):
    return PlainTextResponse("ok")


@profiler_include
async def included(request):
    return PlainTextResponse("ok")


def build(**options):
    app = Starlette(
        routes=[
            Route("/api/widgets", plain),
            Route("/api/gadgets", plain),
            Route("/health", plain),
            Route("/health/db", plain),
            Route("/healthcheck", plain),
            Route("/metrics", excluded),
            Route("/internal/deep", included),
        ]
    )
    return app, install(app, **options)


def recorded(profiler, client, *paths):
    for path in paths:
        client.get(path)
    return [p.path for p in profiler.profiles]


ALL = (
    "/api/widgets",
    "/api/gadgets",
    "/health",
    "/health/db",
    "/healthcheck",
    "/metrics",
    "/internal/deep",
)


# ------------------------------------------------------------ the defaults
def test_everything_is_recorded_by_default_except_decorated_routes():
    """The shape most applications want: profile all, carve out a few."""
    app, profiler = build()
    with TestClient(app) as client:
        paths = recorded(profiler, client, *ALL)

    assert "/api/widgets" in paths
    assert "/health" in paths
    assert "/metrics" not in paths, "@profiler_exclude was ignored"
    assert "/internal/deep" in paths


# ------------------------------------------------------------- exclude_regex
def test_exclude_regex_drops_matching_paths():
    app, profiler = build(exclude_regex=r"^/health")
    with TestClient(app) as client:
        paths = recorded(profiler, client, *ALL)

    assert "/health" not in paths
    assert "/health/db" not in paths
    assert "/healthcheck" not in paths, "search is a substring match by design"
    assert "/api/widgets" in paths


def test_exclude_regex_can_be_anchored_for_the_strict_reading():
    app, profiler = build(exclude_regex=r"^/health$")
    with TestClient(app) as client:
        paths = recorded(profiler, client, "/health", "/health/db", "/healthcheck")

    assert "/health" not in paths
    assert "/health/db" in paths
    assert "/healthcheck" in paths


# ------------------------------------------------------------- include_regex
def test_include_regex_records_only_what_it_matches():
    app, profiler = build(include_regex=r"^/api/")
    with TestClient(app) as client:
        paths = recorded(profiler, client, *ALL)

    assert "/api/widgets" in paths
    assert "/api/gadgets" in paths
    assert "/health" not in paths


def test_dot_star_is_the_explicit_spelling_of_everything():
    app, profiler = build(include_regex=r".*")
    with TestClient(app) as client:
        paths = recorded(profiler, client, "/api/widgets", "/health")

    # newest first, which is how the store keeps them
    assert set(paths) == {"/api/widgets", "/health"}


# ---------------------------------------------------------------- precedence
def test_a_decorator_beats_include_regex():
    """`@profiler_include` carves a route back in that the pattern dropped."""
    app, profiler = build(include_regex=r"^/api/")
    with TestClient(app) as client:
        paths = recorded(profiler, client, "/api/widgets", "/health", "/internal/deep")

    assert "/internal/deep" in paths, "@profiler_include did not override include_regex"
    assert "/health" not in paths


def test_a_decorator_beats_exclude_regex():
    app, profiler = build(exclude_regex=r"^/internal")
    with TestClient(app) as client:
        paths = recorded(profiler, client, "/internal/deep")

    assert "/internal/deep" in paths


def test_profiler_exclude_beats_a_matching_include_regex():
    app, profiler = build(include_regex=r".*")
    with TestClient(app) as client:
        paths = recorded(profiler, client, "/metrics", "/api/widgets")

    assert "/metrics" not in paths
    assert "/api/widgets" in paths


def test_exclude_regex_is_applied_before_include_regex():
    """Both match: exclude wins, so the order in the docs is the real one."""
    app, profiler = build(include_regex=r"^/api/", exclude_regex=r"gadgets")
    with TestClient(app) as client:
        paths = recorded(profiler, client, "/api/widgets", "/api/gadgets")

    assert paths == ["/api/widgets"]


def test_the_viewer_is_never_recorded_even_with_a_matching_include_regex():
    """Otherwise reading the history appends to the history."""
    app, profiler = build(include_regex=r".*")
    with TestClient(app) as client:
        client.get("/api/widgets")
        client.get("/profiler/")
        client.get("/profiler/summary")

    assert [p.path for p in profiler.profiles] == ["/api/widgets"]


# ------------------------------------------------------------- the fast path
def _contextvar_seen_during(app, client, path):
    """Was a capture context set up for this request, inside the request?

    Checked from the endpoint, not from the test: the contextvar is set in the
    request's own context, so reading it out here would always be `None` and
    the assertion would be vacuous whatever the middleware did.
    """
    return client.get(path).text


def test_with_an_include_present_an_excluded_request_is_recorded_then_discarded():
    """This module defines an `@profiler_include`, so the shortcut is off.

    `any_includes()` is process-global and monotonic: once anything anywhere
    is decorated, no request can be skipped before routing, because a late
    decorator might still flip the decision. The work is done and thrown away.
    """
    from asgi_profiler import instrument, rules

    assert rules.any_includes(), "this module's @profiler_include should be counted"

    async def report(request):
        return PlainTextResponse(str(instrument.current_queries.get() is not None))

    app = Starlette(routes=[Route("/health", report)])
    profiler = install(app, exclude_regex=r"^/health")

    with TestClient(app) as client:
        assert _contextvar_seen_during(app, client, "/health") == "True"

    assert profiler.profiles == [], "it was captured, but it must not be stored"


def test_without_any_include_an_excluded_request_skips_the_work_entirely():
    """Run in a subprocess, because `any_includes()` cannot be un-set.

    This is the case that matters in production -- nobody uses
    `@profiler_include` and a health check costs nothing at all.
    """
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent("""
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient
        from asgi_profiler import install, instrument, rules

        assert not rules.any_includes()

        async def report(request):
            return PlainTextResponse(str(instrument.current_queries.get() is not None))

        app = Starlette(routes=[Route("/health", report), Route("/api", report)])
        profiler = install(app, exclude_regex=r"^/health")
        with TestClient(app) as client:
            print("excluded:", client.get("/health").text)
            print("included:", client.get("/api").text)
        print("stored:", [p.path for p in profiler.profiles])
    """)
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "excluded: False" in result.stdout, (
        "an excluded request still set up a capture context, so the "
        f"pre-routing shortcut is not working:\n{result.stdout}"
    )
    assert "included: True" in result.stdout
    assert "stored: ['/api']" in result.stdout


# ------------------------------------------------------------------- errors
def test_a_glob_is_rejected_with_an_explanation_not_a_regex_traceback():
    with pytest.raises(ValueError) as exc:
        ProfilerConfig(include_regex="*").compiled_patterns()

    message = str(exc.value)
    assert "glob" in message
    assert '".*"' in message, "the message should say how to spell 'everything'"


def test_an_invalid_pattern_names_the_field_it_came_from():
    with pytest.raises(ValueError, match="exclude_regex"):
        ProfilerConfig(exclude_regex="(unclosed").compiled_patterns()


# ------------------------------------------ the bug ruff caught, pinned down
def test_an_endpoint_that_raises_still_raises():
    """The decision moved into `finally`, where a bare `return` eats exceptions.

    A handler that raised would have been reported as a clean response, and the
    application's own error handling would never have run.
    """

    async def boom(request):
        raise RuntimeError("kaboom")

    app = Starlette(routes=[Route("/boom", boom)])
    install(app, exclude_regex=r"^/boom")

    with TestClient(app) as client, pytest.raises(RuntimeError, match="kaboom"):
        client.get("/boom")


def test_an_excluded_endpoint_that_raises_still_raises():
    async def boom(request):
        raise RuntimeError("kaboom")

    boom = profiler_exclude(boom)
    app = Starlette(routes=[Route("/boom", boom)])
    install(app)

    with TestClient(app) as client, pytest.raises(RuntimeError, match="kaboom"):
        client.get("/boom")


# ------------------------------------------------------------ live reconfig
def test_patterns_can_be_changed_after_install():
    app, profiler = build()
    with TestClient(app) as client:
        client.get("/health")
        assert len(profiler.profiles) == 1

        profiler.config.exclude_regex = r"^/health"
        client.get("/health")
        client.get("/api/widgets")

    # newest first: the /health after the reconfigure was dropped, the one
    # before it was kept
    assert [p.path for p in profiler.profiles] == ["/api/widgets", "/health"]
