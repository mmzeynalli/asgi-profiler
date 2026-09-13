# SQLAdmin

If you already run [SQLAdmin](https://github.com/aminalaee/sqladmin), the
profiler can be one of its views instead of a page of its own.

```console
pip install "asgi-profiler[sqladmin]"
```

```python
from sqladmin import Admin
from asgi_profiler import install
from asgi_profiler.contrib.sqladmin import register

admin = Admin(app, engine)

profiler = install(app, mount_path=None)
register(admin, profiler)
```

A **Profiler** entry appears in the admin's sidebar with three pages —
requests, request detail and summary — rendered in SQLAdmin's own layout,
theme and dark mode.

## Why bother

Two reasons, and the first is the real one.

**It gets authentication.** SQLAdmin's `expose` wraps every route in its
authentication and accessibility checks. The standalone viewer has none of its
own — that is the loudest warning on the [Security](../guide/security.md)
page — so putting it behind an admin you already log into closes that hole
without you writing an `authorize` callback.

**It is one less thing to find.** The profiler lives where you already go to
look at data, rather than at a URL you have to remember.

## The admin is not recorded by default

The profiler attaches its listeners to SQLAlchemy's `Engine` **class**, and
SQLAdmin is a SQLAlchemy application. Left alone, browsing the admin gets
profiled along with the queries SQLAdmin ran to draw each page — including the
profiler's own pages, so every visit would add itself to the history you came
to read.

So `register()` excludes the admin's own URL by default. If you want it back:

```python
register(admin, profiler, profile_admin=True)
```

Worth turning on only when the admin itself is what you are debugging — a slow
`ModelView`, an expensive `column_list`, a custom view of your own. SQLAdmin's
built-in queries are already tuned, so in normal use this just buries your
application's requests under admin traffic.

## Options

| Argument | Default | What it does |
| --- | --- | --- |
| `name` | `"Profiler"` | Sidebar label |
| `icon` | `"fa-solid fa-gauge-high"` | Sidebar icon (FontAwesome or Tabler) |
| `category` | `""` | Group it under a sidebar category |
| `profile_admin` | `False` | Also record requests to the admin itself |

```python
register(admin, profiler, name="Performance", category="Diagnostics")
```

## Dropping the standalone viewer

`mount_path=None` records without mounting the viewer at all, which is what
you want when the pages are served by the admin — otherwise the same data is
reachable at two URLs and only one of them is authenticated.

Keep both if you have a reason to:

```python
profiler = install(app)  # viewer still at /profiler
register(admin, profiler)  # and inside the admin
```

## Clearing the history

The requests page has a **Clear** button behind a confirmation dialog. It is a
`POST`, so a link prefetch or a crawler cannot wipe the history by following a
URL.

!!! note "No CSRF token"

    SQLAdmin does not carry CSRF tokens — its own delete action is an
    unprotected `fetch`. A hand-rolled token here would protect nothing the
    rest of the admin does not already expose, so the form matches SQLAdmin's
    existing posture rather than inventing a different one.

## Async engines

`register()` reads from whatever `Storage` the profiler was given; it does not
touch your engine. An `AsyncEngine` application works unchanged — SQLAdmin
itself runs its queries through `anyio.to_thread.run_sync`, and the capture
follows the contextvar into the worker thread.

## Version coupling

Links resolve through `request.url_for("admin:view-profiler")`, which is
SQLAdmin's internal route-naming convention rather than a documented API. If a
future SQLAdmin changes it, the view falls back to composing the URL from
`admin.base_url` — links keep working rather than the page returning 500.

Tested against SQLAdmin 0.31; the extra declares `sqladmin>=0.16`.
