from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from photocull.config import STAGES, Stage
from photocull.db import write
from photocull.db.catalog import mark_catalog_updated

DISABLED = "Stage disabled by worker settings"
NO_VIDEO_EXIF = "Video has no image EXIF stage"
NO_VIDEO_SCORE = "Video has no image score stage"
NO_PREVIEW = "Score requires a completed preview"
NO_EMBEDDING_PREVIEW = "Embedding requires a completed preview"
MODEL_FAILURE = "Embedding model unavailable: "


@dataclass(frozen=True)
class Job:
    asset_id: int
    stage: Stage
    content_hash: str
    path: str
    kind: str
    source_key: str
    mtime_ns: int


def request_scan(db: sqlite3.Connection) -> sqlite3.Row:
    with write(db):
        db.execute(
            "INSERT OR IGNORE INTO scan_requests (status, created_at) VALUES ('pending', ?)",
            (time.time(),),
        )
        return db.execute(
            "SELECT * FROM scan_requests WHERE status IN ('pending', 'running')"
        ).fetchone()


def claim_scan(db: sqlite3.Connection) -> int | None:
    if db.execute("SELECT 1 FROM scan_requests WHERE status = 'pending'").fetchone() is None:
        return None
    with write(db):
        row = db.execute(
            "UPDATE scan_requests SET status = 'running' WHERE status = 'pending' RETURNING id"
        ).fetchone()
    return row["id"] if row else None


def recover(db: sqlite3.Connection) -> None:
    with write(db):
        db.execute("UPDATE scan_requests SET status = 'pending' WHERE status = 'running'")
        db.execute("""
            UPDATE jobs SET
                interruptions = interruptions + 1,
                status = CASE WHEN interruptions >= 2 THEN 'error' ELSE 'pending' END,
                error = CASE WHEN interruptions >= 2
                    THEN 'Worker interrupted this stage three times. Check logs and retry.'
                    ELSE NULL END
            WHERE status = 'running'
            """)


def finish_scan(db: sqlite3.Connection, scan_id: int, error: str | None = None) -> None:
    with write(db):
        db.execute(
            "UPDATE scan_requests SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            ("error" if error else "done", time.time(), error, scan_id),
        )


def claim_jobs(db: sqlite3.Connection, limit: int, *, embedding: bool = False) -> list[Job]:
    stage = "j.stage = 'embedding'" if embedding else "j.stage <> 'embedding'"
    sql = f"""
        SELECT j.asset_id, j.stage, j.content_hash, a.path, a.kind, a.source_key, a.mtime_ns
        FROM jobs AS j JOIN assets AS a ON a.id = j.asset_id
        LEFT JOIN jobs AS p ON p.asset_id = j.asset_id AND p.stage = 'preview'
        WHERE j.status = 'pending' AND a.active = 1
          AND (j.stage NOT IN ('score', 'embedding') OR p.status = 'done')
          AND {stage}
        ORDER BY j.asset_id, j.stage LIMIT ?
        """
    if db.execute(sql, (1,)).fetchone() is None:
        return []
    with write(db):
        jobs = [Job(**dict(row)) for row in db.execute(sql, (limit,))]
        db.executemany(
            """UPDATE jobs SET status = 'running', attempts = attempts + 1
               WHERE asset_id = ? AND stage = ?""",
            [(job.asset_id, job.stage) for job in jobs],
        )
    return jobs


def skip_unavailable_previews(db: sqlite3.Connection) -> bool:
    rows = db.execute(
        """SELECT j.asset_id, j.stage FROM jobs AS j
           LEFT JOIN jobs AS p ON p.asset_id = j.asset_id AND p.stage = 'preview'
           WHERE j.stage IN ('score', 'embedding') AND j.status = 'pending'
             AND (p.status IS NULL OR p.status IN ('error', 'skipped'))
           LIMIT 256"""
    ).fetchall()
    if not rows:
        return False
    with write(db):
        db.executemany(
            """UPDATE jobs SET status = 'skipped', error = ?
               WHERE asset_id = ? AND stage = ? AND status = 'pending'
                 AND NOT EXISTS (SELECT 1 FROM jobs AS p WHERE p.asset_id = jobs.asset_id
                     AND p.stage = 'preview' AND p.status IN ('pending', 'running', 'done'))""",
            (
                (NO_PREVIEW if stage == "score" else NO_EMBEDDING_PREVIEW, asset_id, stage)
                for asset_id, stage in rows
            ),
        )
        mark_catalog_updated(db)
    return True


def retry_job(db: sqlite3.Connection, asset_id: int, stage: Stage) -> sqlite3.Row | None:
    with write(db):
        job = db.execute(
            """SELECT j.* FROM jobs AS j JOIN assets AS a ON a.id = j.asset_id
               WHERE j.asset_id = ? AND j.stage = ? AND a.active = 1""",
            (asset_id, stage),
        ).fetchone()
        if job and job["status"] in {"done", "error"}:
            job = db.execute(
                """UPDATE jobs SET status = 'pending', error = NULL, interruptions = 0
                   WHERE asset_id = ? AND stage = ? RETURNING *""",
                (asset_id, stage),
            ).fetchone()
            if stage == "embedding" and job is not None:
                db.execute("UPDATE ml_state SET image_error = NULL")
        return job


def progress(db: sqlite3.Connection) -> dict:
    # Keep these reads on the same snapshot while the worker commits.
    db.execute("BEGIN")
    try:
        scan = db.execute("SELECT * FROM scan_requests ORDER BY id DESC LIMIT 1").fetchone()
        counts: dict[str, dict[str, int]] = {stage: {} for stage in STAGES}
        for row in db.execute("SELECT stage, status, count FROM stage_counts WHERE count > 0"):
            counts.setdefault(row["stage"], {})[row["status"]] = row["count"]
        worker = db.execute("SELECT heartbeat, pid FROM worker_status WHERE id = 1").fetchone()
        activity = db.execute("SELECT revision FROM catalog_status WHERE id = 1").fetchone()[
            "revision"
        ]
        ml = db.execute("SELECT * FROM ml_state").fetchone()
        return {
            "scan": dict(scan) if scan else None,
            "stages": counts,
            "worker": dict(worker) if worker else None,
            "catalog_revision": activity,
            "ml": {
                "enabled": bool(ml["enabled"]),
                "search_enabled": bool(ml["search_enabled"]),
                "tags_enabled": bool(ml["tags_enabled"]),
                "model_key": ml["model_key"],
                "image_error": ml["image_error"],
                "text_error": ml["text_error"],
                "tag_error": ml["tag_error"],
                "group_error": ml["group_error"],
                "grouping": bool(ml["enabled"] and ml["grouped_generation"] != ml["generation"]),
            },
        }
    finally:
        db.rollback()
