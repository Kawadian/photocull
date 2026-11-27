from __future__ import annotations

import json
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
    v.status AS embedding_status, v.error AS embedding_error,
    s.sharpness, s.exposure, s.clipped_highlights, s.clipped_shadows,
    ({AUTO_REJECT_REASON}) AS auto_reject_reason, m.burst_id
"""
ASSET_JOINS = """
    FROM assets AS a
    LEFT JOIN asset_scores AS s ON s.asset_id = a.id
    LEFT JOIN burst_members AS m ON m.asset_id = a.id
    LEFT JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
    LEFT JOIN jobs AS e ON e.asset_id = a.id AND e.stage = 'exif'
    LEFT JOIN jobs AS q ON q.asset_id = a.id AND q.stage = 'score'
    LEFT JOIN jobs AS v ON v.asset_id = a.id AND v.stage = 'embedding'
"""


@dataclass(frozen=True)
class ScoreFilters:
    min_sharpness: float | None = None
    min_exposure: float | None = None
    max_exposure: float | None = None
    auto_reject: bool | None = None

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
            clauses.append(f"(({AUTO_REJECT_REASON}) IS NOT NULL) = ?")
            params.append(self.auto_reject)
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


def asset_page_query(
    cursor: int,
    limit: int,
    scores: ScoreFilters,
    *,
    folder: str | None = None,
    day: str | None = None,
    burst_id: int | None = None,
    similar_group_id: int | None = None,
    tag: str | None = None,
    tag_key: str | None = None,
    person_id: int | None = None,
    face_key: str | None = None,
    kind: str | None = None,
    aesthetic_key: str | None = None,
    min_aesthetic: float | None = None,
    sort_score: tuple[str, str, float | None] | None = None,
) -> tuple[str, tuple]:
    clauses, score_params = scores.clauses()
    # Order burst members through the membership index. All SQL fragments are
    # fixed predicates and external values are bound below.
    order = "m.asset_id" if burst_id is not None else "a.id"
    joins = ASSET_JOINS
    if similar_group_id is not None:
        joins += " JOIN similar_members AS sm ON sm.asset_id = a.id"
        order = "sm.asset_id"
    filters = ["a.active = 1"]
    params: list[str | int | float] = []
    if not sort_score:
        filters.append(f"{order} > ?")
        params.append(cursor)
    filters.extend(clauses)
    params.extend(score_params)
    for condition, value in (
        ("a.folder = ?", folder),
        ("a.day = ?", day),
        ("m.burst_id = ?", burst_id),
        ("sm.group_id = ?", similar_group_id),
        ("a.kind = ?", kind),
    ):
        if value is not None:
            filters.append(condition)
            params.append(value)
    if tag is not None:
        joins += """ JOIN asset_tags AS t ON t.asset_id = a.id
                     JOIN asset_embeddings AS te ON te.asset_id = a.id"""
        filters.extend(("t.tag = ?", "te.tag_key = ?"))
        params.extend((tag, tag_key or ""))
        if not sort_score:
            filters.append("t.asset_id > ?")
            params.append(cursor)
        order = "t.asset_id"
    if person_id is not None:
        filters.append("""a.id IN (SELECT f.asset_id FROM faces AS f
            WHERE f.person_id = ? AND f.key = ? AND f.asset_id > ?
              AND f.content_hash=a.content_hash)""")
        params.extend((person_id, face_key or "", 0 if sort_score else cursor))
    if min_aesthetic is not None:
        if sort_score and sort_score[:2] == ("aesthetic", aesthetic_key):
            filters.append("ranked.score >= ?")
            params.append(min_aesthetic)
        else:
            filters.append("""(SELECT i.score FROM insight_results AS i WHERE i.stage = 'aesthetic'
                AND i.asset_id = a.id AND i.key = ?) >= ?""")
            params.extend((aesthetic_key or "", min_aesthetic))
    if sort_score:
        stage, key, previous = sort_score
        # Starting from the score index permits keyset pagination on large catalogs.
        joins += " JOIN insight_results AS ranked ON ranked.asset_id = a.id"
        filters.extend(("ranked.stage = ?", "ranked.key = ?", "ranked.score IS NOT NULL"))
        params.extend((stage, key))
        if previous is not None:
            filters.append("ranked.score <= ? AND (ranked.score < ? OR ranked.asset_id > ?)")
            params.extend((previous, previous, cursor))
        order = "ranked.score DESC, ranked.asset_id"
    return (
        f"""SELECT {ASSET_COLUMNS} {joins}
            WHERE {" AND ".join(filters)} ORDER BY {order} LIMIT ?""",
        (*params, limit + 1),
    )


def attach_tags(db: sqlite3.Connection, assets: list[dict], tag_key: str, vocabulary: dict) -> None:
    if not assets:
        return
    by_id = {asset["id"]: asset for asset in assets}
    for asset in assets:
        asset["tags"] = []
    for asset_id, tag, score in db.execute(
        """SELECT t.asset_id, t.tag, t.score FROM asset_tags AS t
            JOIN asset_embeddings AS e ON e.asset_id = t.asset_id
            JOIN assets AS a ON a.id = t.asset_id AND a.content_hash = e.content_hash
            WHERE t.asset_id IN (SELECT value FROM json_each(?)) AND e.tag_key = ?
            ORDER BY t.asset_id, t.score DESC, t.tag""",
        (json.dumps(list(by_id)), tag_key),
    ):
        if tag in vocabulary:
            by_id[asset_id]["tags"].append(
                {"tag": tag, "label": " / ".join(vocabulary[tag]), "score": score}
            )


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
