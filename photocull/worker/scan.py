from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from photocull.config import WorkerSettings
from photocull.db import write
from photocull.db.jobs import DISABLED, NO_VIDEO_EXIF, STAGES
from photocull.media.walk import MediaEntry, MediaJail, hash_file, source_key


def fallback_day(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns // 1_000_000_000, UTC).date().isoformat()


def mark_catalog_updated(db: sqlite3.Connection) -> None:
    db.execute("UPDATE catalog_status SET revision = revision + 1 WHERE id = 1")


def _record(
    db: sqlite3.Connection,
    scan_id: int,
    batch: list[tuple[MediaEntry, str, str]],
    settings: WorkerSettings,
) -> None:
    with write(db):
        for entry, key, digest in batch:
            asset = db.execute(
                """
                INSERT INTO assets (path, folder, kind, size, mtime_ns, content_hash, scan_id,
                                    day, source_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (path) DO UPDATE SET
                    active = 1, scan_id = excluded.scan_id, size = excluded.size,
                    mtime_ns = excluded.mtime_ns, source_key = excluded.source_key,
                    day = CASE WHEN assets.content_hash = excluded.content_hash
                               THEN COALESCE(substr(assets.captured_at, 1, 10), excluded.day)
                               ELSE excluded.day END,
                    content_hash = excluded.content_hash
                RETURNING id
                """,
                (
                    entry.path,
                    entry.path.rpartition("/")[0],
                    entry.kind,
                    entry.stat.st_size,
                    entry.stat.st_mtime_ns,
                    digest,
                    scan_id,
                    fallback_day(entry.stat.st_mtime_ns),
                    key,
                ),
            ).fetchone()
            asset_id = asset["id"]
            for stage in STAGES:
                reason = None
                if stage == "exif" and entry.kind == "video":
                    reason = NO_VIDEO_EXIF
                elif stage not in settings.stages:
                    reason = DISABLED
                job = db.execute(
                    """
                    INSERT INTO jobs (asset_id, stage, content_hash, status, error)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (asset_id, stage) DO UPDATE SET
                        content_hash = excluded.content_hash, status = excluded.status,
                        error = excluded.error, attempts = 0
                    WHERE jobs.content_hash <> excluded.content_hash
                    RETURNING asset_id
                    """,
                    (asset_id, stage, digest, "skipped" if reason else "pending", reason),
                ).fetchone()
                if job and stage == "exif":
                    db.execute(
                        """UPDATE assets SET captured_at = NULL, make = NULL, model = NULL,
                           latitude = NULL, longitude = NULL, metadata_error = NULL,
                           day = ? WHERE id = ?""",
                        (fallback_day(entry.stat.st_mtime_ns), asset_id),
                    )
                if job and stage == "preview":
                    db.execute(
                        "UPDATE assets SET width = NULL, height = NULL, duration = NULL"
                        " WHERE id = ?",
                        (asset_id,),
                    )
        mark_catalog_updated(db)


def scan_batches(
    db: sqlite3.Connection,
    scan_id: int,
    jail: MediaJail,
    settings: WorkerSettings,
    pulse: Callable[[], None],
) -> Iterator[None]:
    batch: list[tuple[MediaEntry, str, str]] = []
    for entry in jail.walk(pulse):
        key = source_key(entry.stat)
        prior = db.execute(
            "SELECT source_key, content_hash FROM assets WHERE path = ?", (entry.path,)
        ).fetchone()
        digest = (
            prior["content_hash"]
            if prior and prior["source_key"] == key
            else hash_file(jail, entry, pulse)
        )
        batch.append((entry, key, digest))
        if len(batch) >= settings.batch_size:
            _record(db, scan_id, batch, settings)
            batch.clear()
            yield
    if batch:
        _record(db, scan_id, batch, settings)
        yield

    # Only retire absent assets after a complete walk. Keep their ratings if they return.
    after = 0
    while rows := db.execute(
        "SELECT id FROM assets WHERE active = 1 AND id > ? AND scan_id <> ? ORDER BY id LIMIT ?",
        (after, scan_id, settings.batch_size),
    ).fetchall():
        pulse()
        after = rows[-1]["id"]
        with write(db):
            db.executemany("UPDATE assets SET active = 0 WHERE id = ?", rows)
            db.executemany("DELETE FROM jobs WHERE asset_id = ?", rows)
            mark_catalog_updated(db)
        yield
