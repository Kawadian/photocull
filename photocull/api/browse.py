from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from binascii import Error as BinaryError
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError

from photocull.config import LibraryName, Settings
from photocull.db.browse import TimelineKey, timeline_months, timeline_page
from photocull.media.derivatives import recipe

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
MonthQuery = Annotated[str, Query(pattern=MONTH_PATTERN)]
CursorQuery = Annotated[str, Query(max_length=384, pattern=r"^[A-Za-z0-9_-]+$")]


class TimelineCursor(BaseModel):
    day: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    time: str = Field(max_length=32)
    library: LibraryName
    asset_id: int = Field(gt=0, le=2**63 - 1)
    scope: LibraryName | None
    month: str | None

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

    @router.get("/timeline")
    def timeline(
        response: Response,
        library: LibraryName | None = None,
        month: MonthQuery | None = None,
        cursor: CursorQuery | None = None,
        limit: Annotated[int, Query(ge=1, le=120)] = 72,
    ):
        selected = select(library)
        after = None
        if cursor is not None:
            value = TimelineCursor.decode(cursor)
            if value.scope != library or value.month != month or value.library not in selected:
                raise HTTPException(422, "Cursor does not match the timeline scope")
            after = TimelineKey(value.day, value.time, value.library, value.asset_id)

        items, next_key = timeline_page(selected, limit, month, after, encoding)
        next_cursor = (
            TimelineCursor(**next_key._asdict(), scope=library, month=month).encode()
            if next_key
            else None
        )
        response.headers["Cache-Control"] = "private, no-cache"
        return {"items": items, "next_cursor": next_cursor, "media": media}

    return router
