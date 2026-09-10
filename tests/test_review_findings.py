"""Regressions for the defects an adversarial review of 0.2.0 turned up.

Each of these failed before the fix.
"""

from __future__ import annotations

import asyncio
import math

import pytest
from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from starlette_profiler import (
    Filters,
    MemoryStorage,
    Profile,
    Query,
    SQLiteStorage,
    install,
)
from starlette_profiler.viewer import _same_origin


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widget"
    id = Column(Integer, primary_key=True)
    name = Column(String)


def make(idx: int, **kwargs) -> Profile:
    kwargs.setdefault("path", f"/p{idx}")
    profile = Profile(id=f"{idx:012d}", method="GET", **kwargs)
    profile.finalise()
    return profile


# ------------------------------------- root_path is not applied twice
def test_mounted_app_does_not_double_the_prefix():
    async def endpoint(request):
        return JSONResponse({})

    child = Starlette(routes=[Route("/users/{name}", endpoint)])
    profiler = install(child)
    parent = Starlette(routes=[Mount("/api", app=child)])

    with TestClient(parent) as client:
        assert client.get("/api/users/alice").status_code == 200
        assert client.get("/api/profiler/").status_code == 200

    assert [p.path for p in profiler.profiles] == ["/api/users/alice"]


def test_viewer_stays_excluded_when_the_app_is_mounted():
    """Otherwise the profiler profiles its own UI, forever."""

    async def endpoint(request):
        return JSONResponse({})

    child = Starlette(routes=[Route("/thing", endpoint)])
    profiler = install(child)
    parent = Starlette(routes=[Mount("/api", app=child)])

    with TestClient(parent) as client:
        client.get("/api/profiler/")
        client.get("/api/profiler/summary")
        client.get("/api/thing")

    assert [p.path for p in profiler.profiles] == ["/api/thing"]


# ------------------------------------- exact route patterns
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/u/007", "/u/{uid:int}"),  # str(7) != "007"
        ("/x/1.50", "/x/{v:float}"),  # str(1.5) != "1.50"
        ("/users/users", "/users/{name}"),  # value collides with a literal
        ("/files/", "/files/{rest:path}"),  # empty parameter value
    ],
)
def test_route_pattern_is_exact(url, expected):
    async def endpoint(request):
        return JSONResponse({})

    app = Starlette(
        routes=[
            Route("/u/{uid:int}", endpoint),
            Route("/x/{v:float}", endpoint),
            Route("/users/{name}", endpoint),
            Route("/files/{rest:path}", endpoint),
        ]
    )
    profiler = install(app)

    with TestClient(app) as client:
        client.get(url)

    assert profiler.profiles[0].route == expected


def test_mounted_sub_app_routes_keep_their_prefix():
    """Two endpoints under different mounts must not merge into one row."""

    async def endpoint(request):
        return JSONResponse({})

    a = Starlette(routes=[Route("/things/{name}", endpoint)])
    b = Starlette(routes=[Route("/things/{name}", endpoint)])
    root = Starlette(routes=[Mount("/one", app=a), Mount("/two", app=b)])
    profiler = install(root)

    with TestClient(root) as client:
        client.get("/one/things/x")
        client.get("/two/things/y")

    assert {p.route for p in profiler.profiles} == {
        "/one/things/{name}",
        "/two/things/{name}",
    }


# ------------------------------------- async-engine stacks
def test_async_engine_queries_get_application_stacks(tmp_path):
    """The query runs in a greenlet whose stack is pure SQLAlchemy.

    The frame that issued it lives in the parent greenlet; without crossing
    that boundary every async user gets empty stacks.
    """
    db = tmp_path / "async.db"
    sync = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(sync)
    sync.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")

    async def widgets(request):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return JSONResponse({})

    app = Starlette(routes=[Route("/widgets", widgets)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get("/widgets")

    stack = profiler.profiles[0].queries[0].stack
    assert stack, "async engines must still get a stack"
    assert any("widgets" in frame for frame in stack)


# ------------------------------------- counters cannot go stale
def test_counters_match_the_recorded_queries(tmp_path):
    """A fire-and-forget task inherits the context and keeps appending."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bg.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    done = asyncio.Event()

    async def endpoint(request):
        async def later():
            await asyncio.sleep(0.01)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.execute(text("SELECT 1"))
            done.set()

        asyncio.get_running_loop().create_task(later())
        return JSONResponse({})

    app = Starlette(routes=[Route("/bg", endpoint)])
    profiler = install(app)

    async def drive():
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.get("/bg")
        await asyncio.wait_for(done.wait(), timeout=5)
        await asyncio.sleep(0.05)

    asyncio.run(drive())

    profile = profiler.profiles[0]
    assert profile.query_count == len(profile.queries)
    total = sum(q.duration_ms for q in profile.queries)
    assert profile.query_ms == pytest.approx(total)


# ------------------------------------- the two backends agree
@pytest.fixture
def both(tmp_path):
    sqlite = SQLiteStorage(tmp_path / "cmp.db")
    yield MemoryStorage(), sqlite
    sqlite.close()


def test_backends_agree_on_unicode_search(both):
    memory, sqlite = both
    for store in both:
        store.add(make(1, path="/p", query_string="q=Ünïcode"))
        store.add(make(2, path="/other"))

    for store in both:
        found = [p.path for p in store.search(Filters(q="ünï")).items]
        assert found == ["/p"], f"{type(store).__name__} folded differently"


def test_backends_agree_on_a_nonsense_min_ms(both):
    for store in both:
        for i in range(5):
            store.add(make(i, duration_ms=float(i)))

    filters = Filters.from_params({"min_ms": "nan"})
    assert filters.min_ms is None or math.isfinite(filters.min_ms)
    counts = {store.search(filters).total for store in both}
    assert len(counts) == 1, "backends disagreed on ?min_ms=nan"


def test_backends_agree_on_tie_ordering(both):
    for store in both:
        for i in range(6):
            store.add(make(i, duration_ms=10.0))

    def ids(store, number):
        page = store.search(Filters(order="slowest"), page=number, size=3)
        return [p.id for p in page.items]

    pages = [[ids(store, n) for n in (1, 2)] for store in both]
    assert pages[0] == pages[1]


def test_sqlite_trim_is_exact_across_a_sequence_gap(tmp_path):
    store = SQLiteStorage(tmp_path / "trim.db", max_requests=5)
    try:
        for i in range(12):
            store.add(make(i))
        assert store.count() == 5
        store.add(make(9))  # re-adding an existing id burns a seq
        for i in range(20, 24):
            store.add(make(i))
        assert store.count() == 5
    finally:
        store.close()


# ------------------------------------- CSRF allow-list
def test_same_site_is_not_treated_as_same_origin():
    """A sibling subdomain is a different origin, and can auto-submit a form."""

    def request_with(**headers):
        raw = [(k.encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "method": "POST"})

    assert not _same_origin(request_with(**{"sec-fetch-site": "same-site"}))
    assert not _same_origin(request_with(**{"sec-fetch-site": "cross-site"}))
    assert _same_origin(request_with(**{"sec-fetch-site": "same-origin"}))
    assert _same_origin(request_with(**{"sec-fetch-site": "none"}))
    assert _same_origin(request_with())  # curl / httpx


def test_clear_rejects_a_sibling_subdomain():
    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get("/")
        denied = client.post(
            "/profiler/clear",
            headers={
                "sec-fetch-site": "same-site",
                "origin": "https://evil.example.com",
            },
            follow_redirects=False,
        )
        assert denied.status_code == 403
    assert profiler.profiles


# ------------------------------------- routing under mounts (round 2)
def test_route_pattern_under_a_mount_is_the_mounted_route():
    """The router mutates `root_path` in place; replaying the match with the
    widened value evaluates every top-level route against the wrong path."""

    async def endpoint(request):
        return JSONResponse({})

    child = Starlette(routes=[Route("/u/{uid:int}", endpoint)])
    root = Starlette(routes=[Mount("/api", app=child), Route("/u/{name}", endpoint)])
    profiler = install(root)

    with TestClient(root) as client:
        client.get("/api/u/7")
        client.get("/u/bob")

    by_path = {p.path: p.route for p in profiler.profiles}
    assert by_path["/api/u/7"] == "/api/u/{uid:int}"
    assert by_path["/u/bob"] == "/u/{name}"


def test_mounted_routes_keep_converter_exactness():
    """`/api/u/007` and `/api/u/7` must land on one summary row."""

    async def endpoint(request):
        return JSONResponse({})

    child = Starlette(routes=[Route("/u/{uid:int}", endpoint)])
    root = Starlette(routes=[Mount("/api", app=child)])
    profiler = install(root)

    with TestClient(root) as client:
        client.get("/api/u/007")
        client.get("/api/u/7")

    assert {p.route for p in profiler.profiles} == {"/api/u/{uid:int}"}


def test_a_catch_all_route_does_not_absorb_mounted_requests():
    """An SPA catch-all declared after a mount used to swallow every route."""
    fastapi = pytest.importorskip("fastapi")

    admin = fastapi.FastAPI()

    @admin.get("/dashboard/{tab}")
    def dashboard(tab: str):
        return {}

    app = fastapi.FastAPI()
    app.mount("/admin", admin)

    @app.get("/{page:path}")
    def spa(page: str):
        return {}

    profiler = install(app)

    with TestClient(app) as client:
        client.get("/admin/dashboard/users")
        client.get("/anything/else")

    by_path = {p.path: p.route for p in profiler.profiles}
    assert by_path["/admin/dashboard/users"] == "/admin/dashboard/{tab}"
    # FastAPI normalises converters out of `path_format`; Starlette keeps them
    assert by_path["/anything/else"] == "/{page}"


def test_mount_does_not_collapse_to_the_bare_mount_path():
    async def endpoint(request):
        return JSONResponse({})

    child = Starlette(routes=[Route("/api/thing", endpoint)])
    root = Starlette(routes=[Mount("/api", app=child)])
    profiler = install(root)

    with TestClient(root) as client:
        client.get("/api/api/thing")

    assert profiler.profiles[0].route == "/api/api/thing"


def test_a_raw_asgi_mount_records_each_path():
    """Documents the behaviour rather than asserting a nicety.

    A mount to a bare ASGI app (StaticFiles, say) exposes no route to name, so
    each asset is its own summary row. Acceptable: this is a development tool
    and asset paths are finite. Exclude the mount if it is noisy.
    """
    from starlette.staticfiles import StaticFiles

    async def endpoint(request):
        return JSONResponse({})

    app = Starlette(
        routes=[
            Route("/", endpoint),
            Mount("/static", app=StaticFiles(directory=".", check_dir=False)),
        ]
    )
    profiler = install(app)

    with TestClient(app) as client:
        client.get("/static/nope-a.css")
        client.get("/static/nope-b.css")

    static = {p.route for p in profiler.profiles if p.path.startswith("/static")}
    assert static == {"/static/nope-a.css", "/static/nope-b.css"}

    # and excluding the mount silences them entirely
    other = Starlette(
        routes=[Mount("/static", app=StaticFiles(directory=".", check_dir=False))]
    )
    quiet = install(other, exclude_paths=["/static"], mount_path="/prof")
    with TestClient(other) as client:
        client.get("/static/nope-c.css")
    assert quiet.profiles == []


def test_exclude_paths_do_not_fire_on_a_colliding_mount_prefix():
    """Mounting the app under /health must not exclude the whole app."""

    async def endpoint(request):
        return JSONResponse({})

    child = Starlette(routes=[Route("/thing", endpoint)])
    profiler = install(child, exclude_paths=["/health"])
    parent = Starlette(routes=[Mount("/health", app=child)])

    with TestClient(parent) as client:
        assert client.get("/health/thing").status_code == 200

    assert [p.path for p in profiler.profiles] == ["/health/thing"]


def test_sqlite_trim_holds_across_repeated_sequence_gaps(tmp_path):
    store = SQLiteStorage(tmp_path / "gaps.db", max_requests=5)
    try:
        for i in range(10):
            store.add(make(i))
        assert store.count() == 5
        for _ in range(4):  # each re-add burns a seq
            store.add(make(9))
            assert store.count() == 5
    finally:
        store.close()


def test_a_huge_search_term_does_not_blow_up(both):
    for store in both:
        store.add(make(1))
    filters = Filters.from_params({"q": "x" * 50001})
    totals = {store.search(filters).total for store in both}
    assert totals == {0}


# ------------------------------------- coverage gaps found reviewing 0.2.0
def test_uninstall_sql_hooks_actually_stops_capture(tmp_path):
    """Exported and advertised for teardown, but nothing verified it worked."""
    from sqlalchemy import create_engine, text

    from starlette_profiler import install_sql_hooks, uninstall_sql_hooks

    engine = create_engine(f"sqlite:///{tmp_path / 'u.db'}")

    async def query(request):
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return JSONResponse({})

    app = Starlette(routes=[Route("/q", query)])
    profiler = install(app)
    with TestClient(app) as client:
        client.get("/q")
    assert profiler.profiles[0].query_count == 1

    uninstall_sql_hooks()
    try:
        other = Starlette(routes=[Route("/q", query)])
        quiet = install(other, mount_path="/p-uninstall")
        # install() re-attaches, so detach again behind its back
        uninstall_sql_hooks()
        with TestClient(other) as client:
            client.get("/q")
        assert quiet.profiles[0].query_count == 0, "listeners were still attached"
    finally:
        install_sql_hooks()


def test_order_by_sql_time(both):
    """`_ORDERINGS` has four entries; only three were ever exercised."""
    for store in both:
        store.add(make(1, path="/light", queries=[Query("SELECT 1", "()", 1.0)]))
        store.add(make(2, path="/heavy", queries=[Query("SELECT 2", "()", 90.0)]))
        store.add(make(3, path="/none"))

    for store in both:
        ordered = [p.path for p in store.search(Filters(order="sql")).items]
        assert ordered[0] == "/heavy", type(store).__name__


def test_route_filter_on_both_backends(both):
    for store in both:
        store.add(make(1, path="/users/1", route="/users/{id}"))
        store.add(make(2, path="/users/2", route="/users/{id}"))
        store.add(make(3, path="/other", route="/other"))

    for store in both:
        page = store.search(Filters(route="/users/{id}"))
        assert page.total == 2, type(store).__name__
        assert {p.path for p in page.items} == {"/users/1", "/users/2"}


def test_profiler_clear_and_helpers():
    async def endpoint(request):
        return JSONResponse({})

    app = Starlette(routes=[Route("/x/{n}", endpoint)])
    profiler = install(app)
    with TestClient(app) as client:
        for i in range(5):
            client.get(f"/x/{i}")

    assert len(profiler.slowest(2)) == 2
    assert profiler.summary()[0].count == 5
    assert profiler.statements() == []
    assert profiler.search().total == 5

    profiler.clear()
    assert profiler.profiles == []


def test_memory_storage_len_matches_count():
    store = MemoryStorage()
    assert len(store) == 0 == store.count()
    store.add(make(1))
    assert len(store) == 1 == store.count()
