"""The 0.3.0 detectors, and the two foundations they stand on."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asgi_profiler import (
    DetectorSettings,
    Profile,
    ProfilerConfig,
    Query,
    SQLiteStorage,
    detect,
    install,
    normalise_sql,
    sql_hash,
)
from asgi_profiler.detectors import LABELS, TYPES, Settings
from asgi_profiler.detectors.utils import common_frames, total_span_ms

# ------------------------------------------------------------- fingerprints


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # the case the whole module exists for: a page of ten and a page of
        # eleven are one statement, not two
        ("SELECT a FROM t WHERE id IN (1, 2, 3)", "SELECT a FROM t WHERE id IN (4, 5)"),
        ("SELECT a FROM t WHERE id IN (?, ?)", "SELECT a FROM t WHERE id IN (1, 2)"),
        ("SAVEPOINT sa_savepoint_1", "SAVEPOINT sa_savepoint_27"),
        ("SELECT a FROM t LIMIT 20", "SELECT a FROM t LIMIT 100"),
        ("SELECT a FROM t WHERE n = 'bob'", "SELECT a FROM t WHERE n = 'alice'"),
        ("SELECT a FROM t WHERE ok = true", "SELECT a FROM t WHERE ok = false"),
        # whitespace is not identity
        ("SELECT a\n  FROM t", "SELECT a FROM t"),
    ],
)
def test_statements_that_differ_only_in_their_values_are_one_statement(left, right):
    assert sql_hash(left) == sql_hash(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("SELECT a FROM t1", "SELECT a FROM t2"),
        ("SELECT a FROM t", "SELECT b FROM t"),
        ("INSERT INTO t VALUES (1)", "DELETE FROM t WHERE id = 1"),
        # digits inside an identifier are part of the name, not a value
        ("SELECT * FROM users_2024", "SELECT * FROM users_2025"),
    ],
)
def test_statements_that_differ_in_substance_stay_apart(left, right):
    assert sql_hash(left) != sql_hash(right)


def test_double_quoted_identifiers_are_left_alone():
    """Postgres quotes identifiers with `"`. Folding those would merge the schema."""
    assert normalise_sql('SELECT "users"."id" FROM "users"') == 'SELECT "users"."id" FROM "users"'


def test_escaped_quotes_do_not_end_a_string_early():
    assert normalise_sql("SELECT a FROM t WHERE n = 'it''s' AND b = 2") == (
        "SELECT a FROM t WHERE n = %s AND b = %s"
    )


# ------------------------------------------------------------------- spans


def _q(sql="SELECT 1", *, start=0.0, ms=1.0, stack=None, blocking=False):
    return Query(
        sql=sql,
        params="()",
        duration_ms=ms,
        started_ms=start,
        blocking=blocking,
        stack=list(stack or []),
    )


def test_total_span_counts_overlapping_time_once():
    """Twenty concurrent queries are not twenty queries' worth of waiting."""
    concurrent = [_q(start=0.0, ms=10.0) for _ in range(20)]
    assert total_span_ms(concurrent) == pytest.approx(10.0)


def test_total_span_adds_up_sequential_queries():
    sequential = [_q(start=float(i) * 10, ms=10.0) for i in range(5)]
    assert total_span_ms(sequential) == pytest.approx(50.0)


def test_total_span_merges_partial_overlap_and_bridges_gaps():
    assert total_span_ms([_q(start=0, ms=10), _q(start=5, ms=10)]) == pytest.approx(15.0)
    assert total_span_ms([_q(start=0, ms=10), _q(start=90, ms=10)]) == pytest.approx(20.0)
    assert total_span_ms([]) == 0.0


def test_common_frames_is_the_shared_outer_stack():
    assert common_frames([["a", "b", "c"], ["a", "b", "d"]]) == ["a", "b"]
    assert common_frames([["a"], ["b"]]) == []
    assert common_frames([]) == []


# ------------------------------------------------------------------- N+1


def _profile(queries, *, route="/r", duration=100.0):
    profile = Profile(id="p", method="GET", path="/r", route=route, duration_ms=duration)
    profile.queries = queries
    profile.finalise()
    return profile


def _loop(count, *, sql="SELECT * FROM authors WHERE id = ?", frame="app.py:20 in view"):
    return [
        _q(sql, start=float(i), ms=1.0, stack=["main.py:1 in handler", frame]) for i in range(count)
    ]


def test_an_n_plus_one_is_reported_with_the_line_that_caused_it():
    queries = [_q("SELECT * FROM books", stack=["main.py:1 in handler"]), *_loop(6)]
    problem = _find(detect(_profile(queries)), "n_plus_one_db")
    assert problem is not None
    assert problem.evidence["count"] == 6
    assert problem.evidence["frame"] == "app.py:20 in view"
    # the other half of the story: these six exist because that one returned six rows
    assert problem.evidence["source_sql"] == "SELECT * FROM books"
    assert problem.offenders == [1, 2, 3, 4, 5, 6]


def test_repetition_below_the_count_threshold_is_not_a_finding():
    """Two identical statements are a coincidence. Five hundred are a bug."""
    assert detect(_profile(_loop(4))) == []
    assert _find(detect(_profile(_loop(5))), "n_plus_one_db") is not None


def test_the_count_threshold_is_configurable():
    settings = Settings(n_plus_one_count=3)
    assert _find(detect(_profile(_loop(3)), settings), "n_plus_one_db") is not None


def test_a_duration_floor_can_be_raised_for_production_sized_data():
    slow = [_q("SELECT 1", start=float(i) * 20, ms=20.0, stack=["a.py:1 in f"]) for i in range(6)]
    assert _find(detect(_profile(slow), Settings(n_plus_one_ms=1000.0)), "n_plus_one_db") is None
    assert _find(detect(_profile(slow), Settings(n_plus_one_ms=100.0)), "n_plus_one_db") is not None


def test_the_same_statement_from_unrelated_places_is_not_a_loop():
    """A serialiser and a permission check asking the same question is not an N+1."""
    scattered = [
        _q("SELECT * FROM users WHERE id = ?", start=float(i), stack=[f"mod{i}.py:1 in f{i}"])
        for i in range(6)
    ]
    assert _find(detect(_profile(scattered)), "n_plus_one_db") is None


def test_transaction_bookkeeping_is_never_an_n_plus_one():
    """`SAVEPOINT sa_1` and `SAVEPOINT sa_2` are the same statement, and not a finding."""
    savepoints = [
        _q(f"SAVEPOINT sa_savepoint_{i}", start=float(i), stack=["app.py:5 in nested"])
        for i in range(8)
    ]
    assert detect(_profile(savepoints)) == []


def test_a_truncated_statement_is_not_fingerprinted():
    """Its stored text carries the original length, so it would never group twice."""
    cut = [
        _q(
            f"INSERT INTO t VALUES ... [truncated, {9000 + i} chars]",
            start=float(i),
            stack=["a.py:1 in f"],
        )
        for i in range(6)
    ]
    assert detect(_profile(cut)) == []


def test_without_stacks_an_n_plus_one_is_still_reported():
    bare = [_q("SELECT 1", start=float(i)) for i in range(6)]
    assert _find(detect(_profile(bare), Settings(stacks=False)), "n_plus_one_db") is not None
    # ...but with stacks on, no shared frame means library bookkeeping, not a loop
    assert _find(detect(_profile(bare), Settings(stacks=True)), "n_plus_one_db") is None


# ----------------------------------------------------------- fingerprints


def test_the_same_n_plus_one_has_the_same_fingerprint_on_every_request():
    first = _find(detect(_profile(_loop(6))), "n_plus_one_db")
    second = _find(detect(_profile(_loop(9))), "n_plus_one_db")
    assert first.fingerprint == second.fingerprint


def test_the_same_n_plus_one_on_another_route_is_another_problem():
    a = _find(detect(_profile(_loop(6), route="/a")), "n_plus_one_db")
    b = _find(detect(_profile(_loop(6), route="/b")), "n_plus_one_db")
    assert a.fingerprint != b.fingerprint


def test_a_different_line_is_a_different_problem():
    a = _find(detect(_profile(_loop(6, frame="app.py:20 in view"))), "n_plus_one_db")
    b = _find(detect(_profile(_loop(6, frame="app.py:99 in other"))), "n_plus_one_db")
    assert a.fingerprint != b.fingerprint


def test_a_fingerprint_names_its_detector():
    problem = _find(detect(_profile(_loop(6))), "n_plus_one_db")
    assert problem.fingerprint.startswith("1-n_plus_one_db-")


# ------------------------------------------------------------ slow queries


def test_a_slow_statement_is_reported_once_however_often_it_ran():
    queries = [
        _q("SELECT * FROM big", start=float(i) * 200, ms=150.0, stack=["a.py:1 in f"])
        for i in range(3)
    ]
    problem = _find(detect(_profile(queries, duration=600.0)), "slow_db_query")
    assert problem.evidence["occurrences"] == 3
    assert problem.time_ms == pytest.approx(150.0)


def test_a_fast_statement_is_not_slow():
    assert _find(detect(_profile([_q(ms=10.0)])), "slow_db_query") is None


def test_the_slow_threshold_is_configurable():
    profile = _profile([_q("SELECT * FROM big", ms=30.0)])
    assert _find(detect(profile, Settings(slow_query_ms=20.0)), "slow_db_query") is not None


# --------------------------------------------------------------- blocking


def test_blocking_statements_are_one_finding_per_request():
    """Split per call site, a 30 ms stall reported as "1 ms" twice. It is one stall."""
    queries = [
        _q(start=0.0, ms=10.0, blocking=True, stack=["a.py:1 in f"]),
        _q(start=10.0, ms=10.0, blocking=True, stack=["a.py:2 in g"]),
    ]
    problems = [p for p in detect(_profile(queries)) if p.type == "blocking_db_call"]
    assert len(problems) == 1
    assert problems[0].title == "Blocking database call: 2 statements on the event loop"
    assert problems[0].time_ms == pytest.approx(20.0)
    assert problems[0].evidence["call_sites"] == 2
    # attributed to the costliest site, not the first one seen
    assert problems[0].evidence["frame"] in {"a.py:1 in f", "a.py:2 in g"}


def test_a_floor_can_be_raised_to_silence_a_call_you_have_decided_to_keep():
    profile = _profile([_q(ms=1.0, blocking=True)])
    assert _find(detect(profile), "blocking_db_call") is not None
    assert _find(detect(profile, Settings(blocking_ms=16.0)), "blocking_db_call") is None


def test_nothing_blocking_means_nothing_reported():
    assert _find(detect(_profile([_q(ms=500.0)])), "blocking_db_call") is None


# ------------------------------------------------------------- the runner


def test_detectors_are_ordered_by_cost():
    queries = [
        _q("SELECT * FROM big", start=0.0, ms=400.0, stack=["a.py:1 in f"]),
        *[_q(start=500.0 + i, ms=1.0, stack=["b.py:2 in g"]) for i in range(6)],
    ]
    found = detect(_profile(queries, duration=1000.0))
    assert [p.time_ms for p in found] == sorted((p.time_ms for p in found), reverse=True)


def test_a_detector_that_raises_does_not_take_the_others_with_it():
    from asgi_profiler import detectors

    def broken(profile, settings):
        raise RuntimeError("boom")

    original = detectors.DETECTORS
    detectors.DETECTORS = ((("broken"), broken), *original)
    try:
        found = detect(_profile([_q("SELECT * FROM big", ms=400.0)], duration=500.0))
    finally:
        detectors.DETECTORS = original
    assert _find(found, "slow_db_query") is not None


def test_detectors_can_be_turned_off():
    profile = _profile(_loop(6))
    assert detect(profile, Settings(enabled=())) == []
    assert len(detect(profile, Settings(enabled=["n_plus_one_db"]))) == 1


def test_every_detector_has_a_label():
    assert set(LABELS) == set(TYPES)


# ------------------------------------------------------------ the config


def test_the_package_re_exports_the_detector_settings():
    assert DetectorSettings is Settings


def test_config_defaults_match_the_detector_defaults():
    """They are written once and referenced twice; this is the guard that keeps it so."""
    settings = ProfilerConfig().detector_settings()
    assert settings == Settings(enabled=None, stacks=True)


def test_turning_stacks_off_reaches_the_detectors():
    assert ProfilerConfig(capture_stacks=False).detector_settings().stacks is False


def test_an_unknown_detector_name_is_reported_but_not_fatal():
    config = ProfilerConfig(detectors=["n_plus_one_db", "nonsense"])
    assert config.unknown_detectors() == ("nonsense",)
    assert ProfilerConfig().unknown_detectors() == ()


# ----------------------------------------------------------------- storage


def test_problems_survive_a_round_trip_through_sqlite(tmp_path):
    profile = _profile(_loop(6))
    profile.problems = detect(profile)
    profile.id = "abc123abc123"
    with SQLiteStorage(tmp_path / "p.db", background=False) as store:
        store.add(profile)
        restored = store.get("abc123abc123")
        assert [p.fingerprint for p in restored.problems] == [
            p.fingerprint for p in profile.problems
        ]
        assert restored.problems[0].evidence["frame"] == "app.py:20 in view"
        # and the listing, which deliberately does not load the statements,
        # still knows there is something to badge
        listed = store.search_for_badges = store.search(_filters())
        assert listed.items[0].queries == []
        assert listed.items[0].problems


def _filters():
    from asgi_profiler import Filters

    return Filters()


def test_the_listing_can_be_filtered_to_requests_with_findings(tmp_path):
    from asgi_profiler import Filters

    clean = _profile([_q(ms=1.0)])
    clean.id = "clean0000000"
    noisy = _profile(_loop(6))
    noisy.id = "noisy0000000"
    noisy.problems = detect(noisy)
    with SQLiteStorage(tmp_path / "f.db", background=False) as store:
        store.add(clean)
        store.add(noisy)
        found = store.search(Filters(only_problems=True))
        assert [p.id for p in found.items] == ["noisy0000000"]


# ------------------------------------------------------------ end to end

Base = declarative_base()


class Author(Base):
    __tablename__ = "authors"
    id = Column(Integer, primary_key=True)
    name = Column(String)


class Book(Base):
    __tablename__ = "books"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    author_id = Column(Integer, ForeignKey("authors.id"))
    author = relationship("Author", lazy="select")


@pytest.fixture
def books_app(tmp_path):
    """One app, three endpoints, one N+1 written three different ways."""
    url = f"sqlite:///{tmp_path / 'books.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    with maker() as session:
        for i in range(1, 13):
            session.add(Author(id=i, name=f"a{i}"))
            session.add(Book(id=i, title=f"b{i}", author_id=i))
        session.commit()
    async_engine = create_async_engine(url.replace("sqlite://", "sqlite+aiosqlite://"))

    async def blocking(request):  # async def + sync Session: stops the loop
        with maker() as session:
            return JSONResponse([{"a": b.author.name} for b in session.scalars(select(Book))])

    def threadpool(request):  # def endpoint: Starlette runs it off the loop
        with maker() as session:
            return JSONResponse([{"a": b.author.name} for b in session.scalars(select(Book))])

    async def asynchronous(request):  # async engine: yields while it waits
        async with AsyncSession(async_engine) as session:
            books = (await session.scalars(select(Book))).all()
            out = [{"a": (await session.get(Author, b.author_id)).name} for b in books]
            return JSONResponse(out)

    app = Starlette(
        routes=[
            Route("/blocking", blocking),
            Route("/threadpool", threadpool),
            Route("/async", asynchronous),
        ]
    )
    profiler = install(app)
    yield app, profiler
    profiler.close()
    engine.dispose()


def _types(profiler, path):
    profile = next(p for p in profiler.profiles if p.path == path)
    return profile, {p.type for p in profile.problems}


@pytest.mark.parametrize(
    ("path", "blocks"),
    [
        ("/blocking", True),
        # the same synchronous session, off the loop: an N+1, but nothing frozen
        ("/threadpool", False),
        # an async engine: the driver call yields, so the loop keeps serving
        ("/async", False),
    ],
)
def test_only_the_endpoint_that_blocks_the_loop_is_reported_as_blocking(books_app, path, blocks):
    app, profiler = books_app
    with TestClient(app) as client:
        assert client.get(path).status_code == 200
    profile, types = _types(profiler, path)
    assert "n_plus_one_db" in types, "every one of these is an N+1"
    assert ("blocking_db_call" in types) is blocks
    assert (profile.blocking_count > 0) is blocks


def test_the_n_plus_one_names_the_application_line(books_app):
    app, profiler = books_app
    with TestClient(app) as client:
        client.get("/threadpool")
    profile, _ = _types(profiler, "/threadpool")
    problem = _find(profile.problems, "n_plus_one_db")
    assert "test_detectors.py" in problem.evidence["frame"]
    assert problem.evidence["source_sql"].startswith("SELECT books.")


def test_query_start_times_are_offsets_into_the_request(books_app):
    app, profiler = books_app
    with TestClient(app) as client:
        client.get("/threadpool")
    profile, _ = _types(profiler, "/threadpool")
    starts = [q.started_ms for q in profile.queries]
    assert starts == sorted(starts), "recorded in execution order"
    assert starts[0] >= 0.0
    assert profile.queries[-1].ended_ms <= profile.duration_ms + 1.0


def test_findings_reach_the_viewer_and_the_json(books_app):
    app, profiler = books_app
    with TestClient(app) as client:
        client.get("/blocking")
        listing = client.get("/profiler/")
        assert "Blocking" in listing.text and "N+1" in listing.text

        profile_id = profiler.profiles[0].id
        page = client.get(f"/profiler/request/{profile_id}")
        assert "Findings" in page.text
        assert "N+1 query" in page.text

        payload = client.get(f"/profiler/request/{profile_id}.json").json()
        types = {p["type"] for p in payload["problems"]}
        assert types == {"n_plus_one_db", "blocking_db_call"}
        assert payload["blocking_count"] > 0
        assert all("sql_hash" in q and "started_ms" in q for q in payload["queries"])


def _find(problems, type_name):
    return next((p for p in problems if p.type == type_name), None)
