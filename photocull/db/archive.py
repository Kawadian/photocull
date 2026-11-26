from __future__ import annotations

import json
import time
from uuid import uuid4

from photocull.archive import ArchiveError, ArchiveRequest, packed, preview, validate_capability
from photocull.db import write


def audit(db, job: str, ordinal: int, actor: str, action: str, details: dict) -> None:
    db.execute(
        "INSERT INTO archive_audit(job_id, ordinal, actor, action, at, details) "
        "VALUES(?,?,?,?,?,?)",
        (job, ordinal, actor, action, time.time(), packed(details)),
    )


def enqueue(db, settings, library: str, request: ArchiveRequest, confirmation: str, actor: str):
    validate_capability(settings, library, request)
    result = preview(db, settings, library, request)
    if result["confirmation"] != confirmation or not result["items"]:
        raise ArchiveError("Preview is empty or changed. Run preview again.")
    return insert_job(db, request, result, actor)


def insert_job(db, request, result, actor):
    confirmation = result["confirmation"]
    id_ = uuid4().hex
    db.execute("PRAGMA synchronous = FULL")
    with write(db):
        existing = db.execute(
            "SELECT id FROM archive_jobs WHERE confirmation = ? AND finished_at IS NULL",
            (confirmation,),
        ).fetchone()
        if existing:
            return existing["id"]
        if db.execute("SELECT 1 FROM archive_jobs WHERE finished_at IS NULL").fetchone():
            raise ArchiveError("This library already has an active archive job.")
        db.execute(
            "INSERT INTO archive_jobs(id,actor,request,confirmation,created_at) VALUES(?,?,?,?,?)",
            (id_, actor, request.model_dump_json(), confirmation, time.time()),
        )
        db.executemany(
            "INSERT INTO archive_items(job_id,ordinal,plan) VALUES(?,?,?)",
            [(id_, n, packed(item)) for n, item in enumerate(result["items"], 1)],
        )
        audit(
            db,
            id_,
            0,
            actor,
            "confirmed",
            {
                "request": request.model_dump(mode="json"),
                "confirmation": confirmation,
                "count": result["count"],
                "estimated_source_bytes": result["source_bytes"],
            },
        )
    return id_


def cancel(db, job: str, actor: str):
    db.execute("PRAGMA synchronous = FULL")
    with write(db):
        result = db.execute(
            "UPDATE archive_jobs SET cancelled = 1 WHERE id = ? AND finished_at IS NULL "
            "AND cancelled = 0 RETURNING id",
            (job,),
        ).fetchone()
        if result:
            audit(db, job, 0, actor, "cancel_requested", {})
        exists = db.execute("SELECT 1 FROM archive_jobs WHERE id = ?", (job,)).fetchone()
        if not exists:
            raise LookupError("Unknown archive job")


def retry_recovery(db, job: str, actor: str):
    db.execute("PRAGMA synchronous = FULL")
    with write(db):
        row = db.execute("SELECT finished_at FROM archive_jobs WHERE id = ?", (job,)).fetchone()
        if row is None:
            raise LookupError("Unknown archive job")
        if row["finished_at"] is None:
            raise ArchiveError("Wait for the active job to finish")
        if db.execute("SELECT 1 FROM archive_jobs WHERE finished_at IS NULL").fetchone():
            raise ArchiveError("This library already has an active archive job.")
        if not db.execute(
            "UPDATE archive_items SET status = 'pending',error = NULL "
            "WHERE job_id = ? AND status = 'recovery'",
            (job,),
        ).rowcount:
            raise ArchiveError("This job has no blocked recovery items")
        db.execute("UPDATE archive_jobs SET finished_at = NULL WHERE id = ?", (job,))
        audit(db, job, 0, actor, "recovery_requested", {})


def jobs(db, job=None):
    if job:
        rows = db.execute("SELECT * FROM archive_jobs WHERE id = ?", (job,)).fetchall()
        if not rows:
            raise LookupError("Unknown archive job")
    else:
        rows = db.execute("SELECT * FROM archive_jobs ORDER BY created_at DESC LIMIT 25").fetchall()
    results = []
    for row in rows:
        record = dict(row)
        record["request"] = json.loads(record["request"])
        record["counts"] = dict(
            db.execute(
                "SELECT status,count(*) FROM archive_items WHERE job_id = ? GROUP BY status",
                (row["id"],),
            ).fetchall()
        )
        if job:
            record["items"] = [
                {
                    "ordinal": r["ordinal"],
                    "status": r["status"],
                    "phase": r["phase"],
                    "error": r["error"],
                    "path": json.loads(r["plan"])["relative"],
                }
                for r in db.execute(
                    "SELECT * FROM archive_items WHERE job_id = ? ORDER BY ordinal", (job,)
                )
            ]
        results.append(record)
    return results[0] if job else results


def trash(db, *, before=None, limit=100):
    rows = db.execute(
        "SELECT * FROM archive_trash WHERE status = 'available' AND (? IS NULL OR "
        "(created_at,id) < (SELECT created_at,id FROM archive_trash WHERE id=?)) "
        "ORDER BY created_at DESC,id DESC LIMIT ?",
        (before, before, limit + 1),
    ).fetchall()
    items = []
    for row in rows[:limit]:
        record = dict(row)
        record["files"] = json.loads(record["files"])
        record["renamed_to"] = bool(record["renamed_to"])
        items.append(record)
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}


def audit_page(db, after=0, limit=100):
    rows = db.execute(
        "SELECT * FROM archive_audit WHERE id > ? ORDER BY id LIMIT ?",
        (after, limit + 1),
    ).fetchall()
    items = [dict(row) | {"details": json.loads(row["details"])} for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}
