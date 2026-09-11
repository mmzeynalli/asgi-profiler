"""Request capture."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import ProfilerConfig
from .instrument import current_queries
from .models import Profile, Query
from .routing import request_path, route_path, route_pattern
from .storage import Storage

logger = logging.getLogger("asgi_profiler")


class ProfilerMiddleware:
    """Pure ASGI, one `Profile` per HTTP request.

    Pure ASGI rather than `BaseHTTPMiddleware` on purpose: the latter runs the
    downstream app in a separate task, which breaks contextvar propagation --
    exactly what query attribution depends on.
    """

    def __init__(
        self,
        app: ASGIApp,
        storage: Storage,
        config: ProfilerConfig | None = None,
    ) -> None:
        self.app = app
        self.storage = storage
        self.config = config or ProfilerConfig()
        self._excludes = self.config.build_excludes()
        self._redacted = {h.lower() for h in self.config.redacted_headers}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        original_root = scope.get("root_path", "")
        full_path = request_path(scope)
        # Match exclusions against the app-relative path: `mount_path` and
        # `exclude_paths` are written against the application's own routing
        # table, not against whatever prefix it is deployed under.
        if self._excluded(route_path(scope)):
            await self.app(scope, receive, send)
            return

        profile = Profile(
            id=uuid.uuid4().hex[:12],
            method=scope.get("method", "GET"),
            path=full_path,
            query_string=scope.get("query_string", b"").decode("latin-1"),
            client=self._client(scope),
        )
        if self.config.capture_headers:
            profile.request_headers = self._headers(scope.get("headers", []))

        header_name = self.config.response_header
        stamp = header_name.lower().encode("latin-1") if header_name else None

        queries: list[Query] = []
        token = current_queries.set(queries)
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                profile.status_code = message["status"]
                if stamp is not None:
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (stamp, profile.id.encode("latin-1")),
                        ],
                    }
                if self.config.capture_headers:
                    profile.response_headers = self._headers(message.get("headers", []))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # An unhandled exception is turned into a 500 by
            # ServerErrorMiddleware, which sits *outside* us -- so we never see
            # the http.response.start. Record it ourselves and re-raise.
            if not profile.status_code:
                profile.status_code = 500
            raise
        finally:
            profile.duration_ms = (time.perf_counter() - started) * 1000
            # A snapshot, not the live list: a fire-and-forget task inherits
            # this context and would otherwise keep appending after the
            # counters below are frozen, leaving the detail page showing
            # "0 queries" above a list of statements.
            profile.queries = list(queries)
            profile.route = route_pattern(scope, original_root)
            profile.finalise()
            current_queries.reset(token)
            try:
                self.storage.add(profile)
            except Exception:  # pragma: no cover - a wedged backend
                # The response has already gone out. A full disk or a locked
                # database must not turn every successful request into a
                # logged ASGI exception, and must never be able to take the
                # application down -- a profiler is not load-bearing.
                logger.warning("Could not record profile", exc_info=True)

    # -- helpers ---------------------------------------------------------
    def _excluded(self, path: str) -> bool:
        """Match on segment boundaries.

        A bare `startswith` would make the default `/profiler` mount swallow an
        application's own `/profiler-admin`, silently and with no error --
        which reads to the user as "the profiler is broken".
        """
        for prefix in self._excludes:
            trimmed = prefix.rstrip("/")
            if not trimmed:
                return True  # excluded at the root
            if path == trimmed or path.startswith(trimmed + "/"):
                return True
        return False

    def _headers(self, raw: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in raw:
            name = key.decode("latin-1") if isinstance(key, bytes) else str(key)
            name = name.lower()
            if name in self._redacted:
                out[name] = "<redacted>"
                continue
            out[name] = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        return out

    @staticmethod
    def _client(scope: Scope) -> str:
        client = scope.get("client")
        if not client:
            return ""
        host, port = client
        return f"{host}:{port}"
