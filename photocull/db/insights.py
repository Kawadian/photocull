from __future__ import annotations

import json
from sqlite3 import Connection

from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.insights import ModelIdentity


def register(db: Connection, model: ModelIdentity) -> str:
    key = model.key
    db.execute(
        "INSERT OR IGNORE INTO insight_models VALUES (?, ?, ?)",
        (key, model.model_id, model.revision),
    )
    return key


def configure_stage(db: Connection, stage: str, key: str | None) -> None:
    row = db.execute("SELECT key FROM insight_stages WHERE stage = ?", (stage,)).fetchone()
    if (row[0] if row else None) == key:
        return
    db.execute("DELETE FROM insight_stages WHERE stage = ?", (stage,))
    if key is not None:
        db.execute("INSERT INTO insight_stages VALUES (?, ?)", (stage, key))
        db.execute(
            """INSERT INTO insight_work(stage, asset_id)
               SELECT ?, id FROM assets WHERE active = 1 AND (kind = 'video') = ?""",
            (stage, stage in {"video", "transcript"}),
        )


def stage_key(db: Connection, stage: str) -> str | None:
    row = db.execute("SELECT key FROM insight_stages WHERE stage = ?", (stage,)).fetchone()
    return row[0] if row else None


def finish(db: Connection, stage: str, key: str, asset, *, score=None, payload=None, error=None):
    # Delete and insert so downstream invalidation also applies to explicit retries.
    db.execute("DELETE FROM insight_results WHERE stage = ? AND asset_id = ?", (stage, asset["id"]))
    db.execute(
        "INSERT INTO insight_results VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            stage,
            asset["id"],
            key,
            asset["content_hash"],
            score,
            json.dumps(payload, allow_nan=False) if payload is not None else None,
            error,
        ),
    )
    db.execute("DELETE FROM insight_work WHERE stage = ? AND asset_id = ?", (stage, asset["id"]))
    mark_catalog_updated(db)


def discard(db: Connection, stage: str, asset_id: int) -> None:
    db.execute("DELETE FROM insight_work WHERE stage = ? AND asset_id = ?", (stage, asset_id))


def queue(db: Connection, stage: str, asset_id: int) -> bool:
    with write(db):
        if not db.execute(
            "SELECT 1 FROM assets WHERE id = ? AND active = 1", (asset_id,)
        ).fetchone():
            return False
        db.execute(
            """INSERT INTO insight_work(stage, asset_id) VALUES (?, ?)
               ON CONFLICT(stage, asset_id) DO UPDATE SET attempts = 0""",
            (stage, asset_id),
        )
        return True


def public_keys(db: Connection, settings) -> dict[str, str]:
    from photocull.insights import models, taste_input

    keys = {stage: identity.key for stage, identity in models(settings).items()}
    if settings.insights.taste and settings.ml.enabled:
        row = db.execute(
            "SELECT model_key FROM taste_state WHERE input_key = ?", (taste_input(settings),)
        ).fetchone()
        if row and row[0]:
            keys["taste"] = row[0]
    return keys


def asset_insights(db: Connection, keys: dict, assets: list[dict]) -> None:
    if not keys or not assets:
        return
    by_id = {asset["id"]: asset for asset in assets}
    for row in db.execute(
        """SELECT r.asset_id, r.stage, r.score, r.payload, r.error
           FROM json_each(?) AS ids CROSS JOIN json_each(?) AS k
           CROSS JOIN insight_results AS r ON r.stage = k.key AND r.asset_id = ids.value
           JOIN assets AS a ON a.id = r.asset_id AND r.content_hash = a.content_hash
           WHERE r.key = k.value""",
        (json.dumps(list(by_id)), json.dumps(keys)),
    ):
        stage = row["stage"]
        data = {"score": row["score"], "error": row["error"]}
        if stage in {"aesthetic", "taste"}:
            data["factors"] = json.loads(row["payload"]) if row["payload"] else None
        by_id[row["asset_id"]].setdefault("insights", {})[stage] = data
