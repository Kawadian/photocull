from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from photocull.config import STAGES, WorkerSettings
from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.db.jobs import DISABLED, NO_VIDEO_EXIF, NO_VIDEO_SCORE
from photocull.media.walk import MediaEntry, MediaJail, SourceChanged, hash_file, source_key


def fallback_day(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns // 1_000_000_000, UTC).date().isoformat()


def _record(
    db: sqlite3.Connection,
    scan_id: int,
    batch: list[tuple[MediaEntry, str, str]],
    settings: WorkerSettings,
    model_key: str | None = None,
) -> None:
    with write(db):
        for entry, key, digest in batch:
            prior = db.execute(
                "SELECT content_hash FROM assets WHERE path = ?", (entry.path,)
            ).fetchone()
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
            if model_key and entry.kind != "video":
                db.execute(
                    """INSERT INTO jobs (asset_id, stage, content_hash, status, model_key)
                       VALUES (?, 'embedding', ?, 'pending', ?)
                       ON CONFLICT (asset_id, stage) DO UPDATE SET
                           content_hash = excluded.content_hash, model_key = excluded.model_key,
                           status = 'pending', error = NULL, attempts = 0, interruptions = 0
                       WHERE jobs.content_hash <> excluded.content_hash
                          OR jobs.model_key IS NOT excluded.model_key""",
                    (asset_id, digest, model_key),
                )
            if prior and prior["content_hash"] != digest:
                db.execute("DELETE FROM asset_scores WHERE asset_id = ?", (asset_id,))
                db.execute("DELETE FROM burst_choices WHERE asset_id = ?", (asset_id,))
                db.execute(
                    """UPDATE assets SET auto_reject_dismissed = 0, burst_reject_token = NULL
                       WHERE id = ?""",
                    (asset_id,),
                )
            for stage in STAGES:
                reason = None
                if stage == "exif" and entry.kind == "video":
                    reason = NO_VIDEO_EXIF
                elif stage == "score" and entry.kind == "video":
                    reason = NO_VIDEO_SCORE
                elif stage not in settings.stages:
                    reason = DISABLED
                job = db.execute(
                    """
                    INSERT INTO jobs (asset_id, stage, content_hash, status, error)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (asset_id, stage) DO UPDATE SET
                        content_hash = excluded.content_hash, status = excluded.status,
                        error = excluded.error, attempts = 0, interruptions = 0
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
    model_key: str | None = None,
    *,
    settle_seconds: float = 0,
    defer: Callable[[], None] | None = None,
) -> Iterator[None]:
    batch: list[tuple[MediaEntry, str, str]] = []
    for entry in jail.walk(pulse):
        key = source_key(entry.stat)
        prior = db.execute(
            "SELECT source_key, content_hash FROM assets WHERE path = ?", (entry.path,)
        ).fetchone()
        unchanged = prior and prior["source_key"] == key
        try:
            if not unchanged and time.time_ns() - entry.stat.st_ctime_ns < settle_seconds * 1e9:
                raise SourceChanged(entry.path)
            digest = prior["content_hash"] if unchanged else hash_file(jail, entry, pulse)
        except (SourceChanged, FileNotFoundError):
            if defer is None:
                raise
            # An in-flight write must neither replace existing metadata nor retire it.
            db.execute("UPDATE assets SET scan_id = ? WHERE path = ?", (scan_id, entry.path))
            defer()
            continue
        batch.append((entry, key, digest))
        if len(batch) >= settings.batch_size:
            _record(db, scan_id, batch, settings, model_key)
            batch.clear()
            yield
    if batch:
        _record(db, scan_id, batch, settings, model_key)
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
