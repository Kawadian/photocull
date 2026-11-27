from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as RoutePath
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints, model_validator

from photocull.api.archive import mount_archive
from photocull.api.browse import browse_router
from photocull.api.insights import mount_insights
from photocull.api.media import CacheAccess, cached_media, mount_media
from photocull.config import MLSettings, Settings, Stage, load_settings
from photocull.db import connect, initialize
from photocull.db.bursts import (
    BurstConflict,
    choose_best,
    get_burst,
    list_bursts,
    reject_burst_rest,
    undo_burst_reject,
)
from photocull.db.catalog import (
    RatingConflict,
    ScoreFilters,
    asset_json,
    asset_page_query,
    attach_tags,
    clear_auto_reject,
    get_asset,
)
from photocull.db.catalog import set_rating as save_rating
from photocull.db.insights import asset_insights, public_keys
from photocull.db.jobs import progress, request_scan, retry_job, scan_error_page
from photocull.db.search import (
    QueueFull,
    QueueUnavailable,
    SearchTimedOut,
    cancel,
    enqueue,
    get_result,
    worker_online,
)
from photocull.media.cache import cache_path
from photocull.media.xmp import XMPConflict, XMPError, export_xmp

TOKEN_HEADER = "x-photocull-token"
VARIANTS = {"thumb": "thumbs", "preview": "previews", "detail": "details"}
Limit = Annotated[int, Query(ge=1, le=200)]
ScoreBound = Annotated[float, Query(ge=0, le=1)]
SQLITE_MAX_ID = 2**63 - 1
Cursor = Annotated[int, Query(ge=0, le=SQLITE_MAX_ID)]
CatalogId = Annotated[int, RoutePath(gt=0, le=SQLITE_MAX_ID)]
SearchId = Annotated[str, RoutePath(pattern=r"^[a-f0-9]{32}$")]
TagKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


def score_filters(
    min_sharpness: ScoreBound | None = None,
    min_exposure: ScoreBound | None = None,
    max_exposure: ScoreBound | None = None,
    auto_reject: bool | None = None,
) -> ScoreFilters:
    if min_exposure is not None and max_exposure is not None and min_exposure > max_exposure:
        raise HTTPException(422, "min_exposure must be at most max_exposure")
    return ScoreFilters(min_sharpness, min_exposure, max_exposure, auto_reject)


class BurstBestUpdate(BaseModel):
    asset_id: int = Field(gt=0, le=SQLITE_MAX_ID)
    membership_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class SearchParameters(BaseModel):
    tag: TagKey | None = None
    limit: int = Field(default=60, ge=1, le=100)


class SearchQuery(SearchParameters):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


class BurstRejectUpdate(BaseModel):
    best_id: int = Field(gt=0, le=SQLITE_MAX_ID)
    membership_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class RatingUpdate(BaseModel):
    rating: int = Field(ge=0, le=5)
    rejected: bool = False
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def reject_with_rating(self) -> RatingUpdate:
        if self.rejected and self.rating:
            raise ValueError("Set rating to 0 when rejecting an asset")
        return self


class XMPUpdate(BaseModel):
    dry_run: bool = True
    confirmation: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def require_confirmation(self) -> XMPUpdate:
        if not self.dry_run and self.confirmation is None:
            raise ValueError("Preview the XMP update and supply its confirmation")
        return self


def _get_progress(path: Path, settings: MLSettings | None = None) -> dict:
    with connect(path) as db:
        result = progress(db)
        result["worker_online"] = worker_online(db, path, time.time())
    if settings is not None:
        ml = result["ml"]
        configured = ml["enabled"] and ml["model_key"] == settings.model_key
        ml["waiting"] = settings.enabled and not configured
        ml["enabled"] = settings.enabled
        ml["search_enabled"] = settings.enabled and settings.search_enabled
        ml["tags_enabled"] = settings.enabled and settings.tags_enabled
        if not (settings.enabled and configured):
            for field in ("image_error", "text_error", "tag_error", "group_error"):
                ml[field] = None
            ml["grouping"] = False
            result["stages"].pop("embedding", None)
    return result


async def _events(path: Path, settings: MLSettings | None = None) -> AsyncIterator[str]:
    last = ""
    sent = time.monotonic()
    while True:
        payload = json.dumps(await asyncio.to_thread(_get_progress, path, settings))
        if payload != last:
            yield f"data: {payload}\n\n"
            last, sent = payload, time.monotonic()
        elif time.monotonic() - sent >= 10:
            yield ": keepalive\n\n"
            sent = time.monotonic()
        await asyncio.sleep(1)


def _rss_mb() -> float:
    resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
    return round(resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    token: bytes | None = None
    cache_access = CacheAccess()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal token
        if settings.token_file:
            if not (value := settings.token_file.read_bytes().strip()) or any(
                not 33 <= char <= 126 for char in value
            ):
                raise ValueError("Token file must contain a nonempty printable ASCII token")
            token = value
        for name in settings.libraries:
            initialize(settings.library_state(name) / "index.db")
        task = asyncio.create_task(cache_access.run())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Photocull", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    ml = settings.ml
    model_key, tag_key, vocabulary = ml.model_key, ml.tag_key, ml.vocabulary

    def library_path(library: str) -> Path:
        if library not in settings.libraries:
            raise HTTPException(404, "Unknown library")
        return settings.library_state(library) / "index.db"

    def require_ml(*, text: bool = False) -> None:
        if not ml.enabled:
            raise HTTPException(409, "Photo embeddings are disabled")
        if text and not ml.search_enabled:
            raise HTTPException(409, "Text search is disabled")

    def require_tag(tag: str | None) -> None:
        if tag is not None:
            require_ml()
            if not ml.tags_enabled:
                raise HTTPException(409, "Photo tags are disabled")
            if tag not in vocabulary:
                raise HTTPException(422, "Unknown photo tag")

    def add_tags(db, items: list[dict]) -> None:
        if ml.enabled and ml.tags_enabled:
            attach_tags(db, items, tag_key, vocabulary)

    @app.middleware("http")
    async def access(request: Request, call_next):
        if token is not None and not compare_digest(
            request.headers.get(TOKEN_HEADER, "").encode("latin-1"), token
        ):
            return JSONResponse({"detail": "Photocull token required"}, status_code=401)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            origin = request.headers.get("origin")
            host = request.headers.get("host", "").lower()
            cross_origin = origin and origin.lower() not in {f"http://{host}", f"https://{host}"}
            if cross_origin or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
        return await call_next(request)

    @app.get("/api/health")
    def health():
        libraries = {}
        for name in settings.libraries:
            libraries[name] = _get_progress(library_path(name), ml)
        return {"status": "ok", "rss_mb": _rss_mb(), "libraries": libraries}

    @app.get("/api/libraries")
    def libraries():
        return {"items": [{"name": name} for name in settings.libraries]}

    @app.get("/api/libraries/{library}/groups")
    def groups(
        library: str,
        by: Literal["folder", "day"] = "folder",
        cursor: str | None = None,
        limit: Limit = 100,
    ):
        clause = f"AND {by} > ?" if cursor is not None else ""
        params = (cursor, limit + 1) if cursor is not None else (limit + 1,)
        with connect(library_path(library)) as db:
            rows = db.execute(
                f"""SELECT {by} AS name, count(*) AS count FROM assets
                    WHERE active = 1 {clause} GROUP BY {by} ORDER BY {by} LIMIT ?""",
                params,
            ).fetchall()
        return {
            "items": [dict(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["name"] if len(rows) > limit else None,
        }

    @app.get("/api/libraries/{library}/assets")
    def assets(
        library: str,
        scores: Annotated[ScoreFilters, Depends(score_filters)],
        cursor: Cursor = 0,
        limit: Limit = 100,
        folder: str | None = None,
        day: str | None = None,
        burst_id: Annotated[int, Query(gt=0, le=SQLITE_MAX_ID)] | None = None,
        similar_group_id: Annotated[int, Query(gt=0, le=SQLITE_MAX_ID)] | None = None,
        tag: TagKey | None = None,
        person_id: Annotated[int, Query(gt=0, le=SQLITE_MAX_ID)] | None = None,
        kind: Literal["image", "raw", "heif", "video"] | None = None,
        min_aesthetic: ScoreBound | None = None,
        sort: Literal["id", "aesthetic", "taste"] = "id",
    ):
        path = library_path(library)
        require_tag(tag)
        if similar_group_id is not None:
            require_ml()
        with connect(path) as db:
            db.execute("BEGIN")
            keys = public_keys(db, settings)
            if person_id is not None and "faces" not in keys:
                raise HTTPException(409, "Face grouping is disabled")
            if min_aesthetic is not None and "aesthetic" not in keys:
                raise HTTPException(409, "Aesthetic scoring is disabled")
            if sort == "aesthetic" and "aesthetic" not in keys:
                raise HTTPException(409, "Aesthetic scoring is disabled")
            if sort == "taste" and not (settings.insights.taste and ml.enabled):
                raise HTTPException(409, "Taste learning is disabled")
            sort_score = None
            if sort != "id":
                key = keys.get(sort, "")
                previous = None
                if cursor:
                    row = db.execute(
                        "SELECT score FROM insight_results WHERE stage=? AND key=? AND asset_id=?",
                        (sort, key, cursor),
                    ).fetchone()
                    if row is None or row[0] is None:
                        raise HTTPException(409, "Scores changed. Reload the results.")
                    previous = row[0]
                sort_score = (sort, key, previous)
            group_id = similar_group_id
            if (
                group_id is not None
                and not db.execute(
                    "SELECT 1 FROM ml_state WHERE enabled = 1 AND model_key = ?", (model_key,)
                ).fetchone()
            ):
                group_id = -1
            rows = db.execute(
                *asset_page_query(
                    cursor,
                    limit,
                    scores,
                    folder=folder,
                    day=day,
                    burst_id=burst_id,
                    similar_group_id=group_id,
                    tag=tag,
                    tag_key=tag_key,
                    person_id=person_id,
                    face_key=keys.get("faces"),
                    kind=kind,
                    aesthetic_key=keys.get("aesthetic"),
                    min_aesthetic=min_aesthetic,
                    sort_score=sort_score,
                )
            ).fetchall()
            items = [asset_json(row) for row in rows[:limit]]
            asset_insights(db, keys, items)
            add_tags(db, items)
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if len(rows) > limit else None,
        }

    @app.get("/api/libraries/{library}/similar-groups")
    def similar_groups(library: str, cursor: Cursor = 0, limit: Limit = 100):
        path = library_path(library)
        require_ml()
        with connect(path) as db:
            rows = db.execute(
                """SELECT g.id, a.path, g.member_count FROM similar_groups AS g
                   JOIN assets AS a ON a.id = g.id
                   WHERE g.id > ? AND EXISTS (
                       SELECT 1 FROM ml_state WHERE enabled = 1 AND model_key = ?
                   ) ORDER BY g.id LIMIT ?""",
                (cursor, model_key, limit + 1),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @app.get("/api/libraries/{library}/tags")
    def tags(library: str):
        library_path(library)
        return {
            "items": [
                {"tag": tag, "label": " / ".join(vocabulary[tag])} for tag in sorted(vocabulary)
            ]
            if ml.enabled and ml.tags_enabled
            else [],
        }

    def submit_search(library: str, params: SearchParameters, *, query=None, asset_id=None):
        path = library_path(library)
        require_ml(text=query is not None)
        require_tag(params.tag)
        with connect(path, timeout_ms=500) as db:
            try:
                search_id = enqueue(
                    db, path, ml, query=query, asset_id=asset_id, tag=params.tag, limit=params.limit
                )
            except QueueUnavailable as error:
                raise HTTPException(503, str(error)) from error
            except QueueFull as error:
                raise HTTPException(429, str(error)) from error
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                    raise
                raise HTTPException(503, "The catalog is busy. Try the search again.") from error
        return {"id": search_id}

    @app.post("/api/libraries/{library}/search", status_code=202)
    def search(library: str, params: SearchQuery):
        return submit_search(library, params, query=params.query)

    @app.post("/api/libraries/{library}/assets/{asset_id}/similar", status_code=202)
    def similar(library: str, asset_id: CatalogId, params: SearchParameters):
        return submit_search(library, params, asset_id=asset_id)

    @app.get("/api/libraries/{library}/search/{search_id}")
    def search_result(library: str, search_id: SearchId):
        path = library_path(library)
        require_ml()
        with connect(path) as db:
            db.execute("BEGIN")
            try:
                result = get_result(
                    db, path, search_id, model_key, tag_key if ml.tags_enabled else None
                )
            except QueueUnavailable as error:
                raise HTTPException(503, str(error)) from error
            except SearchTimedOut as error:
                raise HTTPException(504, str(error)) from error
            if result is None:
                raise HTTPException(404, "Unknown or expired search")
            add_tags(db, result["items"])
            return result

    @app.delete("/api/libraries/{library}/search/{search_id}", status_code=204)
    def cancel_search(library: str, search_id: SearchId):
        with connect(library_path(library), timeout_ms=500) as db:
            try:
                cancel(db, search_id)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                    raise
                raise HTTPException(503, "The catalog is busy.") from error
        return Response(status_code=204)

    @app.get("/api/libraries/{library}/bursts")
    def bursts(
        library: str,
        scores: Annotated[ScoreFilters, Depends(score_filters)],
        cursor: Cursor = 0,
        limit: Limit = 100,
    ):
        with connect(library_path(library)) as db:
            return list_bursts(db, cursor, limit, scores)

    @app.get("/api/libraries/{library}/bursts/{burst_id}")
    def burst(library: str, burst_id: CatalogId):
        with connect(library_path(library)) as db:
            try:
                return get_burst(db, burst_id)
            except LookupError as error:
                raise HTTPException(404, str(error)) from error

    @app.patch("/api/libraries/{library}/bursts/{burst_id}/best")
    def set_burst_best(library: str, burst_id: CatalogId, update: BurstBestUpdate):
        with connect(library_path(library)) as db:
            try:
                return choose_best(db, burst_id, **update.model_dump())
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except BurstConflict as error:
                raise HTTPException(409, str(error)) from error
            except ValueError as error:
                raise HTTPException(422, str(error)) from error

    @app.post("/api/libraries/{library}/bursts/{burst_id}/reject-rest")
    def reject_rest(library: str, burst_id: CatalogId, update: BurstRejectUpdate):
        with connect(library_path(library)) as db:
            try:
                return reject_burst_rest(db, burst_id, **update.model_dump())
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except BurstConflict as error:
                raise HTTPException(409, str(error)) from error

    @app.delete("/api/libraries/{library}/burst-rejections/{token}")
    def undo_burst_rejections(
        library: str, token: Annotated[str, RoutePath(pattern=r"^[a-f0-9]{32}$")]
    ):
        with connect(library_path(library)) as db:
            return {"changed": undo_burst_reject(db, token)}

    @app.delete("/api/libraries/{library}/auto-reject")
    def clear_all_rejections(library: str):
        with connect(library_path(library)) as db:
            return {"changed": clear_auto_reject(db)}

    @app.delete("/api/libraries/{library}/assets/{asset_id}/auto-reject")
    def clear_rejection(library: str, asset_id: CatalogId):
        with connect(library_path(library)) as db:
            try:
                return {"changed": clear_auto_reject(db, asset_id)}
            except LookupError as error:
                raise HTTPException(404, str(error)) from error

    @app.get("/api/libraries/{library}/assets/{asset_id}")
    def asset(library: str, asset_id: CatalogId):
        with connect(library_path(library)) as db:
            db.execute("BEGIN")
            row = get_asset(db, asset_id)
            if row is None:
                raise HTTPException(404, "Unknown asset")
            items = [asset_json(row)]
            asset_insights(db, public_keys(db, settings), items)
            add_tags(db, items)
            return items[0]

    @app.patch("/api/libraries/{library}/assets/{asset_id}/rating")
    def set_rating(library: str, asset_id: CatalogId, update: RatingUpdate):
        with connect(library_path(library)) as db:
            try:
                rating = save_rating(db, asset_id, **update.model_dump())
            except RatingConflict as error:
                raise HTTPException(409, str(error)) from error
            if rating is None:
                raise HTTPException(404, "Unknown asset")
            return rating

    @app.post("/api/libraries/{library}/assets/{asset_id}/xmp")
    def xmp(library: str, asset_id: CatalogId, update: XMPUpdate):
        library_path(library)
        try:
            return asdict(export_xmp(settings, library, asset_id, **update.model_dump()))
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except XMPConflict as error:
            raise HTTPException(409, str(error)) from error
        except XMPError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/libraries/{library}/assets/{asset_id}/media/{variant}")
    def media(library: str, asset_id: CatalogId, variant: Literal["thumb", "preview", "detail"]):
        db_path = library_path(library)
        with connect(db_path) as db:
            row = db.execute(
                """SELECT a.content_hash FROM assets AS a
                   JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
                   WHERE a.id = ? AND a.active = 1 AND p.status = 'done'""",
                (asset_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Preview unavailable")
        target = cache_path(db_path.parent, VARIANTS[variant], asset_id, row["content_hash"])
        fallback = None
        if variant == "detail":
            fallback = cache_path(db_path.parent, "previews", asset_id, row["content_hash"])
        return cached_media(target, "image/jpeg", fallback=fallback)

    @app.post("/api/libraries/{library}/scans", status_code=202)
    def enqueue_scan(library: str):
        with connect(library_path(library)) as db:
            return dict(request_scan(db))

    @app.get("/api/libraries/{library}/scans/{scan_id}/errors")
    def scan_errors(library: str, scan_id: CatalogId, cursor: Cursor = 0, limit: Limit = 50):
        with connect(library_path(library)) as db:
            page = scan_error_page(db, scan_id, cursor, limit)
        if page is None:
            raise HTTPException(404, "Scan not found")
        return page

    @app.get("/api/libraries/{library}/jobs")
    def job_progress(library: str):
        return _get_progress(library_path(library), ml)

    @app.post("/api/libraries/{library}/assets/{asset_id}/jobs/{stage}", status_code=202)
    def rerun(library: str, asset_id: CatalogId, stage: Stage):
        path = library_path(library)
        if (stage == "embedding" and not ml.enabled) or (
            stage != "embedding" and stage not in settings.worker.stages
        ):
            raise HTTPException(409, "Stage disabled by worker settings")
        with connect(path) as db:
            job = retry_job(db, asset_id, stage)
        if job is None:
            raise HTTPException(404, "Unknown asset or stage")
        if job["status"] == "skipped":
            raise HTTPException(409, job["error"])
        return dict(job)

    @app.get("/api/libraries/{library}/events")
    async def events(library: str):
        return StreamingResponse(
            _events(library_path(library), ml),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", include_in_schema=False)
    def frontend():
        index = settings.static_dir / "index.html"
        if not index.is_file():
            raise HTTPException(503, "Frontend build unavailable. Build frontend/dist first.")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    mount_insights(app, settings, library_path)
    mount_archive(app, settings, library_path)
    app.include_router(browse_router(settings))
    app.include_router(mount_media(settings, library_path, cache_access))
    if (settings.static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=settings.static_dir / "assets"), name="frontend")
    return app


app = create_app()


def main() -> None:
    settings = app.state.settings
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, proxy_headers=False)
