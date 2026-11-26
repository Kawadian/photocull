from __future__ import annotations

import logging
import sqlite3
from contextlib import suppress
from pathlib import Path

from usearch.index import Index

from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.worker.ml.vectors import blob_vector

LOG = logging.getLogger(__name__)
GROUP_BLOCK = 128


class SimilarityGroups:
    """Assign changed photos to stable anchors, in bounded SQLite transactions."""

    def __init__(self, path: Path, key: str, threshold: float):
        self.path = path
        self.key = key
        self.threshold = threshold
        self.index: Index | None = None
        self.revision: str | None = None
        self.loaded = False
        self.load_cursor = 0
        self.checkpoint_needed = False

    def _file(self, revision: str) -> Path:
        return self.path.with_name(f"similar-anchors-{revision}.usearch")

    def _load(self, db: sqlite3.Connection, revision: str) -> bool:
        if self.revision is None:
            self.revision = revision
            target = self._file(revision)
            if target.is_file():
                try:
                    restored = Index.restore(target)
                    if (
                        restored is not None
                        and len(restored)
                        == db.execute("SELECT count(*) FROM similar_anchors").fetchone()[0]
                    ):
                        self.index = restored
                        self.loaded = True
                        return True
                    LOG.warning("Ignoring incomplete anchor checkpoint %s", target.name)
                except (OSError, RuntimeError, ValueError):
                    LOG.warning("Ignoring unreadable anchor checkpoint %s", target.name)
        rows = db.execute(
            """SELECT a.asset_id, e.vector FROM similar_anchors AS a
               JOIN asset_embeddings AS e ON e.asset_id = a.asset_id AND e.model_key = ?
               WHERE a.asset_id > ? AND NOT EXISTS (
                   SELECT 1 FROM similar_invalid_anchors AS i WHERE i.asset_id = a.asset_id
               ) ORDER BY a.asset_id LIMIT ?""",
            (self.key, self.load_cursor, GROUP_BLOCK),
        ).fetchall()
        for asset_id, blob in rows:
            self._add(asset_id, blob_vector(blob))
        if rows:
            self.load_cursor = rows[-1]["asset_id"]
        self.loaded = len(rows) < GROUP_BLOCK
        self.checkpoint_needed = True
        return self.loaded

    def _add(self, asset_id: int, vector) -> None:
        if self.index is None:
            self.index = Index(
                ndim=len(vector),
                metric="cos",
                dtype="f16",
                connectivity=16,
                expansion_add=128,
                expansion_search=64,
            )
        self.index.add(asset_id, vector, threads=1)

    def _nearest(self, vector) -> int | None:
        if self.index is None or not len(self.index):
            return None
        found = self.index.search(vector, count=8, threads=1)
        threshold = 1 - self.threshold + 1e-6
        nearest = min(
            (
                (float(distance), int(key))
                for key, distance in zip(found.keys, found.distances, strict=True)
                if distance <= threshold
            ),
            default=None,
        )
        return nearest[1] if nearest is not None else None

    def _remove_invalid(self, db: sqlite3.Connection) -> bool:
        rows = db.execute(
            "SELECT asset_id FROM similar_invalid_anchors ORDER BY asset_id LIMIT ?", (GROUP_BLOCK,)
        ).fetchall()
        if not rows:
            return False
        if self.index is not None:
            for (asset_id,) in rows:
                self.index.remove(asset_id, threads=1)
        with write(db):
            db.executemany("DELETE FROM similar_anchors WHERE asset_id = ?", rows)
            db.executemany("DELETE FROM similar_invalid_anchors WHERE asset_id = ?", rows)
            self._advance_revision(db)
        return True

    def _advance_revision(self, db: sqlite3.Connection) -> None:
        self.revision = db.execute(
            """UPDATE ml_state SET anchor_revision = lower(hex(randomblob(16)))
               RETURNING anchor_revision"""
        ).fetchone()[0]
        self.checkpoint_needed = True

    def _assign(self, db: sqlite3.Connection) -> bool:
        rows = db.execute(
            """SELECT d.asset_id, e.vector FROM similar_dirty AS d
               LEFT JOIN asset_embeddings AS e ON e.asset_id = d.asset_id AND e.model_key = ?
               ORDER BY d.asset_id LIMIT ?""",
            (self.key, GROUP_BLOCK),
        ).fetchall()
        if not rows:
            return False
        added = []
        assigned = []
        for asset_id, blob in rows:
            if blob is None:
                continue
            vector = blob_vector(blob)
            if (anchor := self._nearest(vector)) is None:
                self._add(asset_id, vector)
                added.append((asset_id,))
            else:
                assigned.append((asset_id, anchor))
        with write(db):
            db.executemany("INSERT INTO similar_anchors VALUES (?)", added)
            for asset_id, anchor in assigned:
                group = db.execute(
                    """INSERT INTO similar_groups VALUES (?, 2)
                       ON CONFLICT(id) DO UPDATE SET member_count = member_count + 1
                       RETURNING member_count""",
                    (anchor,),
                ).fetchone()
                if group[0] == 2:
                    db.execute("INSERT INTO similar_members VALUES (?, ?)", (anchor, anchor))
                db.execute("INSERT INTO similar_members VALUES (?, ?)", (asset_id, anchor))
            db.executemany(
                "DELETE FROM similar_dirty WHERE asset_id = ?", ((row[0],) for row in rows)
            )
            if added:
                self._advance_revision(db)
            if assigned:
                mark_catalog_updated(db)
        return True

    def _checkpoint(self) -> None:
        if not self.checkpoint_needed or self.revision is None:
            return
        target = self._file(self.revision)
        temporary = target.with_suffix(".part")
        try:
            if self.index is not None and len(self.index):
                self.index.save(temporary)
                temporary.replace(target)
            else:
                self.index = None
                target.unlink(missing_ok=True)
            for file in self.path.parent.glob("similar-anchors-*.usearch"):
                if file != target:
                    file.unlink()
        except (OSError, RuntimeError, ValueError) as error:
            LOG.warning("Could not checkpoint similarity anchors: %s", error)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        self.checkpoint_needed = False

    def step(self, db: sqlite3.Connection) -> bool:
        state = db.execute(
            "SELECT generation, grouped_generation, anchor_revision FROM ml_state"
        ).fetchone()
        generation, done, revision = state
        if generation == done:
            return False
        if not self.loaded and not self._load(db, revision):
            return True
        if self._remove_invalid(db) or self._assign(db):
            return True
        self._checkpoint()
        with write(db):
            db.execute(
                "UPDATE ml_state SET grouped_generation = ?, group_error = NULL", (generation,)
            )
            mark_catalog_updated(db)
        return True
