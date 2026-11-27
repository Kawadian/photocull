from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from photocull.db import write
from photocull.media.derivatives import Variant

LIVE_LIMIT = 512
RETRY_SECONDS = 60 * 5


@dataclass(frozen=True)
class DerivativeJob:
    asset_id: int
    path: str
    kind: str
    source_key: str
    version: str
    variant: str
    priority: int

    @property
    def key(self) -> tuple[int, str, str]:
        return self.asset_id, self.variant, self.version


def cache_status(db: sqlite3.Connection, asset_id: int, variant: str, version: str):
    return db.execute(
        """SELECT status, priority, last_access FROM derivative_files
           WHERE asset_id = ? AND variant = ? AND version = ?""",
        (asset_id, variant, version),
    ).fetchone()


def request_file(
    db: sqlite3.Connection,
    asset_id: int,
    variant: Variant,
    version: str,
    now: float,
    *,
    priority: int = 1,
) -> str:
    with write(db):
        key = (asset_id, variant.name, version)
        if row := cache_status(db, *key):
            if row["status"] in {"running", "pending"}:
                if row["status"] == "pending" and priority > row["priority"]:
                    db.execute(
                        """UPDATE derivative_files SET priority = ?, requested_at = ?
                           WHERE asset_id = ? AND variant = ? AND version = ?""",
                        (priority, now, *key),
                    )
                return "pending"
            if row["status"] == "error" and now - row["last_access"] < RETRY_SECONDS:
                return "error"
        queued = db.execute(
            "SELECT count(*) FROM derivative_files WHERE status = 'pending'"
        ).fetchone()[0]
        if queued >= LIVE_LIMIT:
            return "busy"
        db.execute(
            """INSERT INTO derivative_files
               (asset_id, variant, version, tier, status, priority, requested_at, last_access)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
               ON CONFLICT (asset_id, variant, version) DO UPDATE SET
                   status = 'pending', priority = excluded.priority,
                   requested_at = excluded.requested_at, last_access = excluded.last_access,
                   byte_size = 0, attempts = 0""",
            (*key, variant.tier, priority, now, now),
        )
        return "pending"


def claim_files(db: sqlite3.Connection, recipe: str, priority: int) -> list[DerivativeJob]:
    sql = """
        SELECT f.asset_id, a.path, a.kind, a.source_key, f.version, f.variant, f.priority
        FROM derivative_files AS f JOIN assets AS a ON a.id = f.asset_id
        WHERE f.status = 'pending' AND f.priority = ? AND a.active = 1
          AND f.version = a.content_hash || '-' || ?
        ORDER BY f.requested_at
    """
    if db.execute(sql + "LIMIT 1", (priority, recipe)).fetchone() is None:
        return []
    with write(db):
        first = db.execute(sql + "LIMIT 1", (priority, recipe)).fetchone()
        if first is None:
            return []
        rows = [first]
        if not first["variant"].endswith(".mp4"):
            rows.extend(
                db.execute(
                    """SELECT f.asset_id, a.path, a.kind, a.source_key, f.version,
                              f.variant, f.priority
                       FROM derivative_files AS f JOIN assets AS a ON a.id = f.asset_id
                       WHERE f.asset_id = ? AND f.version = ? AND f.status = 'pending'
                         AND f.variant <> ? AND f.variant NOT LIKE 'video-%'
                       ORDER BY f.priority DESC, f.requested_at LIMIT 3""",
                    (first["asset_id"], first["version"], first["variant"]),
                )
            )
        jobs = [DerivativeJob(**dict(row)) for row in rows]
        db.executemany(
            """UPDATE derivative_files SET status = 'running', attempts = attempts + 1
               WHERE asset_id = ? AND variant = ? AND version = ?""",
            (job.key for job in jobs),
        )
        return jobs


def recover_files(db: sqlite3.Connection) -> list[sqlite3.Row]:
    with write(db):
        interrupted = db.execute("""
            SELECT asset_id, variant, version FROM derivative_files WHERE status = 'running'
        """).fetchall()
        db.execute("""
            UPDATE derivative_files
            SET status = CASE WHEN attempts >= 3 THEN 'error' ELSE 'pending' END
            WHERE status = 'running'
        """)
    return interrupted


def discard_stale_requests(db: sqlite3.Connection, recipe: str) -> None:
    with write(db):
        db.execute(
            """DELETE FROM derivative_files
               WHERE status = 'pending' AND NOT EXISTS (
                   SELECT 1 FROM assets
                   WHERE id = asset_id AND active = 1 AND version = content_hash || '-' || ?
               )""",
            (recipe,),
        )


def finish_files(
    db: sqlite3.Connection,
    jobs: list[DerivativeJob],
    sizes: dict[str, int],
    blurhash: str | None,
    now: float,
) -> None:
    with write(db):
        db.executemany(
            """UPDATE derivative_files SET status = ?, byte_size = ?, last_access = ?
               WHERE asset_id = ? AND variant = ? AND version = ? AND status = 'running'""",
            (
                (
                    "ready" if job.variant in sizes else "error",
                    sizes.get(job.variant, 0),
                    now,
                    *job.key,
                )
                for job in jobs
            ),
        )
        if blurhash:
            db.execute(
                """INSERT INTO derivative_placeholders(asset_id, version, blurhash)
                   VALUES (?, ?, ?) ON CONFLICT (asset_id) DO UPDATE SET
                       version = excluded.version, blurhash = excluded.blurhash""",
                (jobs[0].asset_id, jobs[0].version, blurhash),
            )


def touch_files(db: sqlite3.Connection, hits: list[tuple[float, int, str, str]]) -> None:
    with write(db):
        db.executemany(
            """UPDATE derivative_files SET last_access = max(last_access, ?), hits = hits + 1
               WHERE asset_id = ? AND variant = ? AND version = ? AND status = 'ready'""",
            hits,
        )
