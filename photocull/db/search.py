from __future__ import annotations

import json
import sqlite3
import time
from uuid import uuid4

from photocull.config import MLSettings
from photocull.db import write
from photocull.db.catalog import ASSET_COLUMNS, ASSET_JOINS, asset_json

REQUEST_TTL = 120
MAX_PENDING = 16


class QueueUnavailable(ValueError):
    pass


class QueueFull(ValueError):
    pass


def enqueue(
    db: sqlite3.Connection,
    settings: MLSettings,
    *,
    query: str | None,
    asset_id: int | None,
    tag: str | None,
    limit: int,
) -> str:
    now = time.time()
    key = settings.model_key
    request_id = uuid4().hex
    with write(db):
        state = db.execute("SELECT * FROM ml_state").fetchone()
        worker = db.execute("SELECT heartbeat FROM worker_status WHERE id = 1").fetchone()
        if not worker or now - worker[0] >= 30:
            raise QueueUnavailable("The indexing worker is offline")
        if not state["enabled"] or state["model_key"] != key:
            raise QueueUnavailable("Waiting for the worker to configure embeddings")
        if error := state["image_error"] or (state["text_error"] if query else None):
            raise QueueUnavailable(error)
        if query and not state["search_enabled"]:
            raise QueueUnavailable("Text search is disabled on the worker")
        if tag and (not state["tags_enabled"] or state["tag_key"] != settings.tag_key):
            raise QueueUnavailable("Waiting for the worker to configure tags")
        digest = None
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
                raise QueueUnavailable("This photo is waiting for an embedding")
            digest = row["content_hash"]
        db.execute("DELETE FROM search_requests WHERE created_at <= ?", (now - REQUEST_TTL,))
        if db.execute(
            """SELECT 1 FROM search_requests WHERE status IN ('pending', 'running')
               LIMIT 1 OFFSET ?""",
            (MAX_PENDING - 1,),
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
    earliest = time.time() - REQUEST_TTL
    sql = """SELECT id FROM search_requests WHERE status = 'pending' AND created_at > ?
             ORDER BY created_at, id LIMIT 1"""
    if db.execute(sql, (earliest,)).fetchone() is None:
        return None
    with write(db):
        return db.execute(
            f"UPDATE search_requests SET status = 'running' WHERE id = ({sql}) RETURNING *",
            (earliest,),
        ).fetchone()


def expire(db: sqlite3.Connection) -> None:
    cutoff = time.time() - REQUEST_TTL
    if db.execute(
        "SELECT 1 FROM search_requests WHERE created_at <= ? LIMIT 1", (cutoff,)
    ).fetchone():
        with write(db):
            db.execute("DELETE FROM search_requests WHERE created_at <= ?", (cutoff,))


def get_result(
    db: sqlite3.Connection,
    request_id: str,
    key: str,
    tag_key: str | None,
) -> dict | None:
    row = db.execute(
        """SELECT status, results, error FROM search_requests
           WHERE id = ? AND model_key = ? AND created_at > ?
             AND (tag_key IS NULL OR tag_key = ?)""",
        (request_id, key, time.time() - REQUEST_TTL, tag_key),
    ).fetchone()
    if row is None:
        return None
    items = []
    if row["results"]:
        matches = json.loads(row["results"])
        if matches:
            ids = [asset_id for asset_id, _, _ in matches]
            assets = {
                asset["id"]: asset_json(asset)
                for asset in db.execute(
                    f"""SELECT {ASSET_COLUMNS} {ASSET_JOINS}
                        WHERE a.active = 1 AND a.id IN ({",".join("?" for _ in ids)})""",
                    ids,
                )
            }
            items = [
                {**assets[asset_id], "similarity": score}
                for asset_id, digest, score in matches
                if asset_id in assets and assets[asset_id]["content_hash"] == digest
            ]
    return {"status": row["status"], "items": items, "error": row["error"]}
