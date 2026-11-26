from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from photocull.db import write

AUTO_REJECT_REASON = """
    CASE WHEN a.burst_reject_token IS NOT NULL THEN 'burst_remaining'
         WHEN a.auto_reject_dismissed = 0 THEN s.suggested_reject_reason END
"""

ASSET_COLUMNS = f"""
    a.id, a.path, a.folder, a.kind, a.size, a.day, a.content_hash, a.captured_at,
    a.make, a.model, a.latitude, a.longitude, a.metadata_error, a.width, a.height,
    a.duration, a.rating, a.rejected, a.revision,
    p.status AS preview_status, p.error AS preview_error,
    e.status AS exif_status, e.error AS exif_error,
    q.status AS score_status, q.error AS score_error,
    s.sharpness, s.exposure, s.clipped_highlights, s.clipped_shadows,
    ({AUTO_REJECT_REASON}) AS auto_reject_reason, m.burst_id
"""
ASSET_JOINS = """
    FROM assets AS a
    LEFT JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
    LEFT JOIN jobs AS e ON e.asset_id = a.id AND e.stage = 'exif'
    LEFT JOIN jobs AS q ON q.asset_id = a.id AND q.stage = 'score'
    LEFT JOIN asset_scores AS s ON s.asset_id = a.id
    LEFT JOIN burst_members AS m ON m.asset_id = a.id
"""


@dataclass(frozen=True)
class ScoreFilters:
    min_sharpness: float | None
    min_exposure: float | None
    max_exposure: float | None
    auto_reject: bool | None

    def clauses(self) -> tuple[list[str], list[float]]:
        clauses, params = [], []
        for condition, value in (
            ("s.sharpness >= ?", self.min_sharpness),
            ("s.exposure >= ?", self.min_exposure),
            ("s.exposure <= ?", self.max_exposure),
        ):
            if value is not None:
                clauses.append(condition)
                params.append(value)
        if self.auto_reject is not None:
            operator = "IS NOT NULL" if self.auto_reject else "IS NULL"
            clauses.append(f"({AUTO_REJECT_REASON}) {operator}")
        return clauses, params


class RatingConflict(ValueError):
    pass


def mark_catalog_updated(db: sqlite3.Connection) -> None:
    db.execute("UPDATE catalog_status SET revision = revision + 1 WHERE id = 1")


def asset_json(row: sqlite3.Row) -> dict:
    value = dict(row)
    value["rejected"] = bool(value["rejected"])
    if "auto_reject_reason" in value:
        value["auto_reject"] = value["auto_reject_reason"] is not None
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
            mark_catalog_updated(db)
        return asset_json(row)


def clear_auto_reject(db: sqlite3.Connection, asset_id: int | None = None) -> int:
    with write(db):
        if (
            asset_id is not None
            and not db.execute(
                "SELECT 1 FROM assets WHERE id = ? AND active = 1", (asset_id,)
            ).fetchone()
        ):
            raise LookupError("Unknown asset")
        where = "AND a.id = ?" if asset_id is not None else ""
        count = db.execute(
            f"""UPDATE assets SET auto_reject_dismissed = 1, burst_reject_token = NULL
                WHERE id IN (SELECT a.id FROM assets AS a
                    LEFT JOIN asset_scores AS s ON s.asset_id = a.id
                    WHERE a.active = 1 AND ({AUTO_REJECT_REASON}) IS NOT NULL {where})""",
            () if asset_id is None else (asset_id,),
        ).rowcount
        if count:
            mark_catalog_updated(db)
        return count
