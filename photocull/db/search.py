from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from photocull.config import MLSettings
from photocull.db import write
from photocull.db.catalog import ASSET_COLUMNS, ASSET_JOINS, asset_json

SEARCH_TIMEOUT = 10
REQUEST_TTL = 120
MAX_PENDING = 16
MAX_ATTEMPTS = 2
HEARTBEAT_TIMEOUT = 10
WORKER_OFFLINE = "The indexing worker is offline. Start it and retry the search."
SEARCH_TIMED_OUT = "Photo search timed out. Try again shortly."


class QueueUnavailable(ValueError):
    pass


class QueueFull(ValueError):
    pass


class SearchTimedOut(ValueError):
    pass


def worker_online(db: sqlite3.Connection, path: Path, now: float) -> bool:
    # The lock works across container PID namespaces and is released even on SIGKILL.
    heartbeat = db.execute("SELECT heartbeat FROM worker_status WHERE id = 1").fetchone()
    if not heartbeat or now - heartbeat[0] >= HEARTBEAT_TIMEOUT:
        return False
    try:
        with path.with_name("worker.lock").open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except OSError:
        pass
    return False


def enqueue(
    db: sqlite3.Connection,
    path: Path,
    settings: MLSettings,
    *,
    query: str | None,
    asset_id: int | None,
    tag: str | None,
    limit: int,
) -> str:
    key = settings.model_key
    request_id = uuid4().hex
    if not worker_online(db, path, time.time()):
        raise QueueUnavailable(WORKER_OFFLINE)
    with write(db):
        now = time.time()
        state = db.execute("SELECT * FROM ml_state").fetchone()
        if not worker_online(db, path, now):
            raise QueueUnavailable(WORKER_OFFLINE)
        if not state["enabled"] or state["model_key"] != key:
            raise QueueUnavailable("Waiting for the worker to configure embeddings")
        digest = None
        if query is not None:
            if not state["search_enabled"]:
                raise QueueUnavailable("Text search is disabled on the worker")
            if state["text_error"]:
                raise QueueUnavailable(state["text_error"])
        if tag and (not state["tags_enabled"] or state["tag_key"] != settings.tag_key):
            raise QueueUnavailable("Waiting for the worker to configure tags")
        if asset_id is not None:
            row = db.execute(
                """SELECT a.content_hash, e.model_key FROM assets AS a
                   LEFT JOIN asset_embeddings AS e ON e.asset_id = a.id
                   WHERE a.id = ? AND a.active = 1""",
                (asset_id,),
            ).fetchone()
            if row is None:
                raise LookupError("Unknown asset")
            if row["model_key"] != key:
                raise QueueUnavailable(
                    state["image_error"] or "This photo is waiting for an embedding"
                )
            digest = row["content_hash"]
        elif (
            state["image_error"]
            and not db.execute(
                "SELECT 1 FROM asset_embeddings WHERE model_key = ? LIMIT 1", (key,)
            ).fetchone()
        ):
            raise QueueUnavailable(state["image_error"])
        db.execute("DELETE FROM search_requests WHERE created_at <= ?", (now - REQUEST_TTL,))
        if db.execute(
            """SELECT 1 FROM search_requests
               WHERE status IN ('pending', 'running') AND created_at > ? LIMIT 1 OFFSET ?""",
            (now - SEARCH_TIMEOUT, MAX_PENDING - 1),
        ).fetchone():
            raise QueueFull("Search is busy. Try again shortly.")
        db.execute(
            """INSERT INTO search_requests
                (id, model_key, query, asset_id, content_hash, tag, tag_key,
                 limit_results, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                request_id,
                key,
                query,
                asset_id,
                digest,
                tag,
                settings.tag_key if tag else None,
                limit,
                now,
            ),
        )
    return request_id


def claim(db: sqlite3.Connection) -> sqlite3.Row | None:
    earliest = time.time() - SEARCH_TIMEOUT
    row = db.execute(
        """SELECT id FROM search_requests
           WHERE status = 'pending' AND created_at > ? AND attempts < ?
           ORDER BY created_at, id LIMIT 1""",
        (earliest, MAX_ATTEMPTS),
    ).fetchone()
    if row is None:
        return None
    with write(db):
        return db.execute(
            """UPDATE search_requests SET status = 'running', attempts = attempts + 1
               WHERE id = ? AND status = 'pending' AND created_at > ? RETURNING *""",
            (row["id"], time.time() - SEARCH_TIMEOUT),
        ).fetchone()


def recover(db: sqlite3.Connection) -> None:
    """Recover only live requests and cap restarts that interrupt the same request."""
    with write(db):
        db.execute(
            """UPDATE search_requests SET
                   status = CASE WHEN attempts < ? THEN 'pending' ELSE 'error' END,
                   error = CASE WHEN attempts >= ?
                       THEN 'Search was interrupted twice. Try again.' END
               WHERE status = 'running' AND created_at > ?""",
            (MAX_ATTEMPTS, MAX_ATTEMPTS, time.time() - SEARCH_TIMEOUT),
        )


def finish(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    results=None,
    error: str | None = None,
    *,
    interrupted: bool = False,
) -> None:
    retry = (
        interrupted
        and row["attempts"] < MAX_ATTEMPTS
        and (time.time() < row["created_at"] + SEARCH_TIMEOUT)
    )
    db.execute(
        """UPDATE search_requests SET status = ?, results = ?, error = ?
           WHERE id = ? AND status = 'running'""",
        (
            "pending" if retry else "error" if error else "done",
            json.dumps(results) if results is not None else None,
            None if retry else error,
            row["id"],
        ),
    )


def cancel(db: sqlite3.Connection, request_id: str) -> None:
    # An already completed request remains available for polling until expiry.
    db.execute(
        "DELETE FROM search_requests WHERE id = ? AND status IN ('pending', 'running')",
        (request_id,),
    )


def expire(db: sqlite3.Connection) -> None:
    cutoff = time.time() - REQUEST_TTL
    if db.execute(
        "SELECT 1 FROM search_requests WHERE created_at <= ? LIMIT 1", (cutoff,)
    ).fetchone():
        with write(db):
            db.execute("DELETE FROM search_requests WHERE created_at <= ?", (cutoff,))


def get_result(
    db: sqlite3.Connection,
    path: Path,
    request_id: str,
    key: str,
    tag_key: str | None,
) -> dict | None:
    now = time.time()
    row = db.execute(
        """SELECT status, results, error, created_at FROM search_requests
           WHERE id = ? AND model_key = ? AND created_at > ?
             AND (tag_key IS NULL OR tag_key = ?)""",
        (request_id, key, now - REQUEST_TTL, tag_key),
    ).fetchone()
    if row is None:
        return None
    if row["status"] in {"pending", "running"}:
        if not worker_online(db, path, now):
            raise QueueUnavailable(WORKER_OFFLINE)
        if now >= row["created_at"] + SEARCH_TIMEOUT:
            raise SearchTimedOut(SEARCH_TIMED_OUT)
    items = []
    matches = json.loads(row["results"]) if row["results"] else []
    if matches:
        ids = [asset_id for asset_id, _, _ in matches]
        assets = {
            asset["id"]: asset_json(asset)
            for asset in db.execute(
                f"""SELECT {ASSET_COLUMNS} {ASSET_JOINS}
                    WHERE a.active = 1 AND a.id IN (SELECT value FROM json_each(?))""",
                (json.dumps(ids),),
            )
        }
        items = [
            {**assets[asset_id], "similarity": score}
            for asset_id, digest, score in matches
            if asset_id in assets and assets[asset_id]["content_hash"] == digest
        ]
    return {"status": row["status"], "items": items, "error": row["error"]}
