"""Render the profiler as a view inside SQLAdmin instead of its own page.

    from asgi_profiler import install
    from asgi_profiler.contrib.sqladmin import register

    admin = Admin(app, engine)
    profiler = install(app, mount_path="/profiler")
    register(admin, profiler)

Why this is worth having rather than just mounting the standalone viewer next
to the admin: SQLAdmin's `expose` wraps every route in its authentication and
accessibility checks. The standalone viewer has no authentication of its own --
that is the loudest warning in its README -- so putting it behind an admin the
user already had to log into is a real improvement, not just a cosmetic one.

The templates extend `sqladmin/layout.html` and use Tabler markup, so the pages
arrive with the admin's sidebar, theme and navigation rather than in an iframe
wearing a different stylesheet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jinja2 import ChoiceLoader, PackageLoader, PrefixLoader
from starlette.responses import RedirectResponse

from ..models import group_queries
from ..storage import Filters

if TYPE_CHECKING:  # pragma: no cover
    from starlette.requests import Request

    from .. import Profiler

#: Our templates are namespaced so they cannot collide with a template the
#: application or SQLAdmin already has by that name.
_PREFIX = "asgi_profiler"


def _install_loader(templates: Any) -> None:
    """Teach SQLAdmin's Jinja environment where our templates live.

    SQLAdmin builds a `ChoiceLoader` over the user's directory and its own
    package. Ours has to be appended, or `extends "sqladmin/layout.html"`
    resolves and our own template name does not.
    """
    ours = PrefixLoader({_PREFIX: PackageLoader("asgi_profiler", "templates/sqladmin")})
    loader = templates.env.loader
    if isinstance(loader, ChoiceLoader):
        if not any(
            isinstance(existing, PrefixLoader) and _PREFIX in existing.mapping
            for existing in loader.loaders
        ):
            loader.loaders.append(ours)
    else:  # pragma: no cover - SQLAdmin has always used a ChoiceLoader
        templates.env.loader = ChoiceLoader([loader, ours])


def register(
    admin: Any,
    profiler: Profiler,
    *,
    name: str = "Profiler",
    icon: str = "fa-solid fa-gauge-high",
    category: str = "",
    profile_admin: bool = False,
) -> type:
    """Add the profiler to `admin` and return the generated view class.

    Args:
        admin: a `sqladmin.Admin` instance.
        profiler: the handle returned by :func:`asgi_profiler.install`.
        name, icon, category: how the entry appears in SQLAdmin's sidebar.
        profile_admin: also record requests to the admin itself. Off by
            default, and rarely worth turning on: SQLAdmin builds its own
            queries and they are already tuned, so recording them buries your
            application's requests under admin traffic. It is off rather than
            merely discouraged because the profiler instruments SQLAlchemy's
            `Engine` class and SQLAdmin is a SQLAlchemy application, so with it
            on, opening the profiler records the profiler -- every visit adding
            its own pages to the history you came to read.

            Turn it on when the admin itself is what you are debugging: a slow
            `ModelView`, an expensive `column_list`, a custom view of your own.
    """
    from sqladmin import BaseView, expose  # imported late: an optional extra

    if not profile_admin:
        profiler.exclude(admin.base_url)

    _install_loader(admin.templates)
    storage = profiler.storage
    config = profiler.config

    #: SQLAdmin names an exposed route `view-<identity>` and mounts its
    #: sub-application under `admin`, so the list route resolves as
    #: `admin:view-profiler`. That is its internal convention rather than a
    #: documented API, hence the fallback: if the name ever changes the links
    #: keep working off `base_url` instead of the view 500ing.
    route_name = "admin:view-profiler"
    fallback_base = admin.base_url.rstrip("/") + "/profiler"

    def base_url(request: Request) -> str:
        try:
            return str(request.url_for(route_name))
        except Exception:  # pragma: no cover - only if SQLAdmin renames routes
            return fallback_base

    # Bound late, because the class is not defined yet where `context` is used.
    view_name, view_icon, view_category = name, icon, category

    class ProfilerView(BaseView):  # type: ignore[misc, valid-type]
        # Assigned in the class body rather than onto the class afterwards:
        # a type checker cannot see attributes bolted on later, and neither can
        # anyone reading this.
        name = view_name
        icon = view_icon
        category = view_category
        identity = "profiler"

        def context(self, request: Request) -> dict[str, Any]:
            # `view` is what the sidebar highlights; `base` is our URL root, so
            # the templates never hard-code where SQLAdmin mounted us.
            return {
                "view": type(self),
                "base": base_url(request),
                "config": config,
            }

        @expose("/profiler", methods=["GET"], identity="profiler")
        async def requests_page(self, request: Request) -> Any:
            filters = Filters.from_params(request.query_params)
            number = _int(request.query_params.get("page"), 1)
            page = storage.search(filters, page=number, size=config.page_size)
            return await self.templates.TemplateResponse(
                request,
                f"{_PREFIX}/requests.html",
                {**self.context(request), "page": page, "filters": filters},
            )

        @expose("/profiler/request/{profile_id}", methods=["GET"])
        async def detail_page(self, request: Request) -> Any:
            profile = storage.get(request.path_params["profile_id"])
            if profile is None:
                # Through the same resolver as the templates. Calling `url_for`
                # directly here is what made a missing profile a 500 rather
                # than the redirect it is supposed to be.
                return RedirectResponse(base_url(request), 303)
            return await self.templates.TemplateResponse(
                request,
                f"{_PREFIX}/detail.html",
                {
                    **self.context(request),
                    "profile": profile,
                    "groups": group_queries(profile.queries),
                },
            )

        @expose("/profiler/summary", methods=["GET"])
        async def summary_page(self, request: Request) -> Any:
            return await self.templates.TemplateResponse(
                request,
                f"{_PREFIX}/summary.html",
                {**self.context(request), "rows": storage.summarise()},
            )

        @expose("/profiler/clear", methods=["POST"])
        async def clear(self, request: Request) -> Any:
            """Discard the recorded history.

            POST, and a form rather than a fetch: SQLAdmin carries no CSRF
            token of its own, so a hand-rolled one here would protect nothing
            the rest of the admin does not already expose. `expose` has already
            put this behind the admin's authentication.
            """
            storage.clear()
            return RedirectResponse(base_url(request), 303)

    admin.add_base_view(ProfilerView)
    return ProfilerView


def _int(raw: str | None, default: int) -> int:
    try:
        return max(1, int(raw or default))
    except (TypeError, ValueError):
        return default
