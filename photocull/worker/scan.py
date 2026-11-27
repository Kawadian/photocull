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


def relink_asset(db, entry, digest, prior):
    link = db.execute(
        "SELECT * FROM archive_reindex WHERE path=? AND ready=1", (entry.path,)
    ).fetchone()
    if link is None or link["after_hash"] != digest:
        return prior, False
    source = db.execute(
        "SELECT id,content_hash,rating,rejected FROM assets WHERE id=? AND path=? "
        "AND content_hash=?",
        (link["asset_id"], link["source_path"], link["before_hash"]),
    ).fetchone()
    if source is None:
        return prior, False
    if prior is None:
        db.execute(
            "UPDATE assets SET path=?,folder=?,kind=? WHERE id=?",
            (entry.path, entry.path.rpartition("/")[0], entry.kind, source["id"]),
        )
        return source, True
    if prior["id"] == source["id"]:
        return prior, True
    # The destination has unrelated catalog history. Preserve its row and transfer review.
    db.execute(
        "UPDATE assets SET rating=?,rejected=?,revision=revision+1 WHERE id=?",
        (source["rating"], source["rejected"], prior["id"]),
    )
    return prior, False


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
                "SELECT id,content_hash FROM assets WHERE path = ?", (entry.path,)
            ).fetchone()
            prior, relinked = relink_asset(db, entry, digest, prior)
            size, mtime = entry.stat.st_size, entry.stat.st_mtime_ns
            day = fallback_day(mtime)
            if prior is None:
                asset_id = db.execute(
                    """INSERT INTO assets(path,folder,kind,size,mtime_ns,content_hash,
                                          scan_id,day,source_key)
                       VALUES (?,?,?,?,?,?,?,?,?) RETURNING id""",
                    (
                        entry.path,
                        entry.path.rpartition("/")[0],
                        entry.kind,
                        size,
                        mtime,
                        digest,
                        scan_id,
                        day,
                        key,
                    ),
                ).fetchone()["id"]
            else:
                asset_id = prior["id"]
                # A plain UPDATE lets nested catalog triggers keep their own conflict policies.
                db.execute(
                    """UPDATE assets SET active=1,scan_id=?,size=?,mtime_ns=?,source_key=?,
                         day=CASE WHEN content_hash=? THEN coalesce(substr(captured_at,1,10),?)
                                  ELSE ? END, content_hash=?
                       WHERE id=?""",
                    (scan_id, size, mtime, key, digest, day, day, digest, asset_id),
                )
            rating = db.execute(
                "DELETE FROM archive_rating_transfer WHERE path=? RETURNING *", (entry.path,)
            ).fetchone()
            if rating and rating["content_hash"] == digest:
                db.execute(
                    "UPDATE assets SET rating=?,rejected=?,revision=revision+1 WHERE id=?",
                    (rating["rating"], rating["rejected"], asset_id),
                )
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
                if not relinked:
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
            db.execute("DELETE FROM archive_reindex WHERE path=? AND ready=1", (entry.path,))
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
    # A crash may leave a journal with one path already published. Hold its catalog
    # identity until recovery reports a durable result, including when work is blocked.
    held_paths = {
        path
        for row in db.execute("SELECT path,source_path FROM archive_reindex WHERE ready=0")
        for path in row
    }
    for entry in jail.walk(pulse):
        if entry.path in held_paths:
            continue
        key = source_key(entry.stat)
        prior = db.execute(
            "SELECT source_key, content_hash FROM assets WHERE path = ?", (entry.path,)
        ).fetchone()
        unchanged = prior and prior["source_key"] == key
        try:
            if (
                settle_seconds
                and not unchanged
                and (time.time_ns() - entry.stat.st_ctime_ns < settle_seconds * 1e9)
            ):
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
        "SELECT id FROM assets WHERE active=1 AND id>? AND scan_id<>? "
        "AND NOT EXISTS(SELECT 1 FROM archive_reindex WHERE ready=0 AND asset_id=assets.id) "
        "ORDER BY id LIMIT ?",
        (after, scan_id, settings.batch_size),
    ).fetchall():
        pulse()
        after = rows[-1]["id"]
        with write(db):
            db.executemany("UPDATE assets SET active = 0 WHERE id = ?", rows)
            db.executemany("DELETE FROM jobs WHERE asset_id = ?", rows)
            mark_catalog_updated(db)
        yield
