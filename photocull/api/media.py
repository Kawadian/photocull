from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import ExitStack
from email.utils import formatdate, parsedate_to_datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as RoutePath
from fastapi.responses import FileResponse, JSONResponse, Response

from photocull.config import Settings
from photocull.db import connect
from photocull.db.derivatives import cache_status, request_file, touch_files
from photocull.db.search import HEARTBEAT_TIMEOUT
from photocull.media.artifacts import open_artifact
from photocull.media.derivatives import (
    VARIANTS,
    VERSION_PATTERN,
    WORKER_DISABLED,
    WORKER_RECIPE_FILE,
    Variant,
    derivative_path,
    recipe,
    relative_path,
)
from photocull.media.walk import MediaJail, OutsideLibrary, open_directory, source_key

Id = Annotated[int, RoutePath(gt=0, le=2**63 - 1)]
Version = Annotated[str, RoutePath(pattern=VERSION_PATTERN)]
LOG = logging.getLogger(__name__)
MAX_PENDING_TOUCHES = 8192
IMMUTABLE = "private, max-age=31536000, immutable"
NO_STORE = "private, no-store"


class CacheAccess:
    def __init__(self) -> None:
        self.pending: dict[tuple[Path, int, str, str], float] = {}
        self.lock = Lock()

    def touch(self, path: Path, asset_id: int, variant: str, version: str) -> None:
        with self.lock:
            key = (path, asset_id, variant, version)
            if key in self.pending or len(self.pending) < MAX_PENDING_TOUCHES:
                self.pending[key] = time.time()

    def flush(self) -> None:
        with self.lock:
            pending, self.pending = self.pending, {}
        batches: dict[Path, list[tuple[float, int, str, str]]] = {}
        for (path, asset_id, variant, version), stamp in pending.items():
            batches.setdefault(path, []).append((stamp, asset_id, variant, version))
        for path, batch in batches.items():
            with connect(path, timeout_ms=500) as db:
                try:
                    touch_files(db, batch)
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                        raise
                    with self.lock:
                        for stamp, asset_id, variant, version in batch:
                            if len(self.pending) < MAX_PENDING_TOUCHES:
                                self.pending.setdefault((path, asset_id, variant, version), stamp)

    async def flush_safely(self) -> None:
        try:
            await asyncio.to_thread(self.flush)
        except sqlite3.Error:
            LOG.exception("Could not flush derivative cache access timestamps")

    async def run(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                await self.flush_safely()
        finally:
            await self.flush_safely()


def worker_recipe(db: sqlite3.Connection, catalog: Path) -> str | None:
    worker = db.execute("SELECT pid, heartbeat FROM worker_status WHERE id = 1").fetchone()
    if worker is None or time.time() - worker["heartbeat"] >= HEARTBEAT_TIMEOUT:
        return None
    try:
        with open_artifact(catalog.parent / WORKER_RECIPE_FILE) as (fd, _):
            pid, value = os.read(fd, 80).decode("ascii").split()
    except (OSError, ValueError):
        return None
    if pid == str(worker["pid"]) and (
        value == WORKER_DISABLED or re.fullmatch(r"[a-f0-9]{16}", value)
    ):
        return value
    return None


@lru_cache(maxsize=128)
def warn_recipe(library: str, api_recipe: str, active_recipe: str) -> None:
    LOG.warning(
        "Derivative recipe mismatch for %s (API %s, worker %s)", library, api_recipe, active_recipe
    )


def not_modified(request: Request, etag: str, modified: float) -> bool:
    if (match := request.headers.get("if-none-match")) is not None:
        return any(tag.strip().removeprefix("W/") in ("*", etag) for tag in match.split(","))
    if since := request.headers.get("if-modified-since"):
        try:
            return int(modified) <= parsedate_to_datetime(since).timestamp()
        except (ValueError, OverflowError):
            pass
    return False


def cached_info(target: Path) -> os.stat_result | None:
    try:
        with open_directory(target.parent.absolute()) as parent:
            info = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def derivative_response(
    request: Request, target: Path, mime: str, etag: str, redirect: str | None = None
):
    info = cached_info(target)
    if info is None:
        return None
    if redirect is not None:
        return Response(headers={"X-Accel-Redirect": redirect, "Content-Type": mime})
    headers = {
        "Cache-Control": IMMUTABLE,
        "ETag": etag,
        "Last-Modified": formatdate(info.st_mtime, usegmt=True),
        "X-Content-Type-Options": "nosniff",
    }
    if not_modified(request, etag, info.st_mtime):
        return Response(status_code=304, headers=headers)
    return cached_media(target, mime, headers=headers)


def cached_media(
    target: Path,
    mime: str,
    *,
    headers: dict[str, str] | None = None,
    fallback: Path | None = None,
):
    def open_cached(stack: ExitStack):
        try:
            return stack.enter_context(open_artifact(target))
        except FileNotFoundError:
            if fallback is None:
                raise
            return stack.enter_context(open_artifact(fallback))

    return OpenMedia(
        open_cached,
        mime,
        headers or {"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"},
    )


class OpenMedia(Response):
    def __init__(
        self,
        opener: Callable,
        mime: str,
        file_headers: dict[str, str],
        filename: str | None = None,
    ):
        super().__init__()
        self.opener = opener
        self.mime = mime
        self.file_headers = file_headers
        self.filename = filename

    async def __call__(self, scope, receive, send):
        with ExitStack() as stack:
            try:
                fd, info = self.opener(stack)
            except (OSError, OutsideLibrary):
                response = Response(status_code=404, headers={"Cache-Control": NO_STORE})
            else:
                response = FileResponse(
                    f"/proc/self/fd/{fd}",
                    stat_result=info,
                    media_type=self.mime,
                    headers=self.file_headers,
                    filename=self.filename,
                )
            # The ASGI server must open the path while the pinned descriptor is still live.
            if "http.response.pathsend" in scope.get("extensions", {}):
                scope = {
                    **scope,
                    "extensions": {
                        key: value
                        for key, value in scope["extensions"].items()
                        if key != "http.response.pathsend"
                    },
                }
            await response(scope, receive, send)


def mount_media(settings: Settings, library_path, hits: CacheAccess) -> APIRouter:
    config = settings.derivatives
    encoding = recipe(config)
    version_suffix = "-" + encoding
    router = APIRouter()
    base = "/api/libraries/{library}/assets/{asset_id}"

    def asset_for_media(db, asset_id: int, version: str):
        row = db.execute(
            "SELECT kind, path, content_hash FROM assets WHERE id = ? AND active = 1",
            (asset_id,),
        ).fetchone()
        if row is None or version != row["content_hash"] + version_suffix:
            raise HTTPException(404, "Derivative unavailable")
        return row

    def serve(
        request: Request,
        db,
        path: Path,
        library: str,
        asset_id: int,
        variant: Variant,
        version: str,
        *,
        delivery: bool = False,
        filename: str | None = None,
    ):
        name = variant.name
        entry = cache_status(db, asset_id, name, version)
        target = derivative_path(settings, library, asset_id, variant, version)
        if entry and entry["status"] == "ready":
            if delivery and cached_info(target) is not None:
                hits.touch(path, asset_id, name, version)
                return OpenMedia(
                    lambda stack: stack.enter_context(open_artifact(target)),
                    variant.mime,
                    {
                        "Cache-Control": NO_STORE,
                        "X-Content-Type-Options": "nosniff",
                        "Cross-Origin-Resource-Policy": "same-origin",
                    },
                    filename=filename,
                )
            if not delivery:
                redirect = None
                if config.accel_redirect:
                    relative = relative_path(library, asset_id, variant, version).as_posix()
                    redirect = f"/_photocull/{variant.tier}/{relative}"
                if response := derivative_response(
                    request, target, variant.mime, f'"{version}-{name}"', redirect
                ):
                    hits.touch(path, asset_id, name, version)
                    return response
        worker = worker_recipe(db, path)
        if worker is not None and worker != encoding:
            warn_recipe(library, encoding, worker)
            return JSONResponse(
                {"status": "recipe_mismatch"},
                status_code=503,
                headers={"Cache-Control": NO_STORE, "Retry-After": "5"},
            )
        if entry and (
            entry["status"] == "running"
            or (entry["status"] == "pending" and entry["priority"] == 1)
        ):
            status = "pending"
        else:
            try:
                status = request_file(db, asset_id, variant, version, time.time())
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                    raise
                status = "busy"
        code, delay = {"pending": (202, "2"), "busy": (503, "5"), "error": (409, "300")}[status]
        return JSONResponse(
            {"status": status},
            status_code=code,
            headers={"Cache-Control": NO_STORE, "Retry-After": delay},
        )

    @router.api_route(base + "/media/{name}/{version}", methods=["GET", "HEAD"])
    def derivative(request: Request, library: str, asset_id: Id, name: str, version: Version):
        path = library_path(library)
        variant = VARIANTS.get(name)
        if not config.enabled or variant is None or variant.include_location:
            raise HTTPException(404, "Derivative unavailable")
        with connect(path, timeout_ms=500) as db:
            row = asset_for_media(db, asset_id, version)
            if variant.mime == "video/mp4" and row["kind"] != "video":
                raise HTTPException(404, "Derivative unavailable")
            return serve(request, db, path, library, asset_id, variant, version)

    @router.api_route(base + "/delivery/{version}", methods=["GET", "HEAD"])
    def deliver(
        request: Request,
        library: str,
        asset_id: Id,
        version: Version,
        action: Literal["share", "download"],
        include_location: bool | None = None,
    ):
        path = library_path(library)
        if not config.enabled:
            raise HTTPException(404, "Derivative unavailable")
        include = settings.browser.includes_location(include_location)
        with connect(path, timeout_ms=500) as db:
            row = asset_for_media(db, asset_id, version)
            video = row["kind"] == "video"
            if video:
                variant = VARIANTS["location-video.mp4" if include else "video-720.mp4"]
            else:
                variant = VARIANTS["location-photo.jpg" if include else "fit-2560.jpg"]
            filename = None
            if action == "download":
                filename = PurePosixPath(row["path"]).stem + "." + variant.extension
            return serve(
                request,
                db,
                path,
                library,
                asset_id,
                variant,
                version,
                delivery=True,
                filename=filename,
            )

    @router.api_route(base + "/original", methods=["GET", "HEAD"])
    def original(
        library: str,
        asset_id: Id,
        mode: Literal["download", "stream"] = "download",
        include_location: bool | None = None,
    ):
        path = library_path(library)
        if not (config.original_download if mode == "download" else config.original_streaming):
            raise HTTPException(404, "Original media unavailable")
        if include_location is not True:
            raise HTTPException(422, "Original media requires include_location=true")
        with connect(path) as db:
            row = db.execute(
                "SELECT path, kind, source_key FROM assets WHERE id = ? AND active = 1",
                (asset_id,),
            ).fetchone()
        if row is None or (mode == "stream" and row["kind"] != "video"):
            raise HTTPException(404, "Original media unavailable")
        title = PurePosixPath(row["path"]).name
        mime = "application/octet-stream"
        if mode == "stream":
            mime = {
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".m4v": "video/mp4",
                ".mkv": "video/x-matroska",
            }.get(Path(title).suffix.lower(), mime)

        def open_original(stack: ExitStack):
            jail = MediaJail(settings.libraries[library], settings.state_dir, validate=False)
            fd, _ = stack.enter_context(jail.open_media(row["path"]))
            info = os.fstat(fd)
            if source_key(info) != row["source_key"]:
                raise OSError("Source changed")
            return fd, info

        return OpenMedia(
            open_original,
            mime,
            {"Cache-Control": NO_STORE, "X-Content-Type-Options": "nosniff"},
            filename=title if mode == "download" else None,
        )

    return router
