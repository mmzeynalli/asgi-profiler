"""Serve the viewer against a `profiler.db` file, with no application.

    python -m starlette_profiler profiler.db

Capturing in staging and reading the file on your laptop should not require
booting the application that produced it. The viewer is already a
self-contained ASGI app over a `Storage`, so this is mostly argument parsing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ProfilerConfig
from .storage import IncompatibleCapture, SQLiteStorage
from .viewer import build_viewer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m starlette_profiler",
        description="Browse a starlette-profiler SQLite capture.",
    )
    parser.add_argument("database", type=Path, help="path to a profiler.db file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--page-size", type=int, default=50, help="rows per page (default: 50)"
    )
    args = parser.parse_args(argv)

    if not args.database.exists():
        parser.error(f"no such file: {args.database}")

    # The capture is checked before the server dependency, so a bad file is
    # reported as a bad file rather than as a missing uvicorn.
    #
    # Read-only: opening a capture to look at it must never be able to rewrite
    # or drop it.
    try:
        storage = SQLiteStorage(args.database, background=False, read_only=True)
    except IncompatibleCapture as exc:
        parser.error(str(exc))

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - depends on the environment
        storage.close()
        parser.error("uvicorn is required to serve the viewer: pip install uvicorn")

    config = ProfilerConfig(mount_path="", page_size=args.page_size)
    app = build_viewer(storage, config)

    print(f"{storage.count()} requests in {args.database}")
    print(f"viewer on http://{args.host}:{args.port}/")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        storage.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
