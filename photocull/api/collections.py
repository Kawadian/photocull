from __future__ import annotations

import sqlite3
from collections.abc import Container
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi import Path as RoutePath

from photocull.config import LibraryName, Settings
from photocull.db import connect
from photocull.db.collections import (
    AlbumConflict,
    AlbumEdit,
    AlbumSettings,
    BatchLabels,
    Membership,
    Selection,
    active_assets,
    album,
    batch_labels,
    change_membership,
    delete_album,
    list_albums,
    save_album,
)

ALBUM_PATTERN = r"^[0-9a-f]{32}$"
AlbumId = Annotated[str, RoutePath(pattern=ALBUM_PATTERN)]
AssetId = Annotated[int, RoutePath(gt=0, le=2**63 - 1)]


def visible_album(db: sqlite3.Connection, album_id: str, libraries: Container[str]) -> dict:
    result = album(db, album_id)
    if result is None or (result["library"] and result["library"] not in libraries):
        raise HTTPException(404, "Unknown album")
    return result


def collections_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/browse")
    catalogs = {name: settings.library_state(name) / "index.db" for name in settings.libraries}
    collections = settings.state_dir / "browser.db"

    def catalog(library: str) -> Path:
        if library not in catalogs:
            raise HTTPException(404, "Unknown library")
        return catalogs[library]

    def validate(selection: Selection) -> dict[str, set[int]]:
        grouped = selection.by_library()
        for library in grouped:
            catalog(library)
        return grouped

    @router.get("/albums")
    def albums(response: Response):
        response.headers["Cache-Control"] = "private, no-cache"
        with connect(collections) as db:
            return {"items": list_albums(db, catalogs)}

    @router.get("/albums/{album_id}")
    def album_details(album_id: AlbumId, response: Response):
        response.headers["Cache-Control"] = "private, no-cache"
        with connect(collections) as db:
            return visible_album(db, album_id, catalogs)

    @router.get("/{library}/assets/{asset_id}/albums")
    def asset_albums(library: LibraryName, asset_id: AssetId, response: Response):
        response.headers["Cache-Control"] = "private, no-cache"
        with connect(catalog(library)) as db:
            if not active_assets(db, {asset_id}):
                raise HTTPException(404, "Unknown asset")
        with connect(collections) as db:
            return {"items": list_albums(db, catalogs, (library, asset_id))}

    def save(body: AlbumSettings, album_id: str | None = None):
        if body.library:
            catalog(body.library)
        with connect(collections) as db:
            if album_id:
                visible_album(db, album_id, catalogs)
            try:
                result = save_album(db, body, album_id)
            except AlbumConflict as error:
                raise HTTPException(409, str(error)) from error
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            if result is None:
                raise HTTPException(404, "Unknown album")
            return result

    @router.post("/albums", status_code=201)
    def create_album(body: AlbumSettings):
        return save(body)

    @router.patch("/albums/{album_id}")
    def edit_album(album_id: AlbumId, body: AlbumEdit):
        return save(body, album_id)

    @router.delete("/albums/{album_id}", status_code=204)
    def remove_album(album_id: AlbumId, revision: Annotated[int, Query(ge=0, le=2**63 - 1)]):
        with connect(collections) as db:
            visible_album(db, album_id, catalogs)
            try:
                if not delete_album(db, album_id, revision):
                    raise HTTPException(404, "Unknown album")
            except AlbumConflict as error:
                raise HTTPException(409, str(error)) from error

    @router.post("/albums/{album_id}/members")
    def members(album_id: AlbumId, body: Membership):
        grouped = validate(body)
        remove = body.operation == "remove"
        if not remove:
            for library, ids in grouped.items():
                with connect(catalogs[library]) as db:
                    if active_assets(db, ids) != ids:
                        raise HTTPException(409, f"The selection changed in {library}")
        with connect(collections) as db:
            visible_album(db, album_id, catalogs)
            try:
                changed = change_membership(db, album_id, grouped, remove)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        if changed is None:
            raise HTTPException(404, "Unknown album")
        return {"changed": changed}

    @router.patch("/selection/labels")
    def update_selection(body: BatchLabels):
        grouped = validate(body)
        results = []
        for library, ids in grouped.items():
            with connect(catalogs[library]) as db:
                try:
                    updated = batch_labels(db, ids, body)
                    status = "done" if updated else "stale"
                except ValueError:
                    status = "tag_limit"
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode & 0xFF not in {
                        sqlite3.SQLITE_BUSY,
                        sqlite3.SQLITE_LOCKED,
                    }:
                        raise
                    status = "busy"
            results.append({"library": library, "count": len(ids), "status": status})
        return {"items": results}

    return router
