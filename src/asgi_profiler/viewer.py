"""The viewer: a self-contained Starlette app you mount wherever you like.

Every URL it emits is relative to the mount prefix, so the UI works unchanged
at `/profiler`, at `/admin/_perf`, or behind a proxy that sets a root path.
Nothing is hardcoded, and `url_for` is not used, because a mounted sub-app
cannot resolve its own route names without knowing the name the parent gave
the mount.

The prefix is taken from `scope["root_path"]` when the server sets one --
Starlette's `Mount` does, and it is also what accounts for a proxy prefix --
and otherwise from the explicit `prefix` argument. Relying on `root_path`
alone would be a bet on server-specific behaviour that this library cannot
test from the inside: Litestar's mount, for one, does not set it.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from .config import ProfilerConfig
from .storage import Filters, Storage

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
CSS_PATH = HERE / "static" / "profiler.css"


class _Urls:
    """Mount-aware URL builder handed to the templates."""

    __slots__ = ("root",)

    def __init__(self, request: Request, prefix: str = "") -> None:
        root = request.scope.get("root_path", "").rstrip("/")
        self.root = root or prefix.rstrip("/")

    @property
    def requests(self) -> str:
        return f"{self.root}/"

    @property
    def summary(self) -> str:
        return f"{self.root}/summary"

    @property
    def statements(self) -> str:
        return f"{self.root}/statements"

    @property
    def clear(self) -> str:
        return f"{self.root}/clear"

    @property
    def css(self) -> str:
        return f"{self.root}/static/profiler.css"

    def detail(self, profile_id: str) -> str:
        return f"{self.root}/request/{profile_id}"

    def page(self, params: Any, number: int) -> str:
        """The current listing URL with `page` replaced."""
        query = {k: v for k, v in params.items() if k != "page" and v != ""}
        query["page"] = str(number)
        return f"{self.root}/?{urlencode(query)}"

    def for_route(self, method: str, route: str) -> str:
        return f"{self.root}/?{urlencode({'route': route, 'method': method})}"


def _same_origin(request: Request) -> bool:
    """Reject cross-site state changes.

    `/clear` is a plain POST, so without this any page on the internet can
    make a visiting developer's browser wipe their profiling history with a
    hidden auto-submitting form. Non-browser clients send neither header and
    are left alone.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        # Allow-list, not a deny-list: `same-site` is a *different origin* on a
        # sibling subdomain, which is exactly the attacker we are excluding.
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True  # curl, httpx, the test client
    host = request.headers.get("host", "")
    return origin.split("://", 1)[-1] == host


class Guarded:
    """Wraps the viewer in the caller's `authorize` check.

    A module-level class rather than a closure so that the security boundary
    can be imported, subclassed and tested directly.
    """

    def __init__(self, app: Any, authorize: Any) -> None:
        self.app = app
        self.authorize = authorize

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            verdict = self.authorize(Request(scope, receive))
            if inspect.isawaitable(verdict):
                verdict = await verdict
            if not verdict:
                response = PlainTextResponse("Forbidden", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_viewer(storage: Storage, config: ProfilerConfig, prefix: str = "") -> Any:
    """Return the ASGI app that renders the profiler UI.

    Args:
        storage: where profiles are read from.
        config: the active :class:`ProfilerConfig`.
        prefix: mount prefix, used when the server does not set `root_path`.
    """
    css = CSS_PATH.read_text(encoding="utf-8")

    # Every storage read runs in a worker thread. A `Storage` is a synchronous
    # interface -- SQLite queries, a lock, and a bounded wait for the writer --
    # and calling one directly from an `async def` handler blocks the loop for
    # the whole application, not just this request. Measured: a viewer page
    # froze every other task for five seconds while SQLite waited on a lock.
    async def read(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await run_in_threadpool(fn, *args, **kwargs)

    async def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "config": config,
            "storage_size": await read(storage.count),
            "urls": _Urls(request, prefix),
            **extra,
        }

    async def _page_for(request: Request):
        params = request.query_params
        filters = Filters.from_params(params)
        try:
            number = int(params.get("page", "1"))
        except ValueError:
            number = 1
        page = await read(storage.search, filters, page=number, size=config.page_size)
        return filters, page

    async def requests_page(request: Request) -> Response:
        filters, page = await _page_for(request)
        return TEMPLATES.TemplateResponse(
            request,
            "requests.html",
            await ctx(
                request,
                page=page,
                profiles=page.items,
                total=await read(storage.count),
                order=filters.order,
                filters=request.query_params,
                active_filters=filters.active,
            ),
        )

    async def summary_page(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            "summary.html",
            await ctx(
                request,
                rows=await read(storage.summarise),
                total=await read(storage.count),
            ),
        )

    async def statements_page(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            "statements.html",
            await ctx(
                request,
                rows=await read(storage.statements, config.statement_limit),
                total=await read(storage.count),
            ),
        )

    async def detail_page(request: Request) -> Response:
        profile = await read(storage.get, request.path_params["profile_id"])
        if profile is None:
            return RedirectResponse(_Urls(request, prefix).requests, 302)
        return TEMPLATES.TemplateResponse(
            request,
            "detail.html",
            await ctx(request, profile=profile, groups=profile.query_groups),
        )

    async def clear(request: Request) -> Response:
        if not _same_origin(request):
            return PlainTextResponse("Cross-site request rejected", status_code=403)
        await read(storage.clear)
        return RedirectResponse(_Urls(request, prefix).requests, 303)

    async def stylesheet(request: Request) -> Response:  # noqa: ARG001  (unused-function-argument)
        return Response(
            css,
            media_type="text/css",
            headers={"cache-control": "no-cache"},
        )

    # -- JSON ------------------------------------------------------------
    # The HTML is for reading; this is for scripting. Assert in a test that an
    # endpoint stays under N queries, diff two runs, attach a trace to a bug
    # report -- none of which you can do by scraping a page.
    async def requests_json(request: Request) -> Response:
        _, page = await _page_for(request)
        return JSONResponse(
            {
                "total": page.total,
                "page": page.number,
                "pages": page.pages,
                "size": page.size,
                "requests": [p.as_dict(with_queries=False) for p in page.items],
            }
        )

    async def detail_json(request: Request) -> Response:
        profile = await read(storage.get, request.path_params["profile_id"])
        if profile is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(profile.as_dict())

    async def summary_json(request: Request) -> Response:  # noqa: ARG001  (unused-function-argument)
        rows = await read(storage.summarise)
        return JSONResponse({"routes": [row.as_dict() for row in rows]})

    async def statements_json(request: Request) -> Response:  # noqa: ARG001  (unused-function-argument)
        rows = await read(storage.statements, config.statement_limit)
        return JSONResponse({"statements": [row.as_dict() for row in rows]})

    routes = [
        Route("/", requests_page, name="profiler_requests"),
        Route("/summary", summary_page, name="profiler_summary"),
        Route("/statements", statements_page, name="profiler_statements"),
        # The JSON routes come first: `{profile_id}` matches any non-slash
        # run, so `/request/abc.json` would otherwise be served as HTML for a
        # profile literally named "abc.json".
        Route("/requests.json", requests_json, name="profiler_requests_json"),
        Route("/summary.json", summary_json, name="profiler_summary_json"),
        Route("/statements.json", statements_json, name="profiler_statements_json"),
        Route("/request/{profile_id}.json", detail_json, name="profiler_detail_json"),
        Route("/request/{profile_id}", detail_page, name="profiler_detail"),
        Route("/clear", clear, methods=["POST"], name="profiler_clear"),
        Route("/static/profiler.css", stylesheet, name="profiler_css"),
    ]

    viewer = Starlette(routes=routes)
    if config.authorize is not None:
        return Guarded(viewer, config.authorize)
    return viewer
