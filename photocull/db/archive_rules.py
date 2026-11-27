from __future__ import annotations

import json
import time
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from photocull.archive import VIDEO, ArchiveError, ArchiveRequest
from photocull.db import write
from photocull.db.archive_preview import queue

CONVERSIONS = VIDEO | {"dng", "jpeg", "heic", "avif"}


class ArchiveRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    plan: ArchiveRequest
    interval_hours: int = Field(default=0, ge=0, le=8760)

    @model_validator(mode="after")
    def conversions_only(self):
        if self.plan.operation not in CONVERSIONS:
            raise ValueError("Saved rules can prepare conversion previews only")
        return self


def rules(db):
    rows = db.execute(
        "SELECT r.*,p.status,p.summary,p.error AS preview_error FROM archive_rules r "
        "LEFT JOIN archive_previews p ON p.id=r.preview_id ORDER BY r.name,r.id"
    )
    return [
        dict(row)
        | {
            "request": json.loads(row["request"]),
            "summary": json.loads(row["summary"]) if row["summary"] else None,
            "unread": row["status"] == "ready" and row["seen_preview_id"] != row["preview_id"],
        }
        for row in rows
    ]


def save(db, rule):
    id_ = uuid4().hex
    with write(db):
        if db.execute("SELECT count(*) FROM archive_rules").fetchone()[0] >= 25:
            raise ArchiveError("At most 25 archive rules can be saved per library.")
        db.execute(
            "INSERT INTO archive_rules(id,name,request,interval_hours,next_at) VALUES(?,?,?,?,?)",
            (
                id_,
                rule.name,
                rule.plan.model_dump_json(),
                rule.interval_hours,
                time.time() if rule.interval_hours else None,
            ),
        )
    return id_


def action(db, id_, command):
    with write(db):
        if command == "delete":
            cursor = db.execute("DELETE FROM archive_rules WHERE id=?", (id_,))
        elif command == "pause":
            cursor = db.execute(
                "UPDATE archive_rules SET interval_hours=0,next_at=NULL WHERE id=?", (id_,)
            )
        elif command == "seen":
            cursor = db.execute(
                "UPDATE archive_rules SET seen_preview_id=preview_id WHERE id=?", (id_,)
            )
        else:
            cursor = db.execute(
                "UPDATE archive_rules SET next_at=?,error=NULL WHERE id=?", (time.time(), id_)
            )
        if not cursor.rowcount:
            raise LookupError("Unknown archive rule")


def schedule(db, settings, library):
    if db.execute(
        "SELECT 1 FROM archive_previews WHERE status IN ('pending','running')"
    ).fetchone():
        return
    now = time.time()
    row = db.execute(
        "SELECT * FROM archive_rules WHERE next_at<=? ORDER BY next_at,id LIMIT 1", (now,)
    ).fetchone()
    if row is None:
        return
    try:
        preview = queue(db, settings, library, ArchiveRequest.model_validate_json(row["request"]))
    except (ArchiveError, OSError) as error:
        # If an operator claimed the preview slot, leave this rule due.
        if db.execute(
            "SELECT 1 FROM archive_previews WHERE status IN ('pending','running')"
        ).fetchone():
            return
        db.execute(
            "UPDATE archive_rules SET error=?,next_at=? WHERE id=? AND next_at=?",
            (str(error), next_time(now, row), row["id"], row["next_at"]),
        )
    else:
        db.execute(
            "UPDATE archive_rules SET preview_id=?,error=NULL,next_at=? WHERE id=? AND next_at=?",
            (preview, next_time(now, row), row["id"], row["next_at"]),
        )


def next_time(now, row):
    hours = row["interval_hours"]
    return now + hours * 3600 if hours else None
