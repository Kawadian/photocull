from __future__ import annotations

import json


def days(db, after: str, limit: int):
    return db.execute(
        """SELECT d.day, d.count, d.segments FROM story_days AS d JOIN story_state AS s USING(key)
           WHERE d.day < ? ORDER BY d.day DESC LIMIT ?""",
        (after, limit),
    ).fetchall()


def segments(db, day: str, after: int, limit: int):
    rows = db.execute(
        """SELECT ss.* FROM story_segments AS ss JOIN story_days AS d USING(day)
           JOIN story_state AS s USING(key)
           WHERE ss.day = ? AND ss.position > ? ORDER BY ss.position LIMIT ?""",
        (day, after, limit),
    ).fetchall()
    items = [dict(row) for row in rows]
    ids = {asset for item in items for asset in json.loads(item["representatives"])}
    by_id = {
        row["id"]: dict(row)
        for row in db.execute(
            """SELECT id, path, content_hash FROM assets WHERE active = 1
               AND id IN (SELECT value FROM json_each(?))""",
            (json.dumps(list(ids)),),
        )
    }
    for item in items:
        item["representatives"] = [
            by_id[asset] for asset in json.loads(item["representatives"]) if asset in by_id
        ]
    return items
