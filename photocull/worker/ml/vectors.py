from __future__ import annotations

import json
import sqlite3

import numpy as np
from numkong import angular

BLOCK = 2048
STORAGE_DTYPE = "<f2"

VECTOR_PAGE = """
    SELECT e.asset_id, e.vector FROM asset_embeddings AS e
    WHERE e.model_key = ? AND e.asset_id > ?
    ORDER BY e.asset_id LIMIT ?
"""
TAGGED_VECTOR_PAGE = """
    SELECT t.asset_id, e.vector FROM asset_tags AS t
    JOIN asset_embeddings AS e USING (asset_id)
    WHERE t.tag = ? AND t.asset_id > ? AND e.model_key = ? AND e.tag_key = ?
    ORDER BY t.asset_id LIMIT ?
"""


def normalized(vectors) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    if not np.all(np.isfinite(norms)) or np.any(norms == 0):
        raise ValueError("The model returned an invalid embedding")
    return matrix / norms


def blob_vector(blob: bytes) -> np.ndarray:
    return normalized(np.frombuffer(blob, dtype=STORAGE_DTYPE))


def exact_search(
    db: sqlite3.Connection,
    key: str,
    query: np.ndarray,
    limit: int,
    *,
    exclude: int | None = None,
    minimum: float = -1,
    tag: str | None = None,
    tag_key: str | None = None,
) -> list[tuple[int, str, float]]:
    # The embedding triggers remove inactive and changed assets. Read blocks and
    # winner digests through the same snapshot as the caller's query vector.
    best: list[tuple[int, float]] = []
    query = np.ascontiguousarray(query, dtype=np.float16)
    last_id = 0
    while True:
        if tag is None:
            rows = db.execute(VECTOR_PAGE, (key, last_id, BLOCK)).fetchall()
        else:
            rows = db.execute(TAGGED_VECTOR_PAGE, (tag, last_id, key, tag_key, BLOCK)).fetchall()
        if not rows:
            break
        last_id = rows[-1][0]
        ids = np.fromiter((row[0] for row in rows), dtype=np.int64, count=len(rows))
        matrix = np.frombuffer(b"".join(row[1] for row in rows), dtype=STORAGE_DTYPE).reshape(
            len(rows), len(query)
        )
        scores = np.clip(1 - np.asarray(angular(matrix, query, out_dtype="float32")), -1, 1)
        if exclude is not None:
            scores[ids == exclude] = -np.inf
        order = np.lexsort((ids, -scores))[:limit]
        best.extend((int(ids[i]), float(scores[i])) for i in order if scores[i] >= minimum)
        best.sort(key=lambda item: (-item[1], item[0]))
        del best[limit:]
    if not best:
        return []
    digests = dict(
        db.execute(
            """SELECT asset_id, content_hash FROM asset_embeddings
           WHERE asset_id IN (SELECT value FROM json_each(?))""",
            (json.dumps([item[0] for item in best]),),
        ).fetchall()
    )
    return [(asset_id, digests[asset_id], score) for asset_id, score in best]
