"""Storage backends, pagination, the response header, and the viewer prefix."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asgi_profiler import (
    Filters,
    MemoryStorage,
    Profile,
    ProfilerConfig,
    Query,
    SQLiteStorage,
    install,
)


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widget"
    id = Column(Integer, primary_key=True)
    name = Column(String)


def make_profile(
    idx: int,
    *,
    path: str = "/x",
    method: str = "GET",
    route: str = "",
    status: int = 200,
    duration: float = 1.0,
    queries: list[Query] | None = None,
) -> Profile:
    profile = Profile(
        id=f"{idx:012d}",
        method=method,
        path=path,
        route=route,
        status_code=status,
        duration_ms=duration,
        queries=queries or [],
    )
    profile.finalise()
    return profile


DUPLICATED = [
    Query(sql="SELECT 1", params="()", duration_ms=1.0),
    Query(sql="SELECT 1", params="()", duration_ms=1.0),
]
FAILED = [Query(sql="SELECT bad", params="()", duration_ms=1.0, error="boom")]


@pytest.fixture(params=["memory", "sqlite"])
def storage(request, tmp_path):
    """Every behaviour below is asserted against both backends."""
    if request.param == "memory":
        yield MemoryStorage(max_requests=500)
    else:
        store = SQLiteStorage(tmp_path / "profiler.db", max_requests=500)
        yield store
        store.close()


# ------------------------------------------------------------ the Protocol
def test_add_get_count_clear(storage):
    assert storage.count() == 0
    storage.add(make_profile(1, path="/a"))
    storage.add(make_profile(2, path="/b"))

    assert storage.count() == 2
    assert storage.get("000000000001").path == "/a"
    assert storage.get("nope") is None
    assert [p.path for p in storage.list()] == ["/b", "/a"]  # newest first

    storage.clear()
    assert storage.count() == 0
    assert storage.list() == []


def test_list_limit_and_offset(storage):
    for i in range(5):
        storage.add(make_profile(i, path=f"/{i}"))

    assert [p.path for p in storage.list(limit=2)] == ["/4", "/3"]
    assert [p.path for p in storage.list(limit=2, offset=2)] == ["/2", "/1"]


def test_history_is_bounded(tmp_path):
    for store in (
        MemoryStorage(max_requests=3),
        SQLiteStorage(tmp_path / "b.db", max_requests=3),
    ):
        for i in range(10):
            store.add(make_profile(i, path=f"/{i}"))
        assert store.count() == 3
        assert [p.path for p in store.list()] == ["/9", "/8", "/7"]


def test_queries_survive_a_round_trip(storage):
    query = Query(
        sql="SELECT 1",
        params="(1,)",
        duration_ms=2.5,
        stack=["/app/views.py:10 in index"],
        error=None,
    )
    storage.add(make_profile(1, queries=[query, query]))

    loaded = storage.get("000000000001")
    assert loaded.query_count == 2
    assert loaded.queries[0].stack == ["/app/views.py:10 in index"]
    assert loaded.queries[0].is_duplicate
    assert loaded.duplicate_count == 1


# ------------------------------------------------------------------ search
def test_search_filters(storage):
    storage.add(make_profile(1, path="/alpha", method="GET", status=200, duration=10))
    storage.add(make_profile(2, path="/beta", method="POST", status=404, duration=800))
    storage.add(make_profile(3, path="/gamma", status=500, queries=list(DUPLICATED)))
    storage.add(make_profile(4, path="/delta", queries=list(FAILED)))

    def paths(**kwargs):
        return sorted(p.path for p in storage.search(Filters(**kwargs)).items)

    assert paths(q="alph") == ["/alpha"]
    assert paths(method="POST") == ["/beta"]
    assert paths(status="err") == ["/gamma"]
    assert paths(status="warn") == ["/beta"]
    assert paths(status="ok") == ["/alpha", "/delta"]
    assert paths(min_ms=500) == ["/beta"]
    assert paths(only_duplicates=True) == ["/gamma"]
    assert paths(only_errors=True) == ["/delta"]


def test_search_ordering(storage):
    storage.add(make_profile(1, path="/slow", duration=900))
    storage.add(make_profile(2, path="/fast", duration=1))
    storage.add(make_profile(3, path="/chatty", duration=50, queries=list(DUPLICATED)))

    assert storage.search(Filters(order="slowest")).items[0].path == "/slow"
    assert storage.search(Filters(order="queries")).items[0].path == "/chatty"
    assert storage.search(Filters(order="recent")).items[0].path == "/chatty"


def test_search_paginates(storage):
    for i in range(25):
        storage.add(make_profile(i, path=f"/p{i:02d}"))

    first = storage.search(Filters(), page=1, size=10)
    assert len(first.items) == 10
    assert first.total == 25
    assert first.pages == 3
    assert first.number == 1
    assert first.has_next and not first.has_previous
    assert (first.first_index, first.last_index) == (1, 10)

    last = storage.search(Filters(), page=3, size=10)
    assert len(last.items) == 5
    assert last.has_previous and not last.has_next
    assert (last.first_index, last.last_index) == (21, 25)

    # out of range clamps rather than 500s
    assert storage.search(Filters(), page=99, size=10).number == 3
    assert storage.search(Filters(), page=-4, size=10).number == 1

    # pages do not overlap and cover everything
    seen = []
    for n in (1, 2, 3):
        seen += [p.path for p in storage.search(Filters(), page=n, size=10).items]
    assert len(set(seen)) == 25


def test_search_pagination_respects_filters(storage):
    for i in range(30):
        storage.add(make_profile(i, path=f"/p{i:02d}", method="GET" if i % 2 else "POST"))

    page = storage.search(Filters(method="POST"), page=1, size=5)
    assert page.total == 15
    assert all(p.method == "POST" for p in page.items)


def test_summarise_groups_by_route(storage):
    for i in range(3):
        storage.add(make_profile(i, path=f"/users/{i}", route="/users/{user_id}", duration=10))
    storage.add(make_profile(9, path="/health", duration=1))

    rows = {(r.method, r.path): r for r in storage.summarise()}
    assert rows[("GET", "/users/{user_id}")].count == 3
    assert rows[("GET", "/users/{user_id}")].total_ms == pytest.approx(30)
    assert rows[("GET", "/health")].count == 1


def test_sqlite_survives_reopening(tmp_path):
    path = tmp_path / "persist.db"
    store = SQLiteStorage(path)
    store.add(make_profile(1, path="/kept", queries=list(DUPLICATED)))
    store.close()

    reopened = SQLiteStorage(path)
    try:
        assert reopened.count() == 1
        profile = reopened.get("000000000001")
        assert profile.path == "/kept"
        assert profile.query_count == 2
        assert profile.duplicate_count == 1
    finally:
        reopened.close()


def test_sqlite_search_q_escapes_wildcards(tmp_path):
    store = SQLiteStorage(tmp_path / "esc.db")
    try:
        store.add(make_profile(1, path="/a%b"))
        store.add(make_profile(2, path="/axxb"))
        assert [p.path for p in store.search(Filters(q="a%b")).items] == ["/a%b"]
    finally:
        store.close()


# ------------------------------------------------------------- integration
def test_install_with_sqlite_storage(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)

    async def widgets(request):
        with maker() as session:
            session.scalars(select(Widget)).all()
        return JSONResponse({})

    app = Starlette(routes=[Route("/widgets", widgets)])
    store = SQLiteStorage(tmp_path / "profiles.db")
    profiler = install(app, storage=store)

    try:
        with TestClient(app) as client:
            client.get("/widgets")
            page = client.get("/profiler/")
            assert page.status_code == 200
            assert "/widgets" in page.text

            detail = client.get(f"/profiler/request/{profiler.profiles[0].id}")
            assert detail.status_code == 200
            assert "SELECT" in detail.text
    finally:
        store.close()


def test_viewer_paginates():
    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/thing/{n}", endpoint)])
    install(app, page_size=5)

    with TestClient(app) as client:
        for i in range(12):
            client.get(f"/thing/{i}")

        first = client.get("/profiler/")
        assert "Page 1 of 3" in first.text
        assert first.text.count("/profiler/request/") == 5

        second = client.get("/profiler/?page=2")
        assert "Page 2 of 3" in second.text

        assert client.get("/profiler/?page=nonsense").status_code == 200
        assert "Page 1 of 3" in client.get("/profiler/?page=0").text


def test_pagination_links_keep_the_filters():
    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/thing/{n}", endpoint)])
    install(app, page_size=2)

    with TestClient(app) as client:
        for i in range(6):
            client.get(f"/thing/{i}")
        page = client.get("/profiler/?order=slowest&q=thing")
        assert "order=slowest" in page.text
        assert "q=thing" in page.text


def test_response_carries_the_profile_id():
    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint)])
    profiler = install(app)

    with TestClient(app) as client:
        response = client.get("/")
        profile_id = response.headers["x-profiler-id"]
        assert profile_id == profiler.profiles[0].id
        # and it is a working link
        assert client.get(f"/profiler/request/{profile_id}").status_code == 200


def test_response_header_can_be_disabled():
    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint)])
    install(app, response_header=None)

    with TestClient(app) as client:
        assert "x-profiler-id" not in client.get("/").headers


def test_custom_response_header_name():
    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint)])
    install(app, response_header="X-Trace-Id")

    with TestClient(app) as client:
        assert client.get("/").headers["x-trace-id"]


def test_capture_headers_can_be_disabled():
    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint)])
    profiler = install(app, capture_headers=False)

    with TestClient(app) as client:
        client.get("/", headers={"x-trace": "abc"})

    profile = profiler.profiles[0]
    assert profile.request_headers == {}
    assert profile.response_headers == {}


def test_viewer_prefix_is_used_when_root_path_is_absent():
    """Litestar-style mounts do not set root_path; the prefix must cover it."""
    from asgi_profiler import build_viewer

    storage = MemoryStorage()
    storage.add(make_profile(1, path="/a"))
    viewer = build_viewer(storage, ProfilerConfig(), prefix="/_perf")

    with TestClient(viewer) as client:  # mounted at the root: no root_path
        page = client.get("/")
        assert "/_perf/static/profiler.css" in page.text
        assert "/_perf/summary" in page.text


def test_root_path_wins_over_prefix():
    """A proxy prefix must not be clobbered by the configured mount path."""
    from asgi_profiler import build_viewer

    storage = MemoryStorage()
    viewer = build_viewer(storage, ProfilerConfig(), prefix="/profiler")

    with TestClient(viewer, root_path="/api/profiler") as client:
        page = client.get("/")
        assert "/api/profiler/static/profiler.css" in page.text
