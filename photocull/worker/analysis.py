from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

from photocull.config import BurstSettings, ScoringSettings
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
    mark_bursts_dirty(db)


def mark_bursts_dirty(db: sqlite3.Connection) -> None:
    db.execute("UPDATE analysis_state SET bursts_dirty = 1 WHERE bursts_dirty = 0")


def invalidate_burst(db: sqlite3.Connection, asset_id: int) -> None:
    db.execute(
        "DELETE FROM bursts WHERE id = (SELECT burst_id FROM burst_members WHERE asset_id = ?)",
        (asset_id,),
    )
    mark_bursts_dirty(db)


@dataclass
class Candidate:
    id: int
    timestamp: datetime
    fingerprint: int
    quality: float
    content_hash: str


@dataclass
class Burst:
    anchor: Candidate
    members: list[Candidate] = field(default_factory=list)


def _stage_burst(db: sqlite3.Connection, burst: Burst) -> None:
    if len(burst.members) < 2:
        return
    best = max(burst.members, key=lambda member: (member.quality, -member.id))
    digest = sha256()
    for member in burst.members:
        digest.update(f"{member.id}:{member.content_hash}\n".encode())
    db.execute(
        "INSERT INTO new_bursts VALUES (?, ?, ?)",
        (burst.anchor.id, best.id, digest.hexdigest()),
    )
    db.executemany(
        "INSERT INTO new_members VALUES (?, ?)",
        ((member.id, burst.anchor.id) for member in burst.members),
    )


def refresh_bursts(
    db: sqlite3.Connection, settings: BurstSettings, pulse: Callable[[], None]
) -> bool:
    dirty, previous = db.execute(
        "SELECT bursts_dirty, burst_settings FROM analysis_state"
    ).fetchone()
    config = settings.model_dump_json()
    if not dirty and previous == config:
        return False

    # Build in connection-local SQLite tables while the API can continue writing to WAL.
    db.execute("""CREATE TEMP TABLE IF NOT EXISTS new_bursts (
        id INTEGER PRIMARY KEY, recommended_best_id INTEGER, membership_key TEXT
    )""")
    db.execute("""CREATE TEMP TABLE IF NOT EXISTS new_members (
        asset_id INTEGER PRIMARY KEY, burst_id INTEGER
    )""")
    db.execute("DELETE FROM new_bursts")
    db.execute("DELETE FROM new_members")
    active: deque[Burst] = deque()
    after = ("", 0)
    while rows := db.execute(
        """
        SELECT a.id, a.captured_at, a.content_hash, s.dhash,
               0.60 * s.sharpness + 0.25 * (1 - 2 * abs(s.exposure - 0.5))
               + 0.15 * (1 - s.clipped_highlights - s.clipped_shadows) AS quality
        FROM assets AS a JOIN asset_scores AS s ON s.asset_id = a.id
        WHERE a.active = 1 AND a.captured_at IS NOT NULL AND (a.captured_at, a.id) > (?, ?)
        ORDER BY a.captured_at, a.id LIMIT 256
    """,
        after,
    ).fetchall():
        pulse()
        after = (rows[-1]["captured_at"], rows[-1]["id"])
        for row in rows:
            candidate = Candidate(
                row["id"],
                datetime.fromisoformat(row["captured_at"]),
                int(row["dhash"], 16),
                row["quality"],
                row["content_hash"],
            )
            while (
                active
                and (candidate.timestamp - active[0].anchor.timestamp).total_seconds()
                > settings.window_seconds
            ):
                _stage_burst(db, active.popleft())

            # Match the closest eligible anchor, keeping ties in capture order.
            distance = settings.max_hamming_distance + 1
            match = None
            for burst in active:
                difference = (burst.anchor.fingerprint ^ candidate.fingerprint).bit_count()
                if difference < distance:
                    match, distance = burst, difference
                    if distance == 0:
                        break
            if match is None:
                match = Burst(anchor=candidate)
                active.append(match)
            match.members.append(candidate)
    for burst in active:
        _stage_burst(db, burst)

    pulse()
    with write(db):
        before = db.total_changes
        db.execute("""DELETE FROM bursts
            WHERE NOT EXISTS (SELECT 1 FROM new_bursts AS n
                WHERE n.id = bursts.id AND n.membership_key = bursts.membership_key)
        """)
        db.execute("""INSERT INTO bursts SELECT * FROM new_bursts WHERE true
            ON CONFLICT (id) DO UPDATE SET recommended_best_id = excluded.recommended_best_id
            WHERE bursts.recommended_best_id <> excluded.recommended_best_id
        """)
        db.execute("INSERT OR IGNORE INTO burst_members SELECT * FROM new_members")
        if db.total_changes != before:
            mark_catalog_updated(db)
        db.execute("UPDATE analysis_state SET bursts_dirty = 0, burst_settings = ?", (config,))
    return True
