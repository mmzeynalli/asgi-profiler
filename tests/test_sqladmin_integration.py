"""The SQLAdmin integration, exercised rather than merely claimed.

The interesting assertion here is the exclusion one. The profiler instruments
SQLAlchemy's `Engine` class, and SQLAdmin is a SQLAlchemy application, so
without `Profiler.exclude` every page of the admin is recorded along with the
queries it ran -- including the profiler's own pages, which then fill the
history with themselves every time anyone looks at it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base
from starlette.testclient import TestClient

from asgi_profiler import install

sqladmin = pytest.importorskip("sqladmin")

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String)


@pytest.fixture
def app_and_profiler(tmp_path):
    return _build(tmp_path)


def _build(tmp_path, **register_kwargs):
    from fastapi import FastAPI
    from sqladmin import Admin, ModelView

    from asgi_profiler.contrib.sqladmin import register

    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for i in range(5):
            conn.execute(text("INSERT INTO widgets (name) VALUES (:n)"), {"n": f"w{i}"})

    app = FastAPI()

    @app.get("/things/{thing_id}")
    def things(thing_id: int):
        with engine.connect() as conn:
            for _ in range(6):  # past n_plus_one_count, so a finding renders
                conn.execute(text("SELECT COUNT(*) FROM widgets")).scalar()
        return {"id": thing_id}

    admin = Admin(app, engine, title="Test Admin")

    class WidgetAdmin(ModelView, model=Widget):
        column_list = [Widget.id, Widget.name]

    admin.add_view(WidgetAdmin)
    profiler = install(app, mount_path=None)
    register(admin, profiler, **register_kwargs)
    return app, profiler


def test_the_admin_is_not_recorded_into_its_own_profiler(app_and_profiler):
    app, profiler = app_and_profiler
    with TestClient(app) as client:
        client.get("/things/1")
        client.get("/admin/widget/list")
        client.get("/admin/profiler")
        client.get("/admin/profiler")

    recorded = {p.group for p in profiler.profiles}
    assert recorded == {"/things/{thing_id}"}, (
        "browsing the admin was recorded; the profiler will fill its own "
        f"history with itself. Recorded: {sorted(recorded)}"
    )


def test_every_view_renders_inside_the_admin_layout(app_and_profiler):
    app, profiler = app_and_profiler
    with TestClient(app) as client:
        client.get("/things/1")
        profile = profiler.profiles[0]

        listing = client.get("/admin/profiler")
        assert listing.status_code == 200
        # the admin's own chrome, i.e. we really are inside its layout
        assert "Test Admin" in listing.text
        assert "/things/1" in listing.text

        detail = client.get(f"/admin/profiler/request/{profile.id}")
        assert detail.status_code == 200
        assert "Test Admin" in detail.text
        assert "SELECT COUNT(*) FROM widgets" in detail.text
        assert "Findings" in detail.text
        assert "N+1 query: 6 identical statements" in detail.text

        summary = client.get("/admin/profiler/summary")
        assert summary.status_code == 200
        assert "/things/{thing_id}" in summary.text


def test_an_unknown_profile_redirects_rather_than_500s(app_and_profiler):
    app, _ = app_and_profiler
    with TestClient(app) as client:
        response = client.get("/admin/profiler/request/deadbeef", follow_redirects=False)
    assert response.status_code == 303


def test_the_profiler_appears_in_the_admin_sidebar(app_and_profiler):
    app, _ = app_and_profiler
    with TestClient(app) as client:
        assert "Profiler" in client.get("/admin/").text


def test_exclude_takes_effect_after_install(tmp_path):
    """`install()` happens before the admin exists, so this has to work late."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/kept")
    def kept():
        return {}

    @app.get("/dropped")
    def dropped():
        return {}

    profiler = install(app)
    with TestClient(app) as client:
        client.get("/kept")
        client.get("/dropped")
        assert len(profiler.profiles) == 2

        profiler.exclude("/dropped")
        client.get("/kept")
        client.get("/dropped")

    paths = [p.path for p in profiler.profiles]
    assert paths.count("/dropped") == 1, "exclude() did not take effect"
    assert paths.count("/kept") == 2


def test_profile_admin_records_the_admin_when_asked(tmp_path):
    """The opposite direction, which nothing else in this file exercises."""
    app, profiler = _build(tmp_path, profile_admin=True)
    with TestClient(app) as client:
        client.get("/things/1")
        client.get("/admin/widget/list")

    recorded = {p.group for p in profiler.profiles}
    assert "/things/{thing_id}" in recorded
    assert any(group.startswith("/admin") for group in recorded), (
        f"profile_admin=True did not record the admin. Recorded: {sorted(recorded)}"
    )


def test_clear_empties_the_history_and_redirects(app_and_profiler):
    app, profiler = app_and_profiler
    with TestClient(app) as client:
        client.get("/things/1")
        client.get("/things/2")
        assert len(profiler.profiles) == 2

        response = client.post("/admin/profiler/clear", follow_redirects=False)
        assert response.status_code == 303
        assert profiler.profiles == []

        # and the button is on the page that offers it
        client.get("/things/3")
        assert "profiler-clear" in client.get("/admin/profiler").text


def test_clear_is_not_reachable_by_GET(app_and_profiler):
    """A link crawler or a prefetch must not be able to wipe the history."""
    app, profiler = app_and_profiler
    with TestClient(app) as client:
        client.get("/things/1")
        assert client.get("/admin/profiler/clear").status_code == 405
        assert len(profiler.profiles) == 1


def test_mount_path_none_records_without_mounting_the_viewer(app_and_profiler):
    """Inside SQLAdmin the standalone viewer is redundant, so it is not mounted."""
    app, profiler = app_and_profiler
    with TestClient(app) as client:
        client.get("/things/1")
        assert len(profiler.profiles) == 1, "recording must still work"
        assert client.get("/_profiler/").status_code == 404
        assert client.get("/admin/profiler").status_code == 200
