from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from photocull.db import write
from photocull.db.catalog import AUTO_REJECT_REASON, mark_catalog_updated

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


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def ordered_days(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("End date must be on or after the start date")
        return self


def artifact(state: Path, job: str, kind: str) -> Path:
    return state / "transfers" / f"{job}.{'html' if kind == 'report' else 'zip'}"


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
    result["destination"] = options.get("destination")
    return result


def cancel(db, job_id):
    with write(db):
        row = db.execute(
            """UPDATE transfers SET status = CASE status WHEN 'pending' THEN 'cancelled'
                                                   ELSE 'cancelling' END,
                      finished_at = CASE WHEN status='pending' THEN ? ELSE NULL END
               WHERE id = ? AND status IN ('pending','running','cancelling')
               RETURNING *""",
            (time.time(), job_id),
        ).fetchone()
        if row:
            mark_catalog_updated(db)
        return row


def select_assets(config: Selection, keys: dict):
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
        clauses.append("""EXISTS (SELECT 1 FROM faces AS f WHERE f.asset_id = a.id
                        AND f.person_id = ? AND f.key = ?)""")
        values.extend((config.person_id, keys.get("faces", "")))
    if config.dead is not None:
        joins += """ LEFT JOIN video_flags AS f ON f.asset_id=a.id AND f.content_hash=a.content_hash
                     LEFT JOIN insight_results AS i ON i.asset_id=a.id
                       AND i.content_hash=a.content_hash AND i.stage='video' AND i.key=?"""
        clauses.append(
            "a.kind='video' AND COALESCE(f.dead, "
            "json_array_length(i.payload,'$.reasons') > 0, 0) = ?"
        )
        values.insert(0, keys.get("video", ""))
        values.append(config.dead)
    return (
        f"""SELECT a.id, a.path, a.source_key, a.size FROM assets AS a {joins}
            WHERE {" AND ".join(clauses)} ORDER BY a.id""",
        values,
    )
