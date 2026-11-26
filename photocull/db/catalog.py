from __future__ import annotations

import sqlite3

from photocull.db import write

ASSET_COLUMNS = """
    a.id, a.path, a.folder, a.kind, a.size, a.day, a.content_hash, a.captured_at,
    a.make, a.model, a.latitude, a.longitude, a.metadata_error, a.width, a.height,
    a.duration, a.rating, a.rejected, a.revision,
    p.status AS preview_status, p.error AS preview_error,
    e.status AS exif_status, e.error AS exif_error
"""
ASSET_JOINS = """
    FROM assets AS a
    LEFT JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
    LEFT JOIN jobs AS e ON e.asset_id = a.id AND e.stage = 'exif'
"""


class RatingConflict(ValueError):
    pass


def asset_json(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["rejected"] = bool(value["rejected"])
    return value


def get_asset(db: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
    return db.execute(
        f"SELECT {ASSET_COLUMNS} {ASSET_JOINS} WHERE a.active = 1 AND a.id = ?", (asset_id,)
    ).fetchone()


def set_rating(
    db: sqlite3.Connection,
    asset_id: int,
    rating: int,
    rejected: bool,
    expected_revision: int,
) -> dict | None:
    with write(db):
        row = db.execute(
            "SELECT rating, rejected, revision FROM assets WHERE id = ? AND active = 1", (asset_id,)
        ).fetchone()
        if row is None:
            return None
        if row["revision"] != expected_revision:
            raise RatingConflict("Rating changed. Refresh the asset and try again.")
        if row["rating"] != rating or bool(row["rejected"]) != rejected:
            row = db.execute(
                """UPDATE assets SET rating = ?, rejected = ?, revision = revision + 1
                   WHERE id = ? RETURNING rating, rejected, revision""",
                (rating, rejected, asset_id),
            ).fetchone()
            db.execute("UPDATE catalog_status SET revision = revision + 1 WHERE id = 1")
        return asset_json(row)
