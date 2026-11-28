from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from photocull.db import search, write
from photocull.db.catalog import mark_catalog_updated
from photocull.db.decisions import tags_available
from photocull.db.jobs import (
    DISABLED,
    MODEL_FAILURE,
    NO_EMBEDDING_PREVIEW,
    claim_jobs,
)
from photocull.media.cache import cache_path
from photocull.worker.ml.model import ModelUnavailable
from photocull.worker.ml.process import InferenceInterrupted, InferenceProcess

LOG = logging.getLogger(__name__)


class LibraryML:
    def __init__(self, db: sqlite3.Connection, state: Path, inference: InferenceProcess, pulse):
        self.db = db
        self.state_dir = state
        self.inference = inference
        self.pulse = pulse
        self.settings = inference.settings.ml
        self.key = self.settings.model_key
        self.tag_key = self.settings.tag_key
        self.last_pruned = 0.0
        self.tag_failures = 0
        self.group_failures = 0

    def configure(self) -> None:
        settings = self.settings
        db = self.db
        with write(db):
            previous = db.execute("SELECT * FROM ml_state").fetchone()
            if settings.enabled:
                db.execute(
                    "INSERT OR IGNORE INTO embedding_models VALUES (?, ?, ?, ?)",
                    (
                        self.key,
                        settings.model_id,
                        settings.model_revision,
                        json.dumps(settings.preprocessing, sort_keys=True),
                    ),
                )
                if (
                    previous["model_key"] != self.key
                    or previous["threshold"] != settings.similar_threshold
                ):
                    db.execute("DELETE FROM similar_groups")
                    db.execute("DELETE FROM similar_anchors")
                    db.execute("DELETE FROM similar_invalid_anchors")
                    db.execute("DELETE FROM similar_dirty")
                    db.execute(
                        "INSERT INTO similar_dirty SELECT asset_id FROM asset_embeddings "
                        "WHERE model_key = ?",
                        (self.key,),
                    )
                    db.execute("""UPDATE ml_state SET grouped_generation = -1,
                                  anchor_revision = lower(hex(randomblob(16)))""")
                db.execute(
                    """UPDATE ml_state SET model_key = ?, enabled = 1, search_enabled = ?,
                       tags_enabled = ?, tag_key = ?, image_error = ?, text_error = NULL,
                       tag_error = NULL,
                       group_error = NULL, threshold = ?""",
                    (
                        self.key,
                        settings.search_enabled,
                        settings.tags_enabled,
                        self.tag_key,
                        self.inference.error,
                        settings.similar_threshold,
                    ),
                )
            else:
                db.execute(
                    """UPDATE ml_state SET enabled = 0, search_enabled = 0, tags_enabled = 0,
                       image_error = NULL, text_error = NULL, tag_error = NULL,
                       group_error = NULL"""
                )
                db.execute("DELETE FROM search_requests")
            mark_catalog_updated(db)

        if settings.enabled:
            search.recover(db)
            self._configure_jobs()
        else:
            self._disable_jobs()

    def _disable_jobs(self) -> None:
        while self.db.execute(
            "SELECT 1 FROM jobs WHERE stage = 'embedding' AND status = 'pending' LIMIT 1"
        ).fetchone():
            self.pulse()
            with write(self.db):
                self.db.execute(
                    """UPDATE jobs SET status = 'skipped', error = ? WHERE stage = 'embedding'
                       AND asset_id IN (SELECT asset_id FROM jobs WHERE stage = 'embedding'
                           AND status = 'pending' LIMIT 512)""",
                    (DISABLED,),
                )
                mark_catalog_updated(self.db)

    def _configure_jobs(self) -> None:
        after = 0
        while rows := self.db.execute(
            """SELECT a.id, a.content_hash FROM assets AS a
               WHERE a.id > ? AND a.active = 1 AND a.kind <> 'video' ORDER BY a.id LIMIT 512""",
            (after,),
        ).fetchall():
            self.pulse()
            after = rows[-1]["id"]
            with write(self.db):
                before = self.db.total_changes
                self.db.executemany(
                    """INSERT INTO jobs (asset_id, stage, content_hash, status, model_key)
                       VALUES (?, 'embedding', ?, 'pending', ?)
                       ON CONFLICT (asset_id, stage) DO UPDATE SET
                           status = 'pending', error = NULL, interruptions = 0,
                           attempts = CASE WHEN jobs.model_key IS NOT excluded.model_key
                                           THEN 0 ELSE jobs.attempts END,
                           model_key = excluded.model_key, content_hash = excluded.content_hash
                       WHERE jobs.model_key IS NOT excluded.model_key
                          OR jobs.content_hash <> excluded.content_hash
                          OR (jobs.status = 'skipped' AND jobs.error = ?)
                          OR (jobs.status = 'skipped' AND jobs.error = ? AND EXISTS (
                              SELECT 1 FROM jobs AS p WHERE p.asset_id = jobs.asset_id
                                  AND p.stage = 'preview' AND p.status = 'done'
                          ))
                          OR (jobs.status = 'error' AND substr(jobs.error, 1, ?) = ?)""",
                    (
                        (
                            asset_id,
                            digest,
                            self.key,
                            DISABLED,
                            NO_EMBEDDING_PREVIEW,
                            len(MODEL_FAILURE),
                            MODEL_FAILURE,
                        )
                        for asset_id, digest in rows
                    ),
                )
                if self.db.total_changes != before:
                    mark_catalog_updated(self.db)

    def _has_vectors(self) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM asset_embeddings WHERE model_key = ? LIMIT 1", (self.key,)
            ).fetchone()
            is not None
        )

    def process_search(self) -> bool:
        now = time.monotonic()
        if now - self.last_pruned >= 30:
            search.expire(self.db)
            self.last_pruned = now
        if self.inference.cooling_down or not (row := search.claim(self.db)):
            return False
        results = None
        error = None
        text_error = None
        interrupted = False
        try:
            if row["model_key"] != self.key:
                raise ValueError("The embedding model changed. Submit the search again.")
            if row["query"] is not None and not self.settings.search_enabled:
                raise ValueError("Text search is disabled")
            if row["tag"] and (
                row["tag_key"] != self.tag_key
                or not tags_available(
                    self.db,
                    self.inference.settings.vlm.selection,
                    self.settings.tags_enabled,
                )
            ):
                raise ValueError("This tag is unavailable")
            results = []
            if self._has_vectors():
                remaining = row["created_at"] + search.SEARCH_TIMEOUT - time.time()
                if remaining <= 0:
                    raise ValueError(search.SEARCH_TIMED_OUT)
                results = self.inference.call(
                    "search",
                    (self.state_dir / "index.db", dict(row)),
                    self.pulse,
                    timeout=remaining,
                )
        except InferenceInterrupted as failure:
            LOG.warning("Search inference interrupted: %s", failure)
            error = str(failure)
            interrupted = True
        except ModelUnavailable as failure:
            error = str(failure)
            if row["query"] is not None:
                text_error = error
        except ValueError as failure:
            error = str(failure)
        with write(self.db):
            if text_error:
                self.db.execute("UPDATE ml_state SET text_error = ?", (text_error,))
                mark_catalog_updated(self.db)
            search.finish(self.db, row, results, error, interrupted=interrupted)
        return True

    def process_images(self) -> bool:
        if self.inference.cooling_down:
            return False
        model_error = self.db.execute("SELECT image_error FROM ml_state").fetchone()[0]
        size = 256 if model_error else self.inference.image_batch_size
        if not (jobs := claim_jobs(self.db, size, embedding=True)):
            return False
        paths = [
            cache_path(self.state_dir, "previews", job.asset_id, job.content_hash) for job in jobs
        ]
        try:
            if model_error:
                raise ModelUnavailable(model_error)
            results = self.inference.call("images", (paths,), self.pulse)
        except InferenceInterrupted as error:
            LOG.warning("Image inference interrupted: %s", error)
            with write(self.db):
                self.db.executemany(
                    """UPDATE jobs SET interruptions = interruptions + 1,
                       status = CASE WHEN interruptions >= 2 THEN 'error' ELSE 'pending' END,
                       error = CASE WHEN interruptions >= 2 THEN ? END
                       WHERE asset_id = ? AND stage = 'embedding' AND status = 'running'""",
                    ((str(error), job.asset_id) for job in jobs),
                )
                mark_catalog_updated(self.db)
            return True
        except ModelUnavailable as error:
            model_error = str(error)
            results = [(None, MODEL_FAILURE + model_error)] * len(jobs)
            if size != 256:
                LOG.warning("Embedding encoder unavailable: %s", model_error)
        except ValueError as error:
            results = [(None, str(error))] * len(jobs)
        with write(self.db):
            self.db.execute("UPDATE ml_state SET image_error = ?", (model_error,))
            for job, (blob, error) in zip(jobs, results, strict=True):
                if blob is not None:
                    self.db.execute(
                        "DELETE FROM asset_embeddings WHERE asset_id = ?", (job.asset_id,)
                    )
                    self.db.execute(
                        """INSERT INTO asset_embeddings(asset_id, model_key, content_hash, vector)
                           VALUES (?, ?, ?, ?)""",
                        (job.asset_id, self.key, job.content_hash, blob),
                    )
                self.db.execute(
                    """UPDATE jobs SET status = ?, error = ?, interruptions = 0
                       WHERE asset_id = ? AND stage = 'embedding'""",
                    ("error" if error else "done", error, job.asset_id),
                )
            mark_catalog_updated(self.db)
        return True

    def process_tags(self) -> bool:
        if (
            not self.settings.tags_enabled
            or self.inference.cooling_down
            or self.db.execute("SELECT tag_error FROM ml_state").fetchone()[0]
        ):
            return False
        rows = self.db.execute(
            """WITH candidates AS (
                SELECT asset_id FROM (
                    SELECT asset_id FROM asset_embeddings WHERE model_key = :model
                    AND tag_key IS NULL ORDER BY asset_id LIMIT 32
                ) UNION ALL SELECT asset_id FROM (
                    SELECT asset_id FROM asset_embeddings WHERE model_key = :model
                    AND tag_key < :tags ORDER BY tag_key, asset_id LIMIT 32
                ) UNION ALL SELECT asset_id FROM (
                    SELECT asset_id FROM asset_embeddings WHERE model_key = :model
                    AND tag_key > :tags ORDER BY tag_key, asset_id LIMIT 32
                )
            ) SELECT asset_id, vector FROM candidates JOIN asset_embeddings USING (asset_id)
              LIMIT 32""",
            {"model": self.key, "tags": self.tag_key},
        ).fetchall()
        if not rows:
            return False
        try:
            values = self.inference.call("tags", ([row["vector"] for row in rows],), self.pulse)
        except InferenceInterrupted as error:
            self.tag_failures += 1
            if self.tag_failures < 3:
                return True
            return self._tag_error(error)
        except (ModelUnavailable, ValueError) as error:
            return self._tag_error(error)
        if values is None:
            return True
        self.tag_failures = 0
        with write(self.db):
            for row, tags in zip(rows, values, strict=True):
                asset_id = row["asset_id"]
                self.db.execute("DELETE FROM asset_tags WHERE asset_id = ?", (asset_id,))
                self.db.executemany(
                    "INSERT INTO asset_tags VALUES (?, ?, ?)",
                    ((asset_id, tag, score) for tag, score in tags),
                )
                self.db.execute(
                    "UPDATE asset_embeddings SET tag_key = ? WHERE asset_id = ?",
                    (self.tag_key, asset_id),
                )
            mark_catalog_updated(self.db)
        return True

    def _tag_error(self, error: Exception) -> bool:
        LOG.warning("Tag encoder unavailable: %s", error)
        with write(self.db):
            self.db.execute("UPDATE ml_state SET tag_error = ?", (str(error),))
            mark_catalog_updated(self.db)
        return True

    def group(self) -> bool:
        if self.inference.cooling_down:
            return False
        generation, previous, error = self.db.execute(
            "SELECT generation, grouped_generation, group_error FROM ml_state"
        ).fetchone()
        if generation == previous or error or self.inference.error:
            return False
        if (
            not self._has_vectors()
            and not self.db.execute("SELECT 1 FROM similar_anchors LIMIT 1").fetchone()
        ):
            with write(self.db):
                self.db.execute("DELETE FROM similar_dirty")
                self.db.execute("UPDATE ml_state SET grouped_generation = ?", (generation,))
                mark_catalog_updated(self.db)
            return True
        try:
            worked = self.inference.call("group_step", (self.state_dir / "index.db",), self.pulse)
            self.group_failures = 0
            return worked
        except InferenceInterrupted as error:
            self.group_failures += 1
            if self.group_failures < 3:
                return True
            return self._group_error(error)
        except (ModelUnavailable, ValueError) as error:
            return self._group_error(error)

    def _group_error(self, error: Exception) -> bool:
        LOG.warning("Similar groups unavailable: %s", error)
        with write(self.db):
            self.db.execute("UPDATE ml_state SET group_error = ?", (str(error),))
            mark_catalog_updated(self.db)
        return True
