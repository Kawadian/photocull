from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Iterable
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from photocull.config import LibraryName
from photocull.db import write
from photocull.db.browse_filters import Filters, LabelUpdate, Stars, modify_labels, tag_key
from photocull.db.catalog import mark_catalog_updated

MIGRATIONS = ("browser_0001_albums.sql",)
MAX_SELECTION = 200
AssetId = Annotated[int, Field(gt=0, le=2**63 - 1)]
AlbumName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class AlbumSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AlbumName
    kind: Literal["manual", "smart"] = "manual"
    library: LibraryName | None = None
    filters: Filters | None = None

    @model_validator(mode="after")
    def check_settings(self) -> AlbumSettings:
        if not self.name.isprintable():
            raise ValueError("Album names must use printable characters")
        if self.kind == "manual" and (self.library is not None or self.filters is not None):
            raise ValueError("Only smart albums can store filters and a library")
        return self


class AlbumEdit(AlbumSettings):
    expected_revision: Annotated[int, Field(ge=0, le=2**63 - 1)]


class AssetRef(BaseModel):
    library: LibraryName
    id: AssetId
    expected_revision: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = None


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets: list[AssetRef] = Field(min_length=1, max_length=MAX_SELECTION)

    def by_library(self) -> dict[str, set[int]]:
        grouped: dict[str, set[int]] = {}
        for asset in self.assets:
            grouped.setdefault(asset.library, set()).add(asset.id)
        return grouped


class Membership(Selection):
    operation: Literal["add", "remove"] = "add"


class BatchLabels(Selection, LabelUpdate):
    rating: Stars | None = None

    @model_validator(mode="after")
    def rating_revisions(self) -> BatchLabels:
        if self.rating is not None:
            keys = {(asset.library, asset.id) for asset in self.assets}
            if any(asset.expected_revision is None for asset in self.assets):
                raise ValueError("A rating requires an expected_revision for every asset")
            if len(keys) != len(self.assets):
                raise ValueError("A rating requires unique asset references")
        return self


class AlbumConflict(Exception):
    pass


def _album(row: sqlite3.Row) -> dict:
    data = dict(row)
    if data["filters"] is not None:
        data["filters"] = json.loads(data["filters"])
    return data


ALBUM_FIELDS = "a.id, a.name, a.kind, a.library, a.filters, a.revision"


def album(db: sqlite3.Connection, album_id: str) -> dict | None:
    row = db.execute(
        f"SELECT {ALBUM_FIELDS} FROM albums AS a WHERE a.id = ?", (album_id,)
    ).fetchone()
    return _album(row) if row else None


def list_albums(
    db: sqlite3.Connection, libraries: Iterable[str], member: tuple[str, int] | None = None
) -> list[dict]:
    args = (json.dumps(list(libraries)),)
    criteria = ""
    if member is not None:
        criteria = """AND EXISTS (SELECT 1 FROM album_members AS p
            WHERE p.album_id = a.id AND p.library = ? AND p.asset_id = ?)"""
        args = (*args, *member)
    return [
        _album(row)
        for row in db.execute(
            f"""SELECT {ALBUM_FIELDS}, (SELECT count(*) FROM album_members AS m
                    WHERE m.album_id = a.id AND m.library IN
                        (SELECT value FROM json_each(?1))) AS count
                FROM albums AS a
                WHERE (a.library IS NULL OR a.library IN (SELECT value FROM json_each(?1)))
                {criteria} ORDER BY a.name_key""",
            args,
        )
    ]


def save_album(
    db: sqlite3.Connection, body: AlbumSettings, album_id: str | None = None
) -> dict | None:
    name = unicodedata.normalize("NFC", body.name)
    filters = (
        (body.filters or Filters()).model_dump_json(exclude_none=True)
        if body.kind == "smart"
        else None
    )
    values = (name, tag_key(name), body.kind, body.library, filters)
    try:
        with write(db):
            if album_id is None:
                album_id = uuid4().hex
                db.execute(
                    """INSERT INTO albums(name, name_key, kind, library, filters, id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (*values, album_id),
                )
            else:
                old = album(db, album_id)
                if old is None:
                    return None
                assert isinstance(body, AlbumEdit)
                if old["revision"] != body.expected_revision:
                    raise AlbumConflict("Album changed. Refresh and try again.")
                if old["kind"] != body.kind:
                    raise ValueError("The album kind cannot be changed")
                db.execute(
                    """UPDATE albums SET name = ?, name_key = ?, kind = ?, library = ?, filters = ?,
                           revision = revision + 1 WHERE id = ?""",
                    (*values, album_id),
                )
            return album(db, album_id)
    except sqlite3.IntegrityError as error:
        if error.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE":
            raise AlbumConflict("That album name is already in use") from error
        raise


def delete_album(db: sqlite3.Connection, album_id: str, revision: int) -> bool:
    with write(db):
        old = album(db, album_id)
        if old is None:
            return False
        if old["revision"] != revision:
            raise AlbumConflict("Album changed. Refresh and try again.")
        db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        return True


def active_assets(db: sqlite3.Connection, ids: set[int]) -> set[int]:
    return {
        row[0]
        for row in db.execute(
            "SELECT id FROM assets WHERE active = 1 AND id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(ids)),),
        )
    }


def change_membership(
    db: sqlite3.Connection, album_id: str, ids: dict[str, set[int]], remove: bool
):
    with write(db):
        record = album(db, album_id)
        if record is None:
            return None
        if record["kind"] != "manual":
            raise ValueError("Only manual albums accept selected photos")
        rows = ((album_id, lib, asset) for lib, assets in ids.items() for asset in sorted(assets))
        statement = (
            "DELETE FROM album_members WHERE album_id = ? AND library = ? AND asset_id = ?"
            if remove
            else "INSERT OR IGNORE INTO album_members(album_id, library, asset_id) VALUES (?, ?, ?)"
        )
        return db.executemany(statement, rows).rowcount


def batch_labels(
    db: sqlite3.Connection, library: str, ids: set[int], update: BatchLabels
) -> Literal["done", "stale", "conflict"]:
    id_list = json.dumps(sorted(ids))
    with write(db):
        if update.rating is not None:
            actual = dict(
                db.execute(
                    """SELECT id, revision FROM assets
                       WHERE active = 1 AND id IN (SELECT value FROM json_each(?))""",
                    (id_list,),
                ).fetchall()
            )
            if actual.keys() != ids:
                return "stale"
            if any(
                actual[asset.id] != asset.expected_revision
                for asset in update.assets
                if asset.library == library
            ):
                return "conflict"
        elif active_assets(db, ids) != ids:
            return "stale"
        changed = False
        if update.add_tags or update.remove_tags or update.favorite is not None:
            for asset_id in sorted(ids):
                _, modified = modify_labels(db, asset_id, update)
                changed |= modified
        if update.rating is not None:
            rating = update.rating
            changed |= bool(
                db.execute(
                    """UPDATE assets SET rating = ?,
                           rejected = CASE WHEN ? > 0 THEN 0 ELSE rejected END,
                           revision = revision + 1
                       WHERE id IN (SELECT value FROM json_each(?))
                       AND (rating <> ? OR (? > 0 AND rejected = 1))""",
                    (rating, rating, id_list, rating, rating),
                ).rowcount
            )
        if changed:
            mark_catalog_updated(db)
        return "done"
