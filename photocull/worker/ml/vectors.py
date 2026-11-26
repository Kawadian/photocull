from __future__ import annotations

import sqlite3

import numpy as np

BLOCK = 512
STORAGE_DTYPE = "<f2"


def normalized(vectors) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    if not np.all(np.isfinite(norms)) or np.any(norms == 0):
        raise ValueError("The model returned an invalid embedding")
    return matrix / norms


def blob_vector(blob: bytes) -> np.ndarray:
    return normalized(np.frombuffer(blob, dtype=STORAGE_DTYPE))


def vector_blocks(
    db: sqlite3.Connection,
    key: str,
    *,
    block_size: int = BLOCK,
    tag: str | None = None,
    tag_key: str | None = None,
):
    # Keyset paging gives each block a short read snapshot during grouping. Search
    # opens its own transaction to see the same catalog across all its blocks.
    after = 0
    restriction = ""
    params: tuple = ()
    if tag is not None:
        restriction = """AND e.tag_key = ? AND EXISTS
            (SELECT 1 FROM asset_tags AS t WHERE t.asset_id = e.asset_id AND t.tag = ?)"""
        params = (tag_key, tag)
    while rows := db.execute(
        f"""SELECT e.asset_id, e.content_hash, e.vector FROM asset_embeddings AS e
            JOIN assets AS a ON a.id = e.asset_id
            WHERE e.asset_id > ? AND e.model_key = ? AND a.active = 1 {restriction}
            ORDER BY e.asset_id LIMIT ?""",
        (after, key, *params, block_size),
    ).fetchall():
        yield rows
        after = rows[-1]["asset_id"]


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
    best: list[tuple[int, str, float]] = []
    for rows in vector_blocks(db, key, tag=tag, tag_key=tag_key):
        rows = [row for row in rows if row["asset_id"] != exclude]
        if not rows:
            continue
        ids = np.fromiter((row["asset_id"] for row in rows), dtype=np.int64, count=len(rows))
        data = b"".join(row["vector"] for row in rows)
        matrix = np.frombuffer(data, dtype=STORAGE_DTYPE).reshape(len(rows), -1).astype(np.float32)
        scores = np.einsum("ij,j->i", matrix, query)
        scores /= np.sqrt(np.einsum("ij,ij->i", matrix, matrix))
        np.clip(scores, -1, 1, out=scores)
        order = np.lexsort((ids, -scores))[:limit]
        best.extend(
            (int(ids[i]), rows[i]["content_hash"], float(scores[i]))
            for i in order
            if scores[i] >= minimum
        )
        best.sort(key=lambda item: (-item[2], item[0]))
        del best[limit:]
    return best
