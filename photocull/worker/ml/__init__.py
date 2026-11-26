from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.db.jobs import (
    DISABLED,
    MODEL_FAILURE,
    NO_EMBEDDING_PREVIEW,
    claim_jobs,
)
from photocull.db.search import claim, expire
from photocull.media.cache import cache_path
from photocull.worker.ml.model import ModelUnavailable
from photocull.worker.ml.process import InferenceProcess

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
                    db.execute("UPDATE ml_state SET grouped_generation = -1")
                db.execute(
                    """UPDATE ml_state SET model_key = ?, enabled = 1, search_enabled = ?,
                       tags_enabled = ?, tag_key = ?, image_error = ?, text_error = NULL,
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
                db.execute(
                    """UPDATE search_requests SET status = 'pending' WHERE status = 'running'"""
                )
            else:
                db.execute(
                    """UPDATE ml_state SET enabled = 0, search_enabled = 0, tags_enabled = 0,
                       image_error = NULL, text_error = NULL, group_error = NULL"""
                )
                db.execute("DELETE FROM search_requests")
            mark_catalog_updated(db)

        if settings.enabled:
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

    def _set_error(self, column: str, error: str | None) -> None:
        # All callers use constant ml_state column names.
        self.db.execute(f"UPDATE ml_state SET {column} = ?", (error,))

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
            expire(self.db)
            self.last_pruned = now
        if not (row := claim(self.db)):
            return False
        results = None
        error = None
        text_error = None
        try:
            if row["model_key"] != self.key:
                raise ValueError("The embedding model changed. Submit the search again.")
            if row["query"] is not None and not self.settings.search_enabled:
                raise ValueError("Text search is disabled")
            if row["tag"] and (not self.settings.tags_enabled or row["tag_key"] != self.tag_key):
                raise ValueError("This tag is unavailable")
            results = []
            if self._has_vectors():
                results = self.inference.call(
                    "search", (self.state_dir / "index.db", dict(row)), self.pulse
                )
        except ModelUnavailable as failure:
            error = str(failure)
            if row["query"] is not None:
                text_error = error
        except ValueError as failure:
            error = str(failure)
        with write(self.db):
            if text_error:
                self._set_error("text_error", text_error)
                mark_catalog_updated(self.db)
            self.db.execute(
                "UPDATE search_requests SET status = ?, results = ?, error = ? WHERE id = ?",
                (
                    "error" if error else "done",
                    json.dumps(results) if results is not None else None,
                    error,
                    row["id"],
                ),
            )
        return True

    def process_images(self) -> bool:
        model_error = self.db.execute("SELECT image_error FROM ml_state").fetchone()[0]
        size = 256 if model_error else self.settings.batch_size
        if not (jobs := claim_jobs(self.db, size, embedding=True)):
            return False
        paths = [
            cache_path(self.state_dir, "previews", job.asset_id, job.content_hash) for job in jobs
        ]
        try:
            if model_error:
                raise ModelUnavailable(model_error)
            results = self.inference.call("images", (paths,), self.pulse)
        except ModelUnavailable as error:
            model_error = str(error)
            results = [(None, MODEL_FAILURE + model_error)] * len(jobs)
            if size != 256:
                LOG.warning("Embedding encoder unavailable: %s", model_error)
        except ValueError as error:
            results = [(None, str(error))] * len(jobs)
        with write(self.db):
            self._set_error("image_error", model_error)
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
            or self.db.execute("SELECT text_error FROM ml_state").fetchone()[0]
        ):
            return False
        rows = []
        for predicate, params in (
            ("tag_key IS NULL", ()),
            ("tag_key < ?", (self.tag_key,)),
            ("tag_key > ?", (self.tag_key,)),
        ):
            rows = self.db.execute(
                f"""SELECT asset_id, vector FROM asset_embeddings
                    WHERE model_key = ? AND {predicate} ORDER BY tag_key, asset_id LIMIT 32""",
                (self.key, *params),
            ).fetchall()
            if rows:
                break
        if not rows:
            return False
        try:
            values = self.inference.call("tags", ([row["vector"] for row in rows],), self.pulse)
        except (ModelUnavailable, ValueError) as error:
            LOG.warning("Tag encoder unavailable: %s", error)
            with write(self.db):
                self._set_error("text_error", str(error))
                mark_catalog_updated(self.db)
            return True
        if values is None:
            return True
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

    def group(self) -> bool:
        generation, previous, error, image_error = self.db.execute(
            "SELECT generation, grouped_generation, group_error, image_error FROM ml_state"
        ).fetchone()
        if generation == previous or error or image_error:
            return False
        if not self._has_vectors():
            with write(self.db):
                self.db.execute("UPDATE ml_state SET grouped_generation = ?", (generation,))
                mark_catalog_updated(self.db)
            return True
        try:
            return self.inference.call(
                "group_step", (self.state_dir / "index.db", generation), self.pulse
            )
        except (ModelUnavailable, ValueError) as failure:
            LOG.warning("Similar groups unavailable: %s", failure)
            with write(self.db):
                self._set_error("group_error", str(failure))
                mark_catalog_updated(self.db)
            return True
