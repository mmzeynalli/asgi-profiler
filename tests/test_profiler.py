from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from starlette_profiler import ProfilerConfig, install


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
    author_id = Column(Integer, ForeignKey("author.id"))
    author = relationship("Author", lazy="select")


@pytest.fixture
def session_maker(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    with maker() as session:
        for i in range(1, 6):
            session.add(Author(id=i, name=f"author {i}"))
            session.add(Book(id=i, title=f"book {i}", author_id=i))
        session.commit()
    yield maker
    engine.dispose()


def build_app(session_maker, **options):
    async def hello(request):
        return PlainTextResponse("hi")

    async def books(request):
        with session_maker() as session:
            rows = session.scalars(select(Book)).all()
            return JSONResponse([{"id": b.id} for b in rows])

    async def books_n1(request):
        with session_maker() as session:
            out = []
            for book in session.scalars(select(Book)):
                out.append({"title": book.title, "author": book.author.name})
            return JSONResponse(out)

    async def boom(request):
        raise RuntimeError("kaboom")

    app = Starlette(
        routes=[
            Route("/", hello),
            Route("/books", books),
            Route("/books-n1", books_n1),
            Route("/boom", boom),
        ]
    )
    profiler = install(app, **options)
    return app, profiler


# --------------------------------------------------------------- recording
def test_records_requests(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/books").status_code == 200

    paths = [p.path for p in profiler.profiles]
    assert paths == ["/books", "/"]  # newest first
    assert all(p.duration_ms > 0 for p in profiler.profiles)


def test_captures_sql(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/books")

    profile = profiler.profiles[0]
    assert profile.query_count == 1
    assert profile.queries[0].sql.upper().startswith("SELECT")
    assert profile.query_ms > 0
    assert profile.python_ms >= 0


def test_detects_n_plus_one(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/books-n1")

    profile = profiler.profiles[0]
    assert profile.query_count == 6  # 1 for books + 1 per author
    assert profile.duplicate_count == 4
    assert sum(1 for q in profile.queries if q.is_duplicate) == 5


def test_captures_stack_for_queries(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/books")

    stack = profiler.profiles[0].queries[0].stack
    assert stack, "expected application frames"
    assert any("test_profiler.py" in frame for frame in stack)
    assert not any("sqlalchemy" in frame for frame in stack)


def test_records_failures(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500

    assert profiler.profiles[0].path == "/boom"
    assert profiler.profiles[0].status_code == 500


def test_profiler_pages_are_not_recorded(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/")
        client.get("/profiler/")
        client.get("/profiler/summary")

    assert [p.path for p in profiler.profiles] == ["/"]


def test_excludes_configured_paths(session_maker):
    app, profiler = build_app(session_maker, exclude_paths=["/books"])
    with TestClient(app) as client:
        client.get("/")
        client.get("/books")

    assert [p.path for p in profiler.profiles] == ["/"]


def test_headers_are_redacted(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/", headers={"authorization": "Bearer hunter2", "x-trace": "abc"})

    headers = profiler.profiles[0].request_headers
    assert headers["authorization"] == "<redacted>"
    assert headers["x-trace"] == "abc"


def test_ring_buffer_is_bounded(session_maker):
    app, profiler = build_app(session_maker, max_requests=3)
    with TestClient(app) as client:
        for _ in range(6):
            client.get("/")

    assert len(profiler.profiles) == 3


# ------------------------------------------------------------------ viewer
def test_viewer_pages_render(session_maker):
    app, _ = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/books-n1")

        listing = client.get("/profiler/")
        assert listing.status_code == 200
        assert "/books-n1" in listing.text
        assert "N+1" in listing.text

        summary = client.get("/profiler/summary")
        assert summary.status_code == 200
        assert "/books-n1" in summary.text

        css = client.get("/profiler/static/profiler.css")
        assert css.status_code == 200
        assert css.headers["content-type"].startswith("text/css")


def test_detail_page_shows_sql_and_stack(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/books-n1")
        profile_id = profiler.profiles[0].id

        page = client.get(f"/profiler/request/{profile_id}")
        assert page.status_code == 200
        assert "SELECT" in page.text
        # repeated statements collapse into one row carrying a xN badge
        assert "&times;5" in page.text
        assert "distinct statement" in page.text
        assert "test_profiler.py" in page.text  # the stack


def test_unknown_detail_redirects(session_maker):
    app, _ = build_app(session_maker)
    with TestClient(app) as client:
        response = client.get("/profiler/request/nope", follow_redirects=False)
        assert response.status_code == 302


def test_filters(session_maker):
    app, _ = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/")
        client.get("/books")
        client.get("/books-n1")

        assert "/books-n1" in client.get("/profiler/?q=n1").text
        assert "/books-n1" not in client.get("/profiler/?q=zzz").text
        assert "/books-n1" in client.get("/profiler/?only=duplicates").text
        assert ">/books<" not in client.get("/profiler/?only=duplicates").text
        assert client.get("/profiler/?order=slowest").status_code == 200
        assert client.get("/profiler/?method=POST").status_code == 200


def test_clear(session_maker):
    app, profiler = build_app(session_maker)
    with TestClient(app) as client:
        client.get("/")
        assert profiler.profiles
        response = client.post("/profiler/clear", follow_redirects=False)
        assert response.status_code == 303
    assert profiler.profiles == []


# ------------------------------------------------------------ mount + auth
def test_works_under_a_custom_mount_path(session_maker):
    app, _ = build_app(session_maker, mount_path="/deeply/nested/perf")
    with TestClient(app) as client:
        client.get("/books")
        page = client.get("/deeply/nested/perf/")
        assert page.status_code == 200
        # every emitted link must respect the mount
        assert "/deeply/nested/perf/static/profiler.css" in page.text
        assert 'href="/profiler' not in page.text


def test_authorize_hook_can_deny(session_maker):
    config = ProfilerConfig(
        authorize=lambda request: request.headers.get("x-key") == "letmein"
    )
    app, _ = build_app(session_maker, config=config)
    with TestClient(app) as client:
        assert client.get("/profiler/").status_code == 403
        assert client.get("/profiler/", headers={"x-key": "letmein"}).status_code == 200


def test_unknown_option_is_rejected(session_maker):
    with pytest.raises(TypeError, match="Unknown profiler option"):
        build_app(session_maker, definitely_not_an_option=1)
