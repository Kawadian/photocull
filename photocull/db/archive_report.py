from __future__ import annotations

from pathlib import PurePosixPath

STORAGE_DIMENSIONS = {
    "year": "substr(coalesce(v.captured_at,a.day),1,4)",
    "folder": "a.folder",
    "format": "archive_suffix(a.path)",
    "camera": "trim(coalesce(v.make,a.make,'') || ' ' || coalesce(v.model,a.model,''))",
}
# Same rough assumptions the preview uses. Figures refer to future replacement and purge.
SAVINGS = "CASE a.kind WHEN 'raw' THEN 0.25 WHEN 'video' THEN 0.35 ELSE 0.45 END"


def storage(db, by):
    dimension = STORAGE_DIMENSIONS[by]
    if by == "format":
        # Extension extraction avoids a dependency on SQLite extensions.
        db.create_function(
            "archive_suffix", 1, lambda s: PurePosixPath(s).suffix.lower(), deterministic=True
        )
    metrics = (
        "count(*) AS count, sum(a.size) AS bytes, "
        f"cast(sum(a.size * ({SAVINGS})) AS int) AS estimated_bytes"
    )
    total = dict(db.execute(f"SELECT {metrics} FROM assets a WHERE active=1").fetchone())
    rows = db.execute(
        f"SELECT {dimension} AS dimension, {metrics} FROM assets a "
        "LEFT JOIN archive_video v ON v.asset_id=a.id AND v.content_hash=a.content_hash "
        "WHERE a.active=1 "
        "GROUP BY dimension ORDER BY bytes DESC,dimension LIMIT 101",
    ).fetchall()
    return {
        "total": total,
        "items": [dict(row) for row in rows[:100]],
        "truncated": len(rows) > 100,
    }


def duplicates(db, by, after, limit=20):
    jpeg = "(lower(a.path) GLOB '*.jpg' OR lower(a.path) GLOB '*.jpeg')"
    if by == "near":
        anchors = db.execute(
            """WITH candidates AS (
                 SELECT min(a.id) AS id, a.day AS day, s.dhash AS hash FROM asset_scores s
                 JOIN assets a ON a.id=s.asset_id
                 LEFT JOIN similar_members m ON m.asset_id=a.id
                 LEFT JOIN burst_members b ON b.asset_id=a.id
                 WHERE a.active=1 AND m.asset_id IS NULL AND b.asset_id IS NULL
                 GROUP BY a.day,s.dhash HAVING count(DISTINCT a.content_hash)>1
               ), groups AS (
                 SELECT id,NULL AS day,'ml' AS hash FROM similar_groups
                 UNION ALL
                 SELECT min(b.asset_id),NULL,'burst' FROM burst_members b
                 JOIN assets a ON a.id=b.asset_id AND a.active=1
                 LEFT JOIN similar_members m ON m.asset_id=a.id WHERE m.asset_id IS NULL
                 GROUP BY b.burst_id HAVING count(DISTINCT a.content_hash)>1
               )
               SELECT * FROM (SELECT * FROM candidates UNION ALL SELECT * FROM groups)
               WHERE id>? ORDER BY id LIMIT ?""",
            (after, limit + 1),
        ).fetchall()
    elif by == "exact":
        anchors = db.execute(
            "SELECT min(id) AS id, content_hash AS hash FROM assets WHERE active=1 "
            "GROUP BY content_hash HAVING count(*)>1 AND min(id)>? ORDER BY id LIMIT ?",
            (after, limit + 1),
        ).fetchall()
    else:
        db.create_function(
            "archive_stem", 1, lambda s: PurePosixPath(s).stem.casefold(), deterministic=True
        )
        anchors = db.execute(
            "SELECT min(a.id) AS id,folder,archive_stem(path) AS stem FROM assets a "
            f"WHERE active=1 AND (kind='raw' OR {jpeg}) GROUP BY folder,stem "
            f"HAVING sum(kind='raw')>0 AND sum({jpeg})>0 AND min(a.id)>? "
            "ORDER BY id LIMIT ?",
            (after, limit + 1),
        ).fetchall()
    items = []
    for anchor in anchors[:limit]:
        fields = "a.id,a.path,a.size,a.kind,a.rating"
        if by == "exact":
            query = f"SELECT {fields} FROM assets a WHERE active=1 AND content_hash=?"
            params = (anchor["hash"],)
        elif by == "pairs":
            query = f"""SELECT {fields} FROM assets a WHERE active=1 AND folder=?
                        AND archive_stem(a.path)=? AND (kind='raw' OR {jpeg})"""
            params = anchor["folder"], anchor["stem"]
        elif anchor["day"]:
            query = f"""SELECT {fields} FROM assets a JOIN asset_scores s ON s.asset_id=a.id
                        LEFT JOIN similar_members m ON m.asset_id=a.id
                        LEFT JOIN burst_members b ON b.asset_id=a.id
                        WHERE active=1 AND m.asset_id IS NULL AND b.asset_id IS NULL
                        AND a.day=? AND s.dhash=?"""
            params = anchor["day"], anchor["hash"]
        elif anchor["hash"] == "burst":
            query = f"""SELECT {fields} FROM assets a JOIN burst_members b ON b.asset_id=a.id
                        LEFT JOIN similar_members m ON m.asset_id=a.id WHERE active=1
                        AND m.asset_id IS NULL
                        AND b.burst_id=(SELECT burst_id FROM burst_members WHERE asset_id=?)"""
            params = (anchor["id"],)
        else:
            query = f"""SELECT {fields} FROM assets a JOIN similar_members m ON m.asset_id=a.id
                        WHERE active=1 AND m.group_id=?"""
            params = (anchor["id"],)
        members = db.execute(f"{query} ORDER BY a.id LIMIT 101", params).fetchall()
        if len(members) > 1:
            items.append(
                {
                    "id": anchor["id"],
                    "assets": [dict(row) for row in members[:100]],
                    "truncated": len(members) > 100,
                }
            )
    return {
        "items": items,
        "next_cursor": anchors[limit - 1]["id"] if len(anchors) > limit else None,
    }
