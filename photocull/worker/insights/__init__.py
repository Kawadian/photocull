from __future__ import annotations

import logging
import time

from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.db.insights import configure_stage, discard, finish, register, stage_key
from photocull.insights import AESTHETIC_PROMPTS, models, taste_input
from photocull.media.cache import cache_path
from photocull.worker.insights.model_cache import cache_bytes
from photocull.worker.insights.people import FaceGroups
from photocull.worker.ml.model import ModelUnavailable

LOG = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


class Insights:
    def __init__(self, worker, face_factory=None):
        self.worker = worker
        self.db = worker.db
        self.settings = worker.settings
        self.config = self.settings.insights
        self.inference = worker.ml.inference
        self.identities = models(self.settings)
        self.face_factory = face_factory
        self.face_model = None
        self.groups = FaceGroups()
        self.head = None
        self.cursor = 0
        self.stages = [*self.identities]
        self.last_taste = 0.0
        self.model_retry: dict[str, float] = {}

    @property
    def taste_enabled(self):
        return self.config.taste and self.settings.ml.enabled

    def configure(self):
        with write(self.db):
            for stage in ("aesthetic", "faces", "video", "transcript"):
                model = self.identities.get(stage)
                configure_stage(self.db, stage, register(self.db, model) if model else None)
            state = self.db.execute("SELECT input_key, model_key FROM taste_state").fetchone()
            configure_stage(
                self.db,
                "taste",
                state["model_key"]
                if self.taste_enabled and state["input_key"] == taste_input(self.settings)
                else None,
            )

    def _aesthetic(self, vector: bytes, key: str) -> float:
        import numpy as np

        from photocull.worker.ml.vectors import blob_vector

        if self.head is None:
            target = self.settings.model_cache / "aesthetic" / f"{key}.f32"
            try:
                with target.open("rb") as source:
                    head = np.frombuffer(source.read(2 * len(vector) + 1), dtype="<f4")
                if head.size * 2 != len(vector) or not np.all(np.isfinite(head)):
                    raise ValueError("Invalid cached aesthetic head")
            except (FileNotFoundError, ValueError):
                weights = self.inference.call(
                    "aesthetic_head", (AESTHETIC_PROMPTS,), self.worker.pulse
                )
                head = np.frombuffer(weights, dtype="<f4")
                if head.size * 2 != len(vector) or not np.all(np.isfinite(head)):
                    raise ValueError("Aesthetic head does not match the image encoder") from None
                cache_bytes(target, weights)
            self.head = head
        projection = float(blob_vector(vector) @ self.head)
        return float(1 / (1 + np.exp(-8 * projection)))

    def _faces(self, asset, key, pulse):
        path = cache_path(self.worker.state, "previews", asset["id"], asset["content_hash"])
        if self.face_factory is not None:
            if self.face_model is None:
                self.face_model = self.face_factory()
            detections = self.face_model.detect(path)
        else:
            detections = self.inference.call(
                "faces",
                (
                    path,
                    self.config.face_revision,
                    self.config.offline,
                    self.config.threads,
                    self.config.face_detect_score,
                    self.config.face_min_px,
                ),
                self.worker.pulse,
            )
        self.groups.save(self.db, key, self.config, asset, detections, pulse)

    def _train(self, pulse):
        if not self.taste_enabled:
            return False
        state = self.db.execute("SELECT * FROM taste_state").fetchone()
        if state["input_key"] == taste_input(self.settings) and (
            state["requested"] == state["completed"] or time.monotonic() - self.last_taste < 10
        ):
            return False
        self.last_taste = time.monotonic()
        try:
            from photocull.worker.insights.taste import train

            return train(self.db, self.settings, self.worker.state.name, pulse)
        except (OSError, ValueError, ImportError, MemoryError) as error:
            with write(self.db):
                self.db.execute(
                    """UPDATE taste_state SET completed = ?, input_key = ?, status = 'error',
                       error = ?, model_key = NULL, coefficients = NULL""",
                    (state["requested"], taste_input(self.settings), str(error)),
                )
                configure_stage(self.db, "taste", None)
                mark_catalog_updated(self.db)
            return True

    def step(self, pulse, *, idle=False, video_only=False):
        if idle and not video_only and self._train(pulse):
            return True
        stages = [*self.stages]
        if self.taste_enabled:
            stages.append("taste")
        for offset in range(len(stages)):
            index = (self.cursor + offset) % len(stages)
            stage = stages[index]
            if (stage in {"video", "transcript"}) == video_only and self._process(stage, pulse):
                self.cursor = (index + 1) % len(stages)
                return True
        return False

    def clean_video(self) -> bool:
        rows = self.db.execute(
            """SELECT c.asset_id, a.content_hash, a.active FROM video_cleanup AS c
               JOIN assets AS a ON a.id = c.asset_id LIMIT 32"""
        ).fetchall()
        if not rows:
            return False
        directory = self.worker.state / "video"
        for asset_id, digest, active in rows:
            for target in directory.glob(f"{asset_id}-*.mp4"):
                if not active or not target.name.startswith(f"{asset_id}-{digest}-"):
                    try:
                        target.unlink(missing_ok=True)
                    except OSError as error:
                        LOG.warning("Could not remove obsolete video preview %s: %s", target, error)
        with write(self.db):
            self.db.executemany(
                "DELETE FROM video_cleanup WHERE asset_id=?", ((row[0],) for row in rows)
            )
        return True

    def _process(self, stage, pulse):
        if time.monotonic() < self.model_retry.get(stage, 0.0):
            return False
        key = stage_key(self.db, stage)
        if key is None:
            return False
        row = self.db.execute(
            """SELECT a.id, a.content_hash, a.path, a.source_key, a.kind, a.duration, w.attempts,
                      p.status AS preview_status, e.vector
               FROM insight_work AS w JOIN assets AS a ON a.id = w.asset_id
               LEFT JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
               LEFT JOIN asset_embeddings AS e ON e.asset_id = a.id AND e.model_key = ?
               WHERE w.stage = ? ORDER BY w.asset_id LIMIT 1""",
            (self.settings.ml.model_key, stage),
        ).fetchone()
        if row is None:
            return False
        video = stage in {"video", "transcript"}
        unavailable = (row["kind"] == "video") != video
        unavailable |= stage == "faces" and row["preview_status"] != "done"
        unavailable |= stage in {"aesthetic", "taste"} and row["vector"] is None
        with write(self.db):
            if unavailable:
                discard(self.db, stage, row["id"])
                return True
            self.db.execute(
                "UPDATE insight_work SET attempts = attempts + 1 WHERE stage = ? AND asset_id = ?",
                (stage, row["id"]),
            )
        score = payload = error = None
        try:
            pulse()
            if row["attempts"] >= MAX_ATTEMPTS:
                raise ValueError("Worker was interrupted three times. Retry this stage.")
            if stage == "aesthetic":
                score = self._aesthetic(row["vector"], key)
            elif stage == "faces":
                self._faces(row, key, pulse)
                return True
            elif stage == "taste":
                from photocull.worker.insights.taste import predict

                prediction = predict(self.db, self.settings, row["id"])
                if prediction is not None:
                    score, payload = prediction
            else:
                from photocull.worker.insights.video import run_stage

                payload = run_stage(self.worker, stage, row, key, pulse)
        except (OSError, RuntimeError, ValueError, ImportError, MemoryError) as exc:
            if isinstance(exc, (ModelUnavailable, ImportError)):
                self.model_retry[stage] = time.monotonic() + 60
            error = str(exc) or type(exc).__name__
            LOG.warning("Insight %s for asset %s failed: %s", stage, row["id"], error)
        with write(self.db):
            finish(self.db, stage, key, row, score=score, payload=payload, error=error)
        return True
