from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from functools import partial
from pathlib import Path

from photocull.config import DecisionKind, Settings, VLMSettings
from photocull.db import connect, write
from photocull.db.catalog import mark_catalog_updated
from photocull.media.cache import cache_path
from photocull.vlm import Decision, OpenAIProvider, ProviderError
from photocull.worker.ml import LibraryML
from photocull.worker.ml.model import ModelUnavailable
from photocull.worker.ml.process import InferenceInterrupted
from photocull.worker.rejections import save_scores
from photocull.worker.scoring import score_preview


class LibraryDecisions:
    def __init__(
        self,
        db: sqlite3.Connection,
        state: Path,
        settings: Settings,
        ml: LibraryML,
        remote: OpenAIProvider,
        pool: ThreadPoolExecutor,
    ):
        self.db = db
        self.state = state
        self.settings = settings
        self.ml = ml
        self.remote = remote
        self.config = settings.vlm
        self.pool = pool

    def configure(self, config: VLMSettings) -> None:
        self.config = config
        self.remote = self.remote.with_config(config.openai)

    def _local_tags(self, asset_id: int, digest: str, preview: Path) -> list[tuple[str, float]]:
        settings = self.settings.ml
        if not (settings.enabled and settings.tags_enabled):
            raise ProviderError("local_tags_disabled")
        row = self.db.execute(
            """SELECT vector FROM asset_embeddings
               WHERE asset_id = ? AND content_hash = ? AND model_key = ?""",
            (asset_id, digest, self.ml.key),
        ).fetchone()
        if row is None:
            [(blob, _)] = self.ml.inference.call("images", ([preview],), self.ml.pulse)
            if blob is None:
                raise ProviderError("preview_unavailable")
        else:
            blob = row["vector"]
        # Tag vectors are prepared in batches by the local inference process.
        while (tags := self.ml.inference.call("tags", ([blob],), self.ml.pulse)) is None:
            self.ml.pulse()
        return tags[0]

    def _finish(self, jobs: list[sqlite3.Row], remote: Decision | None) -> None:
        asset_id, digest = jobs[0]["asset_id"], jobs[0]["content_hash"]
        preview = cache_path(self.state, "previews", asset_id, digest)
        for job in jobs:
            kind = job["kind"]
            provider = "unjudged"
            tags: list[tuple[str, float]] = []
            scores = None
            quality = None
            target = job["provider"]
            if target == "openai" and remote is not None and kind in remote.model_fields_set:
                provider = "openai"
                if kind == "tags":
                    tags = [(tag.tag, tag.score) for tag in remote.tags or ()]
                elif remote.flags:
                    quality = remote.flags.model_dump_json()
            elif target == "local" or self.config.on_failure == "local":
                try:
                    if kind == "tags":
                        tags = self._local_tags(asset_id, digest, preview)
                    else:
                        scores = asdict(score_preview(preview))
                    provider = "local"
                except (InferenceInterrupted, ModelUnavailable, ProviderError, OSError, ValueError):
                    pass
            with write(self.db):
                deleted = self.db.execute(
                    """DELETE FROM decision_jobs WHERE asset_id = ? AND kind = ? AND provider = ?
                       AND content_hash = ? AND EXISTS (
                           SELECT 1 FROM assets WHERE id = ? AND active = 1 AND content_hash = ?
                       )""",
                    (asset_id, kind, target, digest, asset_id, digest),
                ).rowcount
                if not deleted:
                    continue
                if kind == "flags":
                    if scores is not None:
                        save_scores(self.db, asset_id, scores, self.settings.scoring)
                    self.db.execute(
                        "UPDATE assets SET vlm_flag_provider = ?, vlm_quality = ? WHERE id = ?",
                        (provider if provider != "local" else None, quality, asset_id),
                    )
                else:
                    self.db.execute(
                        """INSERT INTO decision_tag_results VALUES (?, ?, ?, ?)
                           ON CONFLICT(asset_id) DO UPDATE SET provider = excluded.provider,
                           content_hash = excluded.content_hash, tag_key = excluded.tag_key""",
                        (asset_id, provider, digest, self.ml.tag_key),
                    )
                    self.db.execute("DELETE FROM decision_tags WHERE asset_id = ?", (asset_id,))
                    self.db.executemany(
                        "INSERT INTO decision_tags VALUES (?, ?, ?)",
                        ((asset_id, tag, score) for tag, score in tags),
                    )
                mark_catalog_updated(self.db)

    def _still_requested(self, asset_id: int, digest: str, kinds: tuple[DecisionKind, ...]) -> bool:
        with connect(self.state / "index.db") as db:
            return any(
                row[0] in kinds
                for row in db.execute(
                    """SELECT kind FROM decision_jobs
                       WHERE asset_id = ? AND content_hash = ? AND provider = 'openai'""",
                    (asset_id, digest),
                )
            )

    def step(self, pulse: Callable[[], None]) -> bool:
        jobs = self.db.execute(
            """SELECT * FROM decision_jobs WHERE asset_id IN (
                   SELECT DISTINCT asset_id FROM decision_jobs ORDER BY asset_id LIMIT ?
               ) ORDER BY asset_id, kind""",
            (self.config.openai.concurrency,),
        ).fetchall()
        if not jobs:
            return False
        groups: dict[int, list[sqlite3.Row]] = {}
        for job in jobs:
            groups.setdefault(job["asset_id"], []).append(job)
        pending: dict[Future[Decision], list[sqlite3.Row]] = {}
        for asset_id, group in groups.items():
            digest = group[0]["content_hash"]
            kinds: tuple[DecisionKind, ...] = tuple(
                job["kind"] for job in group if job["provider"] == "openai"
            )
            if kinds:
                future = self.pool.submit(
                    self.remote.analyze,
                    digest,
                    cache_path(self.state, "previews", asset_id, digest),
                    kinds,
                    self.settings.ml,
                    partial(self._still_requested, asset_id, digest, kinds),
                )
                pending[future] = group
            else:
                self._finish(group, None)
        while pending:
            pulse()
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                jobs = pending.pop(future)
                try:
                    result = future.result()
                except ProviderError:
                    result = None
                self._finish(jobs, result)
        return True
