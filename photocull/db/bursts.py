from __future__ import annotations

import sqlite3
from uuid import uuid4

from photocull.db import write
from photocull.db.catalog import AUTO_REJECT_REASON, ScoreFilters, mark_catalog_updated

BURST_SELECT = """
    SELECT b.id, a.captured_at, b.recommended_best_id, b.membership_key,
           (SELECT count(*) FROM burst_members AS m WHERE m.burst_id = b.id) AS member_count,
           (SELECT c.asset_id FROM burst_members AS m
            JOIN burst_choices AS c ON c.asset_id = m.asset_id
            WHERE m.burst_id = b.id ORDER BY c.chosen_revision DESC LIMIT 1) AS user_best_id
    FROM bursts AS b JOIN assets AS a ON a.id = b.id
"""


class BurstConflict(ValueError):
    pass


def _burst_json(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["best_id"] = result["user_best_id"] or result["recommended_best_id"]
    return result


def burst_page_query(cursor: int, limit: int, filters: ScoreFilters) -> tuple[str, tuple]:
    clauses, params = filters.clauses()
    where = ""
    if clauses:
        where = f"""AND EXISTS (
            SELECT 1 FROM burst_members AS m JOIN assets AS a ON a.id = m.asset_id
            LEFT JOIN asset_scores AS s ON s.asset_id = a.id
            WHERE m.burst_id = b.id AND {" AND ".join(clauses)}
        )"""
    return (
        f"{BURST_SELECT} WHERE b.id > ? {where} ORDER BY b.id LIMIT ?",
        (cursor, *params, limit + 1),
    )


def list_bursts(db: sqlite3.Connection, cursor: int, limit: int, filters: ScoreFilters) -> dict:
    rows = db.execute(*burst_page_query(cursor, limit, filters)).fetchall()
    return {
        "items": [_burst_json(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


def get_burst(db: sqlite3.Connection, burst_id: int) -> dict:
    row = db.execute(f"{BURST_SELECT} WHERE b.id = ?", (burst_id,)).fetchone()
    if row is None:
        raise LookupError("Unknown burst")
    return _burst_json(row)


def _check_membership(burst: dict, membership_key: str) -> None:
    if burst["membership_key"] != membership_key:
        raise BurstConflict("Burst membership changed. Refresh the burst and try again.")


def choose_best(db: sqlite3.Connection, burst_id: int, asset_id: int, membership_key: str) -> dict:
    with write(db):
        burst = get_burst(db, burst_id)
        _check_membership(burst, membership_key)
        if not db.execute(
            "SELECT 1 FROM burst_members WHERE burst_id = ? AND asset_id = ?", (burst_id, asset_id)
        ).fetchone():
            raise ValueError("Best photo must belong to the burst")
        if burst["user_best_id"] != asset_id:
            db.execute(
                """DELETE FROM burst_choices WHERE asset_id IN
                   (SELECT asset_id FROM burst_members WHERE burst_id = ?)""",
                (burst_id,),
            )
            mark_catalog_updated(db)
            db.execute(
                "INSERT INTO burst_choices SELECT ?, revision FROM catalog_status WHERE id = 1",
                (asset_id,),
            )
        return get_burst(db, burst_id)


def reject_burst_rest(
    db: sqlite3.Connection, burst_id: int, best_id: int, membership_key: str
) -> dict:
    with write(db):
        burst = get_burst(db, burst_id)
        _check_membership(burst, membership_key)
        if burst["best_id"] != best_id:
            raise BurstConflict("Best photo changed. Refresh the burst and try again.")
        token = uuid4().hex
        count = db.execute(
            f"""UPDATE assets SET burst_reject_token = ? WHERE id IN (
                    SELECT a.id FROM burst_members AS m JOIN assets AS a ON a.id = m.asset_id
                    LEFT JOIN asset_scores AS s ON s.asset_id = a.id
                    WHERE m.burst_id = ? AND a.id <> ? AND ({AUTO_REJECT_REASON}) IS NULL
                )""",
            (token, burst_id, best_id),
        ).rowcount
        if count:
            mark_catalog_updated(db)
        return {"changed": count, "undo_token": token if count else None}


def undo_burst_reject(db: sqlite3.Connection, token: str) -> int:
    with write(db):
        count = db.execute(
            "UPDATE assets SET burst_reject_token = NULL WHERE burst_reject_token = ?", (token,)
        ).rowcount
        if count:
            mark_catalog_updated(db)
        return count
