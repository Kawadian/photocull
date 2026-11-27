from __future__ import annotations

import json
import time
from datetime import date
from pathlib import PurePosixPath
from sqlite3 import Connection
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from photocull.config import InsightSettings
from photocull.db import write
from photocull.db.catalog import AUTO_REJECT_REASON, mark_catalog_updated
from photocull.insights import classify_video
from photocull.media.export_location import MEDIA_SUFFIXES, unsupported_reason

MAX_QUEUED = 16


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_rating: int = Field(ge=0, le=5, default=3)
    rejected: bool | None = False
    auto_reject: bool | None = None
    dead: bool | None = None
    person_id: Annotated[int, Field(gt=0, le=2**63 - 1)] | None = None
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def ordered_days(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("End date must be on or after the start date")
        return self


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["zip", "copy"]
    destination: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    selection: Selection = Field(default_factory=Selection)
    xmp: bool = False
    include_location: bool | None = None


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_location: bool | None = None

    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def ordered_days(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("End date must be on or after the start date")
        return self


def enqueue(db, kind, body):
    with write(db):
        total = db.execute(
            "SELECT count(*) FROM transfers WHERE status IN ('pending','running','cancelling')"
        ).fetchone()[0]
        if total >= MAX_QUEUED:
            raise ValueError("The transfer queue is full")
        job_id = uuid4().hex
        db.execute(
            "INSERT INTO transfers(id, kind, status, request, created_at) VALUES (?,?,?,?,?)",
            (job_id, kind, "pending", body.model_dump_json(), time.time()),
        )
        mark_catalog_updated(db)
    return job_id


def public(row):
    result = dict(row)
    options = json.loads(result.pop("request"))
    result["cancellable"] = result.pop("output_inode") is None and result["status"] in {
        "pending",
        "running",
    }
    del result["output_device"]
    result["destination"] = options.get("destination")
    include_location = options.get("include_location")
    if result["kind"] != "report" or include_location is not None:
        result["include_location"] = include_location is not False
    return result


def cancel(db, job_id):
    with write(db):
        row = db.execute(
            """UPDATE transfers SET status = CASE status WHEN 'pending' THEN 'cancelled'
                                                   ELSE 'cancelling' END,
                      finished_at = CASE WHEN status='pending' THEN ? ELSE NULL END
               WHERE id = ? AND output_inode IS NULL
                 AND status IN ('pending','running','cancelling')
               RETURNING *""",
            (time.time(), job_id),
        ).fetchone()
        if row:
            mark_catalog_updated(db)
        return row


def select_assets(db: Connection, config: Selection, keys: dict, insights: InsightSettings):
    clauses = ["a.active=1", "a.rating>=?"]
    values = [config.minimum_rating]
    for value, clause in (
        (config.rejected, "a.rejected = ?"),
        (config.auto_reject, f"(({AUTO_REJECT_REASON}) IS NOT NULL) = ?"),
        (config.start, "a.day >= ?"),
        (config.end, "a.day <= ?"),
    ):
        if value is not None:
            clauses.append(clause)
            values.append(value.isoformat() if isinstance(value, date) else value)
    joins = "LEFT JOIN asset_scores AS s ON s.asset_id = a.id"
    if config.person_id is not None:
        clauses.append("""a.id IN (SELECT f.asset_id FROM faces AS f
                        WHERE f.person_id = ? AND f.key = ? AND f.content_hash=a.content_hash)""")
        values.extend((config.person_id, keys.get("faces", "")))
    if config.dead is not None:

        def automatic_dead(payload: str | None) -> bool:
            return payload is not None and classify_video(json.loads(payload), insights)[1]

        db.create_function("video_is_dead", 1, automatic_dead, deterministic=True)
        joins += """ LEFT JOIN video_flags AS f ON f.asset_id=a.id AND f.content_hash=a.content_hash
                     LEFT JOIN insight_results AS i ON i.asset_id=a.id
                       AND i.content_hash=a.content_hash AND i.stage='video' AND i.key=?"""
        clauses.append("COALESCE(f.dead, video_is_dead(i.payload), 0) = ?")
        values = [keys.get("video", ""), *values]
        values.append(config.dead)
    return (
        f"""SELECT a.id, a.path, a.source_key, a.size, a.kind FROM assets AS a {joins}
            WHERE {" AND ".join(clauses)} ORDER BY a.id""",
        values,
    )


def export_preview(db, request, keys, insights, after, limit=50):
    selection, params = select_assets(db, request.selection, keys, insights)
    if request.include_location:
        total = db.execute(f"SELECT count(*) FROM ({selection})", params).fetchone()[0]
        return {"total": total, "skipped": 0, "items": [], "next_cursor": None}
    accepted = " OR ".join("lower(path) LIKE ?" for _ in MEDIA_SUFFIXES)
    suffixes = [f"%{suffix}" for suffix in MEDIA_SUFFIXES]
    total, skipped = db.execute(
        f"SELECT count(*), coalesce(sum(NOT ({accepted})), 0) FROM ({selection})",
        (*suffixes, *params),
    ).fetchone()
    rows = db.execute(
        f"""SELECT id,path FROM ({selection}) WHERE id > ? AND NOT ({accepted})
            ORDER BY id LIMIT ?""",
        (*params, after, *suffixes, limit + 1),
    ).fetchall()
    return {
        "total": total,
        "skipped": skipped,
        "items": [
            {"path": row["path"], "reason": unsupported_reason(PurePosixPath(row["path"]).suffix)}
            for row in rows[:limit]
        ],
        "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


def transfer_skips(db, job_id, after, limit=50):
    rows = db.execute(
        """SELECT asset_id,path,reason FROM transfer_skips
           WHERE transfer_id=? AND asset_id>? ORDER BY asset_id LIMIT ?""",
        (job_id, after, limit + 1),
    ).fetchall()
    return {
        "items": [{"path": row["path"], "reason": row["reason"]} for row in rows[:limit]],
        "next_cursor": rows[limit - 1]["asset_id"] if len(rows) > limit else None,
    }
