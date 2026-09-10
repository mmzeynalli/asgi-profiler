"""The diagnostic layer: collapsed duplicates, statements, JSON, percentiles."""

from __future__ import annotations

import contextlib
import json

import pytest
from sqlalchemy import Column, Integer, String, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from starlette_profiler import (
    MemoryStorage,
    Profile,
    Query,
    SQLiteStorage,
    aggregate_statements,
    group_queries,
    install,
    summarise,
)


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "author"
    id = Column(Integer, primary_key=True)
    name = Column(String)


class Book(Base):
    __tablename__ = "book"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    author_id = Column(Integer, nullable=True)
    author = relationship(
        "Author",
        primaryjoin="foreign(Book.author_id) == Author.id",
        lazy="select",
    )


@pytest.fixture
def maker(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'd.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_maker = sessionmaker(bind=engine)
    with session_maker() as session:
        for i in range(1, 21):
            session.add(Author(id=i, name=f"author {i}"))
            session.add(Book(id=i, title=f"book {i}", author_id=i))
        session.commit()
    yield session_maker
    engine.dispose()


def n1_app(maker, **options):
    async def books_n1(request):
        with maker() as session:
            return JSONResponse(
                [
                    {"title": b.title, "author": b.author.name}
                    for b in session.scalars(select(Book))
                ]
            )

    async def one(request):
        with maker() as session:
            session.scalars(select(Book)).all()
        return JSONResponse({})

    app = Starlette(routes=[Route("/n1", books_n1), Route("/one/{n}", one)])
    return app, install(app, **options)


def make(idx, *, path="/p", route="", duration=1.0, queries=None):
    profile = Profile(
        id=f"{idx:012d}",
        method="GET",
        path=path,
        route=route,
        duration_ms=duration,
        queries=queries or [],
    )
    profile.finalise()
    return profile


# ----------------------------------------------- collapsing duplicates
def test_group_queries_collapses_and_keeps_order():
    queries = [
        Query(sql="SELECT a", params="(1,)", duration_ms=1.0),
        Query(sql="SELECT b", params="()", duration_ms=2.0),
        Query(sql="SELECT a", params="(2,)", duration_ms=3.0),
        Query(sql="SELECT a", params="(3,)", duration_ms=6.0),
    ]
    groups = group_queries(queries)

    assert [g.sql for g in groups] == ["SELECT a", "SELECT b"]
    first = groups[0]
    assert first.count == 3
    assert first.total_ms == pytest.approx(10.0)
    assert first.avg_ms == pytest.approx(10.0 / 3)
    assert first.max_ms == pytest.approx(6.0)
    assert first.is_duplicate
    assert first.positions == [1, 3, 4]
    assert first.params == ["(1,)", "(2,)", "(3,)"]
    assert not groups[1].is_duplicate


def test_group_queries_tracks_failures():
    groups = group_queries(
        [
            Query(sql="SELECT bad", params="()", duration_ms=1.0, error="boom"),
            Query(sql="SELECT bad", params="()", duration_ms=1.0, error="boom"),
        ]
    )
    assert groups[0].failed
    assert groups[0].failures == 2
    assert groups[0].error == "boom"


def test_detail_page_collapses_an_n_plus_one(maker):
    """20 identical statements must render as one row, not twenty."""
    app, profiler = n1_app(maker)
    with TestClient(app) as client:
        client.get("/n1")
        profile = profiler.profiles[0]
        page = client.get(f"/profiler/request/{profile.id}").text

    assert profile.query_count == 21  # 1 for books, 20 for authors
    assert profile.duplicate_count == 19

    # two distinct statements: the book select and the repeated author select
    assert len(profile.query_groups) == 2
    assert "2 distinct statements" in page
    assert "21 executions" in page
    assert "&times;20" in page

    # and the stack appears once, not twenty times
    assert page.count("Stack (") == 2


def test_collapsing_shrinks_a_large_n_plus_one():
    """The page you need most must not be the one that is unusable."""
    storage = MemoryStorage()
    profile = make(
        1,
        queries=[
            Query(
                sql="SELECT a.id, a.name FROM author a WHERE a.id = ?",
                params=f"({i},)",
                duration_ms=0.4,
                stack=[f"/srv/app/views.py:{40 + j} in list_books" for j in range(8)],
            )
            for i in range(500)
        ],
    )
    storage.add(profile)

    groups = profile.query_groups
    assert len(groups) == 1
    assert groups[0].count == 500
    assert len(groups[0].params) == 5  # a sample, not five hundred
    assert len(groups[0].positions) == 50


# ------------------------------------------------- cross-request statements
def test_aggregate_statements_spans_routes():
    shared = "SELECT permissions FROM acl WHERE user_id = ?"
    profiles = [
        make(
            1,
            route="/a",
            queries=[
                Query(sql=shared, params="()", duration_ms=2.0),
                Query(sql="SELECT * FROM a", params="()", duration_ms=1.0),
            ],
        ),
        make(
            2,
            route="/b",
            queries=[
                Query(sql=shared, params="()", duration_ms=3.0),
                Query(sql=shared, params="()", duration_ms=5.0),
            ],
        ),
    ]
    rows = {r.sql: r for r in aggregate_statements(profiles)}

    acl = rows[shared]
    assert acl.count == 3
    assert acl.requests == 2
    assert acl.route_count == 2
    assert acl.total_ms == pytest.approx(10.0)
    assert acl.per_request == pytest.approx(1.5)
    assert acl.operation == "SELECT"
    # heaviest first
    assert aggregate_statements(profiles)[0].sql == shared


def test_aggregate_statements_respects_the_limit():
    profile = make(
        1,
        queries=[
            Query(sql=f"SELECT {i}", params="()", duration_ms=float(i))
            for i in range(20)
        ],
    )
    rows = aggregate_statements([profile], limit=5)
    assert len(rows) == 5
    assert rows[0].sql == "SELECT 19"  # the most expensive


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_statements_agree_across_backends(backend, tmp_path):
    store = (
        MemoryStorage()
        if backend == "memory"
        else SQLiteStorage(tmp_path / "st.db", background=False)
    )
    try:
        shared = "SELECT 1 FROM shared"
        store.add(make(1, route="/a", queries=[Query(shared, "()", 2.0)]))
        store.add(
            make(
                2,
                route="/b",
                queries=[Query(shared, "()", 3.0), Query("SELECT 2", "()", 9.0)],
            )
        )
        rows = {r.sql: r for r in store.statements()}
        assert rows[shared].count == 2
        assert rows[shared].requests == 2
        assert rows[shared].route_count == 2
        assert rows[shared].total_ms == pytest.approx(5.0)
        assert store.statements()[0].sql == "SELECT 2"  # 9ms beats 5ms
    finally:
        if isinstance(store, SQLiteStorage):
            store.close()


def test_statements_page_renders(maker):
    app, _ = n1_app(maker)
    with TestClient(app) as client:
        client.get("/n1")
        page = client.get("/profiler/statements")

    assert page.status_code == 200
    assert "author" in page.text
    assert "Statements by total time" in page.text
    # the repeated author lookup ran 20x in one request
    assert "20.0" in page.text or "20" in page.text


def test_statements_survive_a_trim(tmp_path):
    """Evicted profiles must not leave their statements behind."""
    with SQLiteStorage(tmp_path / "trim.db", max_requests=3, background=False) as store:
        for i in range(10):
            store.add(
                make(i, route=f"/r{i}", queries=[Query(f"SELECT {i}", "()", 1.0)])
            )
        assert store.count() == 3
        sqls = {r.sql for r in store.statements()}
        assert sqls == {"SELECT 7", "SELECT 8", "SELECT 9"}


# -------------------------------------------------------------- percentiles
def test_summary_reports_percentiles():
    profiles = [
        make(i, path="/x", route="/x", duration=float(i)) for i in range(1, 101)
    ]
    row = summarise(profiles)[0]

    assert row.count == 100
    assert row.p50_ms == pytest.approx(50.0)
    assert row.p95_ms == pytest.approx(95.0)
    assert row.p99_ms == pytest.approx(99.0)
    assert row.max_ms == pytest.approx(100.0)


def test_percentiles_are_not_ruined_by_one_outlier():
    profiles = [make(i, route="/x", duration=10.0) for i in range(99)]
    profiles.append(make(999, route="/x", duration=100_000.0))
    row = summarise(profiles)[0]

    assert row.p50_ms == pytest.approx(10.0)
    assert row.avg_ms > 1000  # the average is useless here
    assert row.p50_ms < row.avg_ms


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_percentiles_agree_across_backends(backend, tmp_path):
    store = (
        MemoryStorage()
        if backend == "memory"
        else SQLiteStorage(tmp_path / "p.db", background=False)
    )
    try:
        for i in range(1, 101):
            store.add(make(i, route="/x", duration=float(i)))
        row = store.summarise()[0]
        assert (row.p50_ms, row.p95_ms, row.p99_ms) == (50.0, 95.0, 99.0)
    finally:
        if isinstance(store, SQLiteStorage):
            store.close()


def test_summary_page_shows_percentiles(maker):
    app, _ = n1_app(maker)
    with TestClient(app) as client:
        client.get("/one/1")
        page = client.get("/profiler/summary")
    assert page.status_code == 200
    assert "p50" in page.text and "p95" in page.text and "p99" in page.text


# -------------------------------------------------------------------- JSON
def test_requests_json(maker):
    app, profiler = n1_app(maker)
    with TestClient(app) as client:
        client.get("/one/1")
        client.get("/n1")
        payload = client.get("/profiler/requests.json").json()

    assert payload["total"] == 2
    assert payload["page"] == 1
    paths = {r["path"] for r in payload["requests"]}
    assert paths == {"/one/1", "/n1"}
    row = next(r for r in payload["requests"] if r["path"] == "/n1")
    assert row["query_count"] == 21
    assert row["duplicate_count"] == 19
    assert row["route"] == "/n1"
    assert "queries" not in row  # a listing row


def test_requests_json_honours_filters(maker):
    app, _ = n1_app(maker)
    with TestClient(app) as client:
        client.get("/one/1")
        client.get("/n1")
        payload = client.get("/profiler/requests.json?only=duplicates").json()

    assert payload["total"] == 1
    assert payload["requests"][0]["path"] == "/n1"


def test_detail_json(maker):
    app, profiler = n1_app(maker)
    with TestClient(app) as client:
        client.get("/n1")
        profile_id = profiler.profiles[0].id
        payload = client.get(f"/profiler/request/{profile_id}.json").json()

    assert payload["id"] == profile_id
    assert payload["query_count"] == 21
    assert len(payload["queries"]) == 21
    assert payload["queries"][0]["sql"].upper().startswith("SELECT")
    assert isinstance(payload["queries"][0]["stack"], list)
    assert "request_headers" in payload


def test_detail_json_404s_for_an_unknown_id(maker):
    app, _ = n1_app(maker)
    with TestClient(app) as client:
        response = client.get("/profiler/request/nope.json")
    assert response.status_code == 404
    assert response.json()["error"]


def test_summary_and_statements_json(maker):
    app, _ = n1_app(maker)
    with TestClient(app) as client:
        client.get("/n1")
        summary = client.get("/profiler/summary.json").json()
        statements = client.get("/profiler/statements.json").json()

    assert summary["routes"][0]["route"] == "/n1"
    assert "p95_ms" in summary["routes"][0]
    assert statements["statements"]
    assert statements["statements"][0]["count"] >= 1
    assert "sql" in statements["statements"][0]


def test_json_is_the_ci_assertion_story(maker):
    """The point of the JSON: fail a build when an endpoint regresses."""
    app, _ = n1_app(maker)
    with TestClient(app) as client:
        client.get("/n1")
        payload = client.get("/profiler/requests.json").json()

    worst = max(payload["requests"], key=lambda r: r["query_count"])
    assert worst["duplicate_count"] > 0, "this endpoint has an N+1"
    assert json.dumps(payload)  # fully serialisable


def test_json_routes_are_not_shadowed_by_the_html_detail_route(maker):
    """`/request/{id}` matches any non-slash run, `.json` included."""
    app, profiler = n1_app(maker)
    with TestClient(app) as client:
        client.get("/one/1")
        profile_id = profiler.profiles[0].id
        html = client.get(f"/profiler/request/{profile_id}")
        js = client.get(f"/profiler/request/{profile_id}.json")

    assert html.headers["content-type"].startswith("text/html")
    assert js.headers["content-type"].startswith("application/json")


# ------------------------------------------------------- standalone viewer
def test_standalone_viewer_reads_a_capture_file(tmp_path):
    from starlette_profiler.config import ProfilerConfig
    from starlette_profiler.viewer import build_viewer

    path = tmp_path / "captured.db"
    with SQLiteStorage(path, background=False) as store:
        store.add(make(1, path="/from-staging", route="/from-staging"))

    reopened = SQLiteStorage(path, background=False)
    try:
        viewer = build_viewer(reopened, ProfilerConfig(mount_path=""))
        with TestClient(viewer) as client:
            assert "/from-staging" in client.get("/").text
            assert client.get("/summary").status_code == 200
            assert client.get("/statements").status_code == 200
    finally:
        reopened.close()


def test_main_rejects_a_missing_file(tmp_path, capsys):
    from starlette_profiler.__main__ import main

    with pytest.raises(SystemExit):
        main([str(tmp_path / "nope.db")])
    assert "no such file" in capsys.readouterr().err


# --------------------------------------------- status classes in templates
@pytest.mark.parametrize(
    ("status", "css_class"),
    [(200, "ok"), (302, "info"), (404, "warn"), (500, "err")],
)
def test_every_status_class_renders(status, css_class):
    """Nothing had ever rendered a 3xx, 4xx or 5xx row through the viewer."""

    async def coded(request):
        return PlainTextResponse("x", status_code=int(request.path_params["code"]))

    app = Starlette(routes=[Route("/code/{code}", coded)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get(f"/code/{status}", follow_redirects=False)
        listing = client.get("/profiler/")
        detail = client.get(f"/profiler/request/{profiler.profiles[0].id}")

    assert profiler.profiles[0].status_class == css_class
    assert f'class="pill {css_class}"' in listing.text
    assert f'class="pill {css_class}"' in detail.text


def test_a_failing_endpoint_renders_as_an_error_row():
    async def boom(request):
        raise RuntimeError("kaboom")

    app = Starlette(routes=[Route("/boom", boom)])
    profiler = install(app)

    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/boom")
        listing = client.get("/profiler/?status=err")

    assert profiler.profiles[0].status_code == 500
    assert "/boom" in listing.text
    assert 'class="pill err"' in listing.text


def test_sql_error_pill_renders(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'e.db'}")

    async def bad(request):
        with engine.connect() as conn, contextlib.suppress(Exception):
            conn.execute(text("SELECT * FROM nope"))
        return JSONResponse({})

    app = Starlette(routes=[Route("/bad", bad)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get("/bad")
        listing = client.get("/profiler/")
        detail = client.get(f"/profiler/request/{profiler.profiles[0].id}")

    assert "SQL error" in listing.text
    assert "failed" in detail.text
