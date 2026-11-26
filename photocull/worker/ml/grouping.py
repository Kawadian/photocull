from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from itertools import chain

from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.worker.ml.vectors import blob_vector, vector_blocks


def rebuild(db: sqlite3.Connection, key: str, generation: int, threshold: float) -> Iterator[None]:
    from usearch.index import Index

    anchors = None
    groups: dict[int, list[int]] = defaultdict(list)
    for rows in vector_blocks(db, key, block_size=128):
        for asset_id, _, blob in rows:
            vector = blob_vector(blob)
            if anchors is None:
                anchors = Index(
                    ndim=len(vector),
                    metric="cos",
                    dtype="f16",
                    connectivity=16,
                    expansion_add=128,
                    expansion_search=64,
                )
            if len(anchors):
                neighbors = anchors.search(vector, count=8, threads=1)
                candidates = sorted(
                    (float(distance), int(anchor))
                    for anchor, distance in zip(neighbors.keys, neighbors.distances, strict=True)
                )
                if candidates and candidates[0][0] <= 1 - threshold + 1e-6:
                    groups[candidates[0][1]].append(asset_id)
                    continue
            anchors.add(asset_id, vector, threads=1)
        yield

    with write(db):
        current = db.execute("SELECT model_key, generation FROM ml_state").fetchone()
        if tuple(current) != (key, generation):
            return
        db.execute("DELETE FROM similar_groups")
        db.executemany(
            "INSERT INTO similar_groups VALUES (?, ?)",
            ((anchor, len(members) + 1) for anchor, members in groups.items()),
        )
        db.executemany(
            "INSERT INTO similar_members VALUES (?, ?)",
            (
                (member, anchor)
                for anchor, members in groups.items()
                for member in chain((anchor,), members)
            ),
        )
        db.execute("UPDATE ml_state SET grouped_generation = ?, group_error = NULL", (generation,))
        mark_catalog_updated(db)
