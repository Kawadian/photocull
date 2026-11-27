from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from photocull.config import ScoringSettings
from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.media.cache import cache_path

LOG = logging.getLogger(__name__)

REJECT_REASON_SQL = """
    CASE
        WHEN (detail IS NULL OR detail >= :min_detail_when_clipped)
          AND (clipped_highlights > :max_clipped_highlights
               OR clipped_shadows > :max_clipped_shadows
               OR exposure NOT BETWEEN :min_exposure AND :max_exposure) THEN NULL
        WHEN sharpness < :min_sharpness
          AND exposure BETWEEN :min_exposure AND :max_exposure THEN 'blur'
        WHEN clipped_highlights > :max_clipped_highlights THEN 'clipped_highlights'
        WHEN clipped_shadows > :max_clipped_shadows THEN 'clipped_shadows'
        WHEN exposure < :min_exposure THEN 'underexposed'
        WHEN exposure > :max_exposure THEN 'overexposed'
    END
"""


def configure_scoring(
    db: sqlite3.Connection, state: Path, settings: ScoringSettings, pulse: Callable[[], None]
) -> None:
    config = settings.model_dump_json()
    previous = db.execute("SELECT scoring_settings FROM analysis_state").fetchone()[0]
    if previous == config:
        return

    from photocull.worker.scoring import score_preview

    if previous:
        # A restart with either configuration must finish an interrupted backfill.
        with write(db):
            db.execute("UPDATE analysis_state SET scoring_settings = ''")
    after = 0
    while True:
        pulse()
        rows = db.execute(
            """SELECT s.asset_id, a.content_hash, s.detail FROM asset_scores AS s
               JOIN assets AS a ON a.id = s.asset_id
               WHERE s.asset_id > ? ORDER BY s.asset_id LIMIT 256""",
            (after,),
        ).fetchall()
        if not rows:
            with write(db):
                db.execute("UPDATE analysis_state SET scoring_settings = ?", (config,))
            return

        updates = []
        for asset_id, digest, detail in rows:
            if detail is not None:
                continue
            pulse()
            preview = cache_path(state, "previews", asset_id, digest)
            try:
                scores = score_preview(preview)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                LOG.warning("Could not backfill scoring for asset %s: %s", asset_id, error)
                continue
            updates.append((scores.detail, scores.bright_p99, scores.dark_p01, asset_id, digest))

        with write(db):
            db.executemany(
                """UPDATE asset_scores SET detail = ?, bright_p99 = ?, dark_p01 = ?
                   WHERE asset_id = ? AND detail IS NULL AND EXISTS (
                       SELECT 1 FROM assets WHERE id = asset_scores.asset_id
                       AND content_hash = ?)""",
                updates,
            )
            changed = db.execute(
                f"""UPDATE asset_scores SET suggested_reject_reason = ({REJECT_REASON_SQL})
                    WHERE asset_id > :after AND asset_id <= :end
                      AND suggested_reject_reason IS NOT ({REJECT_REASON_SQL})""",
                {**settings.model_dump(), "after": after, "end": rows[-1]["asset_id"]},
            ).rowcount
            if changed:
                mark_catalog_updated(db)
            after = rows[-1]["asset_id"]


def save_scores(
    db: sqlite3.Connection, asset_id: int, scores: dict, settings: ScoringSettings
) -> None:
    db.execute(
        """INSERT INTO asset_scores (asset_id, sharpness, exposure, clipped_highlights,
                                    clipped_shadows, dhash, detail, bright_p99, dark_p01)
           VALUES (:id, :sharpness, :exposure, :clipped_highlights, :clipped_shadows, :dhash,
                   :detail, :bright_p99, :dark_p01)
           ON CONFLICT (asset_id) DO UPDATE SET sharpness = excluded.sharpness,
               exposure = excluded.exposure, clipped_highlights = excluded.clipped_highlights,
               clipped_shadows = excluded.clipped_shadows, dhash = excluded.dhash,
               detail = excluded.detail, bright_p99 = excluded.bright_p99,
               dark_p01 = excluded.dark_p01""",
        {**scores, "id": asset_id},
    )
    db.execute(
        f"UPDATE asset_scores SET suggested_reject_reason = ({REJECT_REASON_SQL})"
        " WHERE asset_id = :id",
        {**settings.model_dump(), "id": asset_id},
    )
