from __future__ import annotations

import sqlite3
from collections.abc import Callable

from photocull.config import ScoringSettings
from photocull.db import write
from photocull.db.catalog import mark_catalog_updated

REJECT_REASON_SQL = """
    CASE
        WHEN clipped_highlights > :max_clipped_highlights THEN 'clipped_highlights'
        WHEN clipped_shadows > :max_clipped_shadows THEN 'clipped_shadows'
        WHEN exposure < :min_exposure THEN 'underexposed'
        WHEN exposure > :max_exposure THEN 'overexposed'
        WHEN sharpness < :min_sharpness THEN 'blur'
    END
"""


def configure_scoring(
    db: sqlite3.Connection, settings: ScoringSettings, pulse: Callable[[], None]
) -> None:
    config = settings.model_dump_json()
    previous = db.execute("SELECT scoring_settings FROM analysis_state").fetchone()[0]
    if previous == config:
        return
    if previous:
        # A restart with either configuration must finish an interrupted backfill.
        with write(db):
            db.execute("UPDATE analysis_state SET scoring_settings = ''")
    after = 0
    while True:
        pulse()
        with write(db):
            ids = db.execute(
                "SELECT asset_id FROM asset_scores WHERE asset_id > ? ORDER BY asset_id LIMIT 256",
                (after,),
            ).fetchall()
            if not ids:
                db.execute("UPDATE analysis_state SET scoring_settings = ?", (config,))
                return
            changed = db.execute(
                f"""UPDATE asset_scores SET suggested_reject_reason = ({REJECT_REASON_SQL})
                    WHERE asset_id > :after AND asset_id <= :end
                      AND suggested_reject_reason IS NOT ({REJECT_REASON_SQL})""",
                {**settings.model_dump(), "after": after, "end": ids[-1][0]},
            ).rowcount
            if changed:
                mark_catalog_updated(db)
            after = ids[-1][0]


def save_scores(
    db: sqlite3.Connection, asset_id: int, scores: dict, settings: ScoringSettings
) -> None:
    db.execute(
        """INSERT INTO asset_scores (asset_id, sharpness, exposure, clipped_highlights,
                                    clipped_shadows, dhash)
           VALUES (:id, :sharpness, :exposure, :clipped_highlights, :clipped_shadows, :dhash)
           ON CONFLICT (asset_id) DO UPDATE SET sharpness = excluded.sharpness,
               exposure = excluded.exposure, clipped_highlights = excluded.clipped_highlights,
               clipped_shadows = excluded.clipped_shadows, dhash = excluded.dhash""",
        {**scores, "id": asset_id},
    )
    db.execute(
        f"UPDATE asset_scores SET suggested_reject_reason = ({REJECT_REASON_SQL})"
        " WHERE asset_id = :id",
        {**settings.model_dump(), "id": asset_id},
    )
