from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from binascii import Error as BinaryError
from hashlib import sha256
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi import Path as RoutePath
from pydantic import BaseModel, Field, ValidationError

from photocull.config import LibraryName, Settings
from photocull.db import connect
from photocull.db.browse import TimelineKey, timeline_months, timeline_page
from photocull.db.browse_filters import Filters, LabelUpdate, apply_labels, facets
from photocull.media.derivatives import recipe

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
Id = Annotated[int, RoutePath(gt=0, le=2**63 - 1)]


class TimelineQuery(Filters):
    library: LibraryName | None = None
    month: str | None = Field(default=None, pattern=MONTH_PATTERN)
    cursor: str | None = Field(default=None, max_length=512, pattern=r"^[A-Za-z0-9_-]+$")
    limit: int = Field(default=72, ge=1, le=120)


class TimelineCursor(BaseModel):
    day: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    time: str = Field(max_length=32)
    library: LibraryName
    asset_id: int = Field(gt=0, le=2**63 - 1)
    scope: LibraryName | None
    month: str | None
    filters: str = ""

    @classmethod
    def decode(cls, cursor: str) -> TimelineCursor:
        try:
            return cls.model_validate_json(
                b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            )
        except (BinaryError, ValidationError) as error:
            raise HTTPException(422, "Invalid timeline cursor") from error

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


def browse_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/browse")
    catalogs = {name: settings.library_state(name) / "index.db" for name in settings.libraries}
    config = settings.derivatives
    encoding = recipe(config) if config.enabled else None
    media = {
        "recipe": encoding,
        "original_download": config.original_download,
        "original_streaming": config.original_streaming,
    }

    def select(library: str | None) -> dict[str, Path]:
        if library is None:
            return catalogs
        if library not in catalogs:
            raise HTTPException(404, "Unknown library")
        return {library: catalogs[library]}

    @router.get("/months")
    def months(response: Response, library: LibraryName | None = None):
        response.headers["Cache-Control"] = "private, no-cache"
        return {"items": timeline_months(select(library))}

    @router.get("/media")
    def browse_media():
        return media

    @router.get("/facets")
    def filter_options(response: Response, library: LibraryName | None = None):
        response.headers["Cache-Control"] = "private, no-cache"
        return facets(select(library), settings)

    @router.patch("/{library}/assets/{asset_id}/labels")
    def update_labels(library: LibraryName, asset_id: Id, change: LabelUpdate):
        with connect(select(library)[library]) as db:
            try:
                result = apply_labels(db, asset_id, change)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        if result is None:
            raise HTTPException(404, "Unknown asset")
        return result

    @router.get("/timeline")
    def timeline(
        response: Response,
        query: Annotated[TimelineQuery, Query()],
    ):
        library, month, cursor, limit = query.library, query.month, query.cursor, query.limit
        scoped = query.model_dump_json(
            exclude={"library", "month", "cursor", "limit"}, exclude_none=True
        )
        fingerprint = sha256(scoped.encode()).hexdigest()[:16] if scoped != "{}" else ""
        selected = select(library)
        after = None
        if cursor is not None:
            value = TimelineCursor.decode(cursor)
            if (
                value.scope != library
                or value.month != month
                or value.library not in selected
                or value.filters != fingerprint
            ):
                raise HTTPException(422, "Cursor does not match the timeline scope")
            after = TimelineKey(value.day, value.time, value.library, value.asset_id)

        items, next_key = timeline_page(selected, limit, month, after, encoding, query, settings)
        next_cursor = (
            TimelineCursor(
                **next_key._asdict(), scope=library, month=month, filters=fingerprint
            ).encode()
            if next_key
            else None
        )
        response.headers["Cache-Control"] = "private, no-cache"
        return {"items": items, "next_cursor": next_cursor, "media": media}

    return router
