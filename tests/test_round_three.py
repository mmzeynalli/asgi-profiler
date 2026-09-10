"""Regressions for the defects an adversarial review of 0.3.0 turned up.

The template break at the top is the reason this file exists: 147 tests
passed while the request detail page was structurally broken, because every
assertion was a substring search and the surviving fragments still matched.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from html.parser import HTMLParser

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
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
)
from starlette_profiler.storage import IncompatibleCapture


async def endpoint(request):
    return PlainTextResponse("ok")


def make(idx, *, path=None, route="", method="GET", duration=1.0, queries=None):
    profile = Profile(
        id=f"{idx:012d}",
        method=method,
        path=path or f"/p{idx}",
        route=route,
        duration_ms=duration,
        queries=queries or [],
    )
    profile.finalise()
    return profile


# =====================================================================
# The detail page must be well-formed HTML
# =====================================================================
class _Structure(HTMLParser):
    """Enough of a parser to catch an unclosed tag or a swallowed attribute."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.unbalanced: list[str] = []
        self.tiles: list[str] = []
        self.sections = 0
        self._void = {"br", "hr", "img", "input", "meta", "link", "source"}
        self._in_tile_label = False

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class") or ""
        if "\n" in classes or "<" in classes:
            self.unbalanced.append(f"attribute swallowed markup: {classes!r}")
        if tag == "section":
            self.sections += 1
        if tag == "div" and "label" in classes.split():
            self._in_tile_label = True
        if tag not in self._void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self._void:
            return
        if not self.stack:
            self.unbalanced.append(f"</{tag}> with nothing open")
            return
        if self.stack[-1] != tag:
            self.unbalanced.append(f"</{tag}> closed <{self.stack[-1]}>")
        while self.stack and self.stack.pop() != tag:
            pass

    def handle_data(self, data):
        if self._in_tile_label and data.strip():
            self.tiles.append(data.strip())
            self._in_tile_label = False


def _structure(html: str) -> _Structure:
    parser = _Structure()
    parser.feed(html)
    return parser


@pytest.mark.parametrize("with_duplicates", [False, True])
def test_the_detail_page_is_well_formed(with_duplicates):
    """A slice-edit once destroyed two tiles and a </section> unnoticed."""
    storage = MemoryStorage()
    queries = [Query("SELECT a", "(1,)", 1.0, stack=["/app.py:1 in v"])]
    if with_duplicates:
        queries += [Query("SELECT a", "(2,)", 1.0, stack=["/app.py:1 in v"])]
    storage.add(make(1, queries=queries))

    app = Starlette(routes=[Route("/", endpoint)])
    install(app, storage=storage)
    with TestClient(app) as client:
        page = client.get("/profiler/request/000000000001")

    assert page.status_code == 200
    parsed = _structure(page.text)
    assert parsed.unbalanced == [], parsed.unbalanced
    assert parsed.stack == [], f"unclosed tags: {parsed.stack}"
    assert "Duplicates" in parsed.tiles
    assert {"Status", "Total", "In SQL", "In Python", "Queries"} <= set(parsed.tiles)
    assert "<h2>" in page.text or "Queries" in page.text


def test_every_viewer_page_is_well_formed():
    storage = MemoryStorage()
    storage.add(
        make(1, route="/r", queries=[Query("SELECT 1", "()", 1.0, error="boom")])
    )
    app = Starlette(routes=[Route("/", endpoint)])
    install(app, storage=storage)

    with TestClient(app) as client:
        for path in ("/profiler/", "/profiler/summary", "/profiler/statements"):
            parsed = _structure(client.get(path).text)
            assert parsed.unbalanced == [], (path, parsed.unbalanced)
            assert parsed.stack == [], (path, parsed.stack)


def test_the_error_tile_appears_only_when_there_are_errors():
    storage = MemoryStorage()
    storage.add(make(1, queries=[Query("SELECT 1", "()", 1.0, error="boom")]))
    storage.add(make(2, queries=[Query("SELECT 1", "()", 1.0)]))
    app = Starlette(routes=[Route("/", endpoint)])
    install(app, storage=storage)

    with TestClient(app) as client:
        failed = _structure(client.get("/profiler/request/000000000001").text)
        clean = _structure(client.get("/profiler/request/000000000002").text)

    assert "SQL errors" in failed.tiles
    assert "SQL errors" not in clean.tiles


# =====================================================================
# Collapsed groups must not lose the failure
# =====================================================================
def test_a_late_failure_keeps_its_error_and_stack():
    """The failure is rarely the first execution of that SQL."""
    group = group_queries(
        [
            Query("SELECT x", "(1,)", 1.0, stack=["/app.py:10 in ok"]),
            Query("SELECT x", "(2,)", 1.0, stack=["/app.py:10 in ok"]),
            Query(
                "SELECT x",
                "(3,)",
                1.0,
                stack=["/app.py:99 in the_bad_one"],
                error="OperationalError: no such column",
            ),
        ]
    )[0]

    assert group.failed
    assert group.error == "OperationalError: no such column"
    assert group.stack == ["/app.py:99 in the_bad_one"]


def test_a_late_failure_is_rendered():
    storage = MemoryStorage()
    storage.add(
        make(
            1,
            queries=[
                Query("SELECT x", "(1,)", 1.0, stack=["/app.py:10 in ok"]),
                Query(
                    "SELECT x",
                    "(2,)",
                    1.0,
                    stack=["/app.py:99 in bad"],
                    error="OperationalError: no such column: id",
                ),
            ],
        )
    )
    app = Starlette(routes=[Route("/", endpoint)])
    install(app, storage=storage)
    with TestClient(app) as client:
        page = client.get("/profiler/request/000000000001").text

    assert "no such column: id" in page
    assert "/app.py:99 in bad" in page


def test_multiple_call_sites_are_disclosed():
    """One shared stack would be a lie when the SQL came from two places."""
    group = group_queries(
        [
            Query("SELECT x", "()", 1.0, stack=["/a.py:1 in one"]),
            Query("SELECT x", "()", 1.0, stack=["/b.py:2 in two"]),
        ]
    )[0]
    assert group.call_sites == 2

    storage = MemoryStorage()
    storage.add(
        make(
            1,
            queries=[
                Query("SELECT x", "()", 1.0, stack=["/a.py:1 in one"]),
                Query("SELECT x", "()", 1.0, stack=["/b.py:2 in two"]),
            ],
        )
    )
    app = Starlette(routes=[Route("/", endpoint)])
    install(app, storage=storage)
    with TestClient(app) as client:
        assert (
            "2 different call sites"
            in client.get("/profiler/request/000000000001").text
        )


def test_empty_params_are_not_rendered():
    group = group_queries([Query("SELECT 1", "()", 1.0)])[0]
    assert group.params == []
    assert group.distinct_params == 0


def test_the_and_n_more_hint_counts_distinct_renderings():
    queries = [Query("SELECT x", f"({i},)", 1.0) for i in range(8)]
    queries += [Query("SELECT x", "(0,)", 1.0)] * 20  # repeats, not new values
    group = group_queries(queries)[0]

    assert group.count == 28
    assert len(group.params) == 5
    assert group.distinct_params == 8  # so the hint says "and 3 more"


# =====================================================================
# The SQLite writer must not lose data, hang, or fail in silence
# =====================================================================
def test_a_write_failure_is_logged_not_swallowed(tmp_path, caplog):
    store = SQLiteStorage(tmp_path / "fail.db", background=False)
    try:
        store._conn.close()  # simulate a wedged connection
        # synchronous mode surfaces the failure to the caller, and the
        # middleware logs it rather than letting it escape
        with pytest.raises(sqlite3.ProgrammingError):
            store.add(make(1))
    finally:
        store._closed = True

    background = SQLiteStorage(tmp_path / "fail2.db")
    try:
        background._conn.close()
        background.add(make(1))
        background.flush(timeout=5)
    finally:
        background._closed = True
    assert any("Could not write" in r.message for r in caplog.records)


def test_a_dead_writer_does_not_hang_flush(tmp_path):
    """flush() is called from request handlers; it must be bounded."""
    store = SQLiteStorage(tmp_path / "dead.db")
    try:
        store._conn.close()  # every write from here on fails
        for i in range(5):
            store.add(make(i))
        start = time.perf_counter()
        store.flush(timeout=2)
        assert time.perf_counter() - start < 5, "flush() hung"
        # and the writer survived to accept more
        assert store._alive()
    finally:
        store._closed = True


def test_queued_profiles_survive_interpreter_exit(tmp_path):
    """A daemon writer is killed at exit; nothing calls close() for you."""
    path = tmp_path / "exit.db"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(sys.path[0])!r})
        from starlette_profiler import SQLiteStorage, Profile
        store = SQLiteStorage({str(path)!r})
        for i in range(100):
            p = Profile(id=f"{{i:012d}}", method="GET", path=f"/p{{i}}")
            p.finalise()
            store.add(p)
        # no close(), no flush() -- exactly what install() leaves behind
    """)
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )

    with SQLiteStorage(path, background=False, read_only=False) as reopened:
        assert reopened.count() == 100, "the tail of the capture was lost"


def test_the_row_count_is_shared_between_processes(tmp_path):
    """A per-process counter goes stale the moment another worker clears."""
    path = tmp_path / "shared.db"
    a = SQLiteStorage(path, max_requests=100, background=False)
    b = SQLiteStorage(path, max_requests=100, background=False)
    try:
        for i in range(100):
            a.add(make(i))
        assert a.count() == 100

        b.clear()  # another worker wipes the history
        assert b.count() == 0

        for i in range(50):  # a keeps recording, using its own connection
            a.add(make(1000 + i))
        assert a.count() == 50, "a stale row count made every insert self-delete"
    finally:
        a.close()
        b.close()


def test_concurrent_adds_hold_the_cap(tmp_path):
    store = SQLiteStorage(tmp_path / "conc.db", max_requests=200)
    errors: list[BaseException] = []

    def worker(base):
        try:
            for i in range(300):
                store.add(make(base + i))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w * 100_000,)) for w in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        store.flush()
        assert not errors
        assert store.count() == 200
    finally:
        store.close()


# =====================================================================
# The offline viewer must not destroy the capture it reads
# =====================================================================
def test_a_read_only_store_cannot_write(tmp_path):
    path = tmp_path / "ro.db"
    with SQLiteStorage(path, background=False) as store:
        store.add(make(1))

    reader = SQLiteStorage(path, background=False, read_only=True)
    try:
        assert reader.count() == 1
        reader.add(make(2))  # a no-op, not a crash
        reader.clear()
        assert reader.count() == 1
    finally:
        reader.close()

    with SQLiteStorage(path, background=False) as check:
        assert check.count() == 1


def test_an_incompatible_capture_is_reported_not_rebuilt(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE profiles (id TEXT)")
    legacy.execute("INSERT INTO profiles VALUES ('keep-me')")
    legacy.execute("PRAGMA user_version = 0")
    legacy.commit()
    legacy.close()

    with pytest.raises(IncompatibleCapture, match="different version"):
        SQLiteStorage(path, read_only=True)

    # the file is untouched
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT id FROM profiles").fetchone()[0] == "keep-me"
    conn.close()


def test_the_cli_refuses_an_incompatible_capture(tmp_path, capsys):
    import sqlite3

    from starlette_profiler.__main__ import main

    path = tmp_path / "cli.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE profiles (id TEXT)")
    legacy.execute("PRAGMA user_version = 0")
    legacy.commit()
    legacy.close()

    with pytest.raises(SystemExit):
        main([str(path)])
    assert "different version" in capsys.readouterr().err


# =====================================================================
# statements(): identical answers from both backends
# =====================================================================
def test_replacing_a_profile_does_not_duplicate_its_statements(tmp_path):
    with SQLiteStorage(tmp_path / "replace.db", background=False) as store:
        profile = make(1, route="/r", queries=[Query("SELECT 1", "()", 2.0)])
        for _ in range(3):
            store.add(profile)  # same id, three times

        assert store.count() == 1
        rows = store.statements()
        assert len(rows) == 1
        assert rows[0].count == 1, "statement rows were orphaned by the replace"
        assert rows[0].requests == 1
        assert rows[0].total_ms == pytest.approx(2.0)


def test_trimming_removes_orphaned_statements(tmp_path):
    with SQLiteStorage(tmp_path / "orphan.db", max_requests=3, background=False) as s:
        for i in range(10):
            s.add(make(i, route=f"/r{i}", queries=[Query(f"SELECT {i}", "()", 1.0)]))
            s.add(make(i, route=f"/r{i}", queries=[Query(f"SELECT {i}", "()", 1.0)]))
        assert s.count() == 3
        rows = s.statements()
        assert sum(r.count for r in rows) == 3, "orphan statement rows survived"


def test_statements_ties_resolve_identically(tmp_path):
    """A limit that returns different rows per backend is not one API."""
    queries = [Query(sql, "()", 5.0) for sql in ("SELECT a", "SELECT b", "SELECT c")]
    memory = MemoryStorage()
    memory.add(make(1, route="/r", queries=queries))

    with SQLiteStorage(tmp_path / "tie.db", background=False) as sqlite_store:
        sqlite_store.add(make(1, route="/r", queries=queries))

        for limit in (1, 2, 3):
            assert [r.sql for r in memory.statements(limit)] == [
                r.sql for r in sqlite_store.statements(limit)
            ], f"limit={limit}"


def test_statements_sample_route_agrees(tmp_path):
    shared = "SELECT shared"
    profiles = [
        make(1, route="/zebra", method="POST", queries=[Query(shared, "()", 1.0)]),
        make(2, route="/alpha", method="GET", queries=[Query(shared, "()", 1.0)]),
    ]
    memory = MemoryStorage()
    with SQLiteStorage(tmp_path / "sample.db", background=False) as store:
        for profile in profiles:
            memory.add(profile)
            store.add(profile)

        mine = memory.statements()[0]
        theirs = store.statements()[0]
        assert mine.sample_route == theirs.sample_route == "/alpha"
        assert mine.sample_method == theirs.sample_method == "GET"


def test_a_negative_limit_does_not_invert_the_result(tmp_path):
    queries = [Query(sql, "()", float(i)) for i, sql in enumerate("abc")]
    memory = MemoryStorage()
    memory.add(make(1, queries=queries))
    with SQLiteStorage(tmp_path / "neg.db", background=False) as store:
        store.add(make(1, queries=queries))
        assert memory.statements(-1) == [] == store.statements(-1)


def test_statements_fuzz_agrees_across_backends(tmp_path):
    """A hand-rolled fuzz, seeded so a failure is reproducible."""
    import random

    rng = random.Random(20260816)
    sqls = [f"SELECT {c}" for c in "abcde"]
    routes = ["/a", "/b", "/c"]
    methods = ["GET", "POST"]

    for case in range(60):
        memory = MemoryStorage(max_requests=1000)
        store = SQLiteStorage(
            tmp_path / f"fuzz{case}.db", max_requests=1000, background=False
        )
        try:
            for idx in range(rng.randint(1, 12)):
                queries = [
                    Query(
                        rng.choice(sqls),
                        "()",
                        float(rng.randint(0, 4)),
                        error="x" if rng.random() < 0.2 else None,
                    )
                    for _ in range(rng.randint(0, 5))
                ]
                profile = make(
                    idx,
                    route=rng.choice(routes),
                    method=rng.choice(methods),
                    queries=queries,
                )
                memory.add(profile)
                store.add(profile)

            for limit in (1, 3, 100):
                mine = [
                    (r.sql, r.count, r.requests, r.route_count, r.failures)
                    for r in memory.statements(limit)
                ]
                theirs = [
                    (r.sql, r.count, r.requests, r.route_count, r.failures)
                    for r in store.statements(limit)
                ]
                assert mine == theirs, f"case {case}, limit {limit}"
        finally:
            store.close()


def test_aggregate_statements_is_stable_under_ties():
    queries = [Query(sql, "()", 1.0) for sql in ("SELECT c", "SELECT a", "SELECT b")]
    rows = aggregate_statements([make(1, queries=queries)])
    assert [r.sql for r in rows] == ["SELECT a", "SELECT b", "SELECT c"]
