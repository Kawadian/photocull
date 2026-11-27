from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path
from uuid import uuid4

from photocull.archive import (
    ArchiveError,
    ArchiveRequest,
    PreviewTotals,
    packed,
    policy_key,
    preview_digest,
    preview_rows,
    validate_preview,
)
from photocull.db import connect, write

PAGE_SIZE = 100
BATCH_SIZE = 128
TTL_SECONDS = 7 * 86400


def queue(db, settings, library, request):
    validate_preview(db, settings, library, request)
    key, body = policy_key(settings, library), request.model_dump_json()
    with write(db):
        active = db.execute(
            "SELECT id,request,policy_key FROM archive_previews "
            "WHERE status IN ('pending','running')"
        ).fetchone()
        if active:
            if active["request"] == body and active["policy_key"] == key:
                return active["id"]
            raise ArchiveError("A preview is already being prepared for this library.")
        id_ = uuid4().hex
        db.execute(
            "INSERT INTO archive_previews(id,request,policy_key,created_at) VALUES(?,?,?,?)",
            (id_, body, key, time.time()),
        )
    return id_


def page(db, id_, after=0):
    row = db.execute("SELECT * FROM archive_previews WHERE id=?", (id_,)).fetchone()
    if row is None or (row["finished_at"] and row["finished_at"] < time.time() - TTL_SECONDS):
        raise LookupError("Preview expired or does not exist.")
    result = {
        "id": id_,
        "status": row["status"],
        "confirmation": row["confirmation"],
        "error": row["error"],
        "items": [],
        "skipped": [],
        "next_cursor": None,
    }
    if row["status"] != "ready":
        return result
    rows = db.execute(
        "SELECT ordinal,payload,eligible FROM archive_preview_items "
        "WHERE preview_id=? AND ordinal>? ORDER BY ordinal LIMIT ?",
        (id_, after, PAGE_SIZE + 1),
    ).fetchall()
    for item in rows[:PAGE_SIZE]:
        result["items" if item["eligible"] else "skipped"].append(json.loads(item["payload"]))
    if len(rows) > PAGE_SIZE:
        result["next_cursor"] = rows[PAGE_SIZE - 1]["ordinal"]
    return result | json.loads(row["summary"])


def prune(db):
    # Release expired rule previews and prune completed manifests in short write transactions.
    with write(db):
        db.execute(
            "UPDATE archive_rules SET preview_id=NULL,error='Preview expired. Prepare a new one.' "
            "WHERE preview_id IN (SELECT id FROM archive_previews WHERE finished_at<?)",
            (time.time() - TTL_SECONDS,),
        )
        old = db.execute(
            "SELECT id FROM archive_previews WHERE status IN ('ready','error') AND finished_at<? "
            "AND NOT EXISTS(SELECT 1 FROM archive_jobs WHERE preview_id=archive_previews.id) "
            "ORDER BY finished_at LIMIT 1",
            (time.time() - TTL_SECONDS,),
        ).fetchone()
        if old:
            removed = db.execute(
                "DELETE FROM archive_preview_items WHERE preview_id=? AND ordinal IN ("
                "SELECT ordinal FROM archive_preview_items WHERE preview_id=? LIMIT 2048)",
                (old["id"], old["id"]),
            ).rowcount
            if not removed:
                db.execute("DELETE FROM archive_previews WHERE id=?", (old["id"],))


def build(db_path: Path, settings, library, *, id_=None, stop=None):
    """Claim and construct one manifest. flock lets a restarted worker or CLI resume safely."""
    with db_path.with_suffix(".archive-preview.lock").open("a") as lock:
        flags = fcntl.LOCK_EX if id_ else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(lock, flags)
        except BlockingIOError:
            return
        with connect(db_path) as db:
            row = db.execute(
                "SELECT * FROM archive_previews WHERE status IN ('pending','running') "
                "AND (? IS NULL OR id=?)",
                (id_, id_),
            ).fetchone()
            if not row:
                return
            id_ = row["id"]
            with write(db):
                db.execute("UPDATE archive_previews SET status='running' WHERE id=?", (id_,))
                db.execute("DELETE FROM archive_preview_items WHERE preview_id=?", (id_,))
            try:
                request = ArchiveRequest.model_validate_json(row["request"])
                if policy_key(settings, library) != row["policy_key"]:
                    raise ArchiveError("Archive configuration changed. Create a new preview.")
                validate_preview(db, settings, library, request)
                totals = PreviewTotals()
                digest = preview_digest(request, row["policy_key"])
                batch = []
                with connect(db_path) as reader:
                    # Catalog membership and hashes are from one SQLite snapshot.
                    reader.execute("BEGIN")
                    for ordinal, (eligible, item, size) in enumerate(
                        preview_rows(reader, settings, library, request), 1
                    ):
                        if stop and stop.is_set():
                            return
                        digest.update(packed([eligible, item]).encode() + b"\n")
                        totals.record(item if eligible else None, size, request)
                        batch.append((id_, ordinal, packed(item), eligible))
                        if len(batch) >= BATCH_SIZE:
                            save_batch(db, batch)
                            batch.clear()
                if batch:
                    save_batch(db, batch)
                summary = packed(totals.summary(request))
                digest.update(summary.encode())
                with write(db):
                    db.execute(
                        "UPDATE archive_previews SET status='ready',finished_at=?,summary=?,"
                        "confirmation=? WHERE id=?",
                        (time.time(), summary, digest.hexdigest(), id_),
                    )
            except (OSError, ValueError, RuntimeError) as error:
                with write(db):
                    db.execute(
                        "UPDATE archive_previews SET status='error',finished_at=?,error=? "
                        "WHERE id=?",
                        (time.time(), str(error), id_),
                    )


def save_batch(db, rows):
    with write(db):
        db.executemany("INSERT INTO archive_preview_items VALUES(?,?,?,?)", rows)
