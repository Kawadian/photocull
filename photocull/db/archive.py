from __future__ import annotations

import json
import time
from uuid import uuid4

from photocull.archive import ArchiveError, packed, policy_key, validate_preview
from photocull.db import write
from photocull.db.archive_preview import TTL_SECONDS


def audit(db, job: str, ordinal: int, actor: str, action: str, details: dict) -> None:
    db.execute(
        "INSERT INTO archive_audit(job_id, ordinal, actor, action, at, details) "
        "VALUES(?,?,?,?,?,?)",
        (job, ordinal, actor, action, time.time(), packed(details)),
    )


def enqueue(db, settings, library, request, confirmation, actor):
    validate_preview(db, settings, library, request)
    row = db.execute(
        "SELECT id,summary FROM archive_previews WHERE confirmation=? AND request=? "
        "AND policy_key=? "
        "AND status='ready' AND finished_at>? ORDER BY finished_at DESC LIMIT 1",
        (
            confirmation,
            request.model_dump_json(),
            policy_key(settings, library),
            time.time() - TTL_SECONDS,
        ),
    ).fetchone()
    if not row or not (summary := json.loads(row["summary"]))["count"]:
        raise ArchiveError("Preview is missing, empty or expired. Run preview again.")
    return insert_job(db, request, summary | {"confirmation": confirmation}, actor, row["id"])


def insert_job(db, request, result, actor, preview_id=None):
    confirmation = result["confirmation"]
    id_ = uuid4().hex
    db.execute("PRAGMA synchronous = FULL")
    with write(db):
        existing = db.execute(
            "SELECT id FROM archive_jobs WHERE confirmation=? AND finished_at IS NULL",
            (confirmation,),
        ).fetchone()
        if existing:
            return existing["id"]
        if db.execute("SELECT 1 FROM archive_jobs WHERE finished_at IS NULL").fetchone():
            raise ArchiveError("This library already has an active archive job.")
        # Read confirmation and attach the manifest within the same writer transaction.
        if (
            preview_id
            and not db.execute(
                "SELECT 1 FROM archive_previews WHERE id=? AND finished_at>?",
                (preview_id, time.time() - TTL_SECONDS),
            ).fetchone()
        ):
            raise ArchiveError("Preview expired. Run preview again.")
        db.execute(
            "INSERT INTO archive_jobs(id,actor,request,confirmation,created_at,preview_id,"
            "manifest_remaining) VALUES(?,?,?,?,?,?,?)",
            (
                id_,
                actor,
                request.model_dump_json(),
                confirmation,
                time.time(),
                preview_id,
                result["count"] if preview_id else 0,
            ),
        )
        if preview_id is None:
            db.executemany(
                "INSERT INTO archive_items(job_id,ordinal,plan) VALUES(?,?,?)",
                [(id_, n, packed(item)) for n, item in enumerate(result["items"], 1)],
            )
        elif request.scrub_copy and result["skipped_count"]:
            db.execute(
                """INSERT INTO archive_items(job_id,ordinal,plan,status,error)
                   SELECT ?,ordinal,json_object('relative',json_extract(payload,'$.path')),
                          'skipped',json_extract(payload,'$.reason')
                   FROM archive_preview_items WHERE preview_id=? AND eligible=0""",
                (id_, preview_id),
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
                "matched_count": result["matched_count"],
                "estimated_source_bytes": result["source_bytes"],
            },
        )
    return id_


def materialize(db, size=128):
    job = db.execute(
        "SELECT * FROM archive_jobs WHERE finished_at IS NULL AND cancelled=0 "
        "AND manifest_remaining>0 AND NOT EXISTS(SELECT 1 FROM archive_items "
        "WHERE job_id=archive_jobs.id AND status IN ('pending','running'))"
    ).fetchone()
    if job is None:
        return
    with write(db):
        if not db.execute(
            "SELECT 1 FROM archive_jobs WHERE id=? AND cancelled=0", (job["id"],)
        ).fetchone():
            return
        rows = db.execute(
            "SELECT ordinal,payload FROM archive_preview_items WHERE preview_id=? AND ordinal>? "
            "AND eligible=1 ORDER BY ordinal LIMIT ?",
            (job["preview_id"], job["manifest_cursor"], size),
        ).fetchall()
        if not rows:
            raise RuntimeError("Archive manifest is incomplete")
        db.executemany(
            "INSERT INTO archive_items(job_id,ordinal,plan) VALUES(?,?,?)",
            [(job["id"], row["ordinal"], row["payload"]) for row in rows],
        )
        db.execute(
            "UPDATE archive_jobs SET manifest_cursor=?,manifest_remaining=manifest_remaining-?,"
            "preview_id=CASE WHEN manifest_remaining=? THEN NULL ELSE preview_id END WHERE id=?",
            (rows[-1]["ordinal"], len(rows), len(rows), job["id"]),
        )


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


def jobs(db, job=None, after=0, limit=100):
    db.execute("BEGIN")
    try:
        return read_jobs(db, job, after, limit)
    finally:
        db.rollback()


def read_jobs(db, job, after, limit):
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
        remaining = row["manifest_remaining"]
        if remaining:
            state = "cancelled" if row["cancelled"] else "pending"
            record["counts"][state] = record["counts"].get(state, 0) + remaining
        if job:
            detail = db.execute(
                "SELECT * FROM archive_items WHERE job_id=? AND ordinal>? ORDER BY ordinal LIMIT ?",
                (job, after, limit + 1),
            ).fetchall()
            record["next_cursor"] = detail[limit - 1]["ordinal"] if len(detail) > limit else None
            record["items"] = [
                {
                    "ordinal": r["ordinal"],
                    "status": r["status"],
                    "phase": r["phase"],
                    "error": r["error"],
                    "path": json.loads(r["plan"])["relative"],
                }
                for r in detail[:limit]
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
    counts = db.execute(
        "SELECT count(DISTINCT t.id),coalesce(sum(json_extract(f.value,'$.size')),0) "
        "FROM archive_trash t JOIN json_each(t.files) f WHERE t.status='available'"
    ).fetchone()
    return {
        "items": items,
        "next_cursor": items[-1]["id"] if len(rows) > limit else None,
        "total_count": counts[0],
        "total_bytes": counts[1],
    }


def audit_page(db, after=0, limit=100):
    rows = db.execute(
        "SELECT * FROM archive_audit WHERE id > ? ORDER BY id LIMIT ?",
        (after, limit + 1),
    ).fetchall()
    items = [dict(row) | {"details": json.loads(row["details"])} for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}
