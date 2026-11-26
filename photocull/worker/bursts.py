from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from itertools import combinations, islice

from photocull.config import BurstSettings
from photocull.db import write
from photocull.db.catalog import mark_catalog_updated

CANDIDATES_SQL = """
    SELECT a.id, a.captured_at, a.content_hash, s.dhash, e.vector,
           (1 - CASE WHEN i.score IS NOT NULL THEN :weight ELSE 0 END) *
           (0.60 * s.sharpness + 0.25 * (1 - 2 * abs(s.exposure - 0.5))
            + 0.15 * (1 - s.clipped_highlights - s.clipped_shadows))
           + COALESCE(:weight * i.score, 0) AS quality
    FROM assets AS a JOIN asset_scores AS s ON s.asset_id = a.id
    LEFT JOIN asset_embeddings AS e ON e.asset_id = a.id AND e.model_key = :model_key
    LEFT JOIN insight_results AS i ON i.asset_id = a.id AND i.stage = 'aesthetic'
        AND i.key = :aesthetic_key AND i.content_hash = a.content_hash
    WHERE a.active = 1 AND a.captured_at IS NOT NULL
      AND a.captured_at BETWEEN :start AND :end
      AND (a.captured_at, a.id) > (:after_time, :after_id)
    ORDER BY a.captured_at, a.id LIMIT 256
"""


@dataclass(frozen=True, slots=True)
class Candidate:
    id: int
    timestamp: datetime
    fingerprint: int
    quality: float
    content_hash: str


@dataclass(slots=True)
class Burst:
    anchor: Candidate
    members: list[Candidate] = field(default_factory=list)


class AnchorIndex:
    """Find the nearest active anchor, breaking ties by capture order."""

    def __init__(self, distance: int) -> None:
        self.distance = distance
        self.anchors: dict[int, dict[int, Burst]] = {}
        # For a dense window, partition the bits and probe nearby band values.
        # The sum of the first excluded distances is k+1, so every match is probed
        # in at least one band. Wide thresholds are cheaper to scan directly.
        self.bands = []
        if 0 < distance <= 12:
            count = min(distance + 1, 4)
            radius, extra = divmod(distance + 1 - count, count)
            for band in range(count):
                shift, end = 64 * band // count, 64 * (band + 1) // count
                width = end - shift
                masks = tuple(
                    sum(bits)
                    for changes in range(radius + (band < extra) + 1)
                    for bits in combinations((1 << i for i in range(width)), changes)
                )
                buckets: dict[int, set[int]] = defaultdict(set)
                self.bands.append((shift, (1 << width) - 1, masks, buckets))

    def add(self, burst: Burst) -> None:
        fingerprint = burst.anchor.fingerprint
        if fingerprint in self.anchors:
            self.anchors[fingerprint][burst.anchor.id] = burst
            return
        self.anchors[fingerprint] = {burst.anchor.id: burst}
        for shift, mask, _, buckets in self.bands:
            buckets[(fingerprint >> shift) & mask].add(fingerprint)

    def remove(self, burst: Burst) -> None:
        fingerprint = burst.anchor.fingerprint
        del self.anchors[fingerprint][burst.anchor.id]
        if self.anchors[fingerprint]:
            return
        del self.anchors[fingerprint]
        for shift, mask, _, buckets in self.bands:
            key = (fingerprint >> shift) & mask
            buckets[key].remove(fingerprint)
            if not buckets[key]:
                del buckets[key]

    def nearest(self, fingerprint: int) -> Burst | None:
        exact = self.anchors.get(fingerprint)
        if exact or self.distance == 0:
            return next(iter(exact.values())) if exact else None
        candidates = self.anchors.keys()
        if self.bands and len(candidates) >= 256:
            matches: set[int] = set()
            for shift, mask, offsets, buckets in self.bands:
                key = (fingerprint >> shift) & mask
                for offset in offsets:
                    if bucket := buckets.get(key ^ offset):
                        matches.update(bucket)
            candidates = matches
        best = None
        best_distance = self.distance + 1
        for candidate in candidates:
            distance = (candidate ^ fingerprint).bit_count()
            if distance > best_distance or distance > self.distance:
                continue
            burst = next(iter(self.anchors[candidate].values()))
            if distance < best_distance or (
                best is not None
                and (burst.anchor.timestamp, burst.anchor.id)
                < (best.anchor.timestamp, best.anchor.id)
            ):
                best, best_distance = burst, distance
        return best


def _component_edge(
    db: sqlite3.Connection, seed: str, window: float, forward: bool, pulse: Callable[[], None]
) -> str:
    # Include deleted or moved timestamps as points so both sides of a newly
    # opened gap are reconsidered together. Each branch walks a timestamp index.
    operator, order = (">", "ASC") if forward else ("<", "DESC")
    edge = seed
    clock = datetime.fromisoformat(edge)
    limit = 16
    while rows := db.execute(
        f"""SELECT a.captured_at FROM assets AS a
            JOIN asset_scores AS s ON s.asset_id = a.id
            WHERE a.active = 1 AND a.captured_at IS NOT NULL AND a.captured_at {operator} :after
            UNION
            SELECT captured_at FROM burst_changes WHERE captured_at {operator} :after
            ORDER BY captured_at {order} LIMIT :limit""",
        {"after": edge, "limit": limit},
    ).fetchall():
        pulse()
        for (timestamp,) in rows:
            candidate = datetime.fromisoformat(timestamp)
            if abs((candidate - clock).total_seconds()) > window:
                return edge
            edge, clock = timestamp, candidate
        limit = 256
    return edge


def _affected_ranges(
    db: sqlite3.Connection, window: float, pulse: Callable[[], None]
) -> Iterator[tuple[str, str]]:
    end = ""
    while row := db.execute(
        "SELECT captured_at FROM burst_changes WHERE captured_at > ? ORDER BY captured_at LIMIT 1",
        (end,),
    ).fetchone():
        seed = row[0]
        start = _component_edge(db, seed, window, False, pulse)
        end = _component_edge(db, seed, window, True, pulse)
        yield start, end


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


def _nearest_embedding(anchors: dict[int, tuple[Burst, bytes]], blob: bytes, threshold: float):
    if not anchors:
        return None
    import numpy as np

    from photocull.worker.ml.vectors import STORAGE_DTYPE, blob_vector, normalized

    query = blob_vector(blob)
    candidates = iter(anchors.values())
    best = None
    while batch := list(islice(candidates, 128)):
        data = b"".join(vector for _, vector in batch)
        matrix = normalized(np.frombuffer(data, dtype=STORAGE_DTYPE).reshape(len(batch), -1))
        scores = np.clip(np.einsum("ij,j->i", matrix, query), -1, 1)
        index = int(scores.argmax())
        score = float(scores[index])
        if score >= threshold and (best is None or score > threshold):
            best, threshold = batch[index][0], score
    return best


def _stage_range(
    db: sqlite3.Connection,
    start: str,
    end: str,
    settings: BurstSettings,
    pulse: Callable[[], None],
    model_key: str | None,
    aesthetic_key: str | None,
) -> None:
    active: deque[Burst] = deque()
    index = AnchorIndex(settings.max_hamming_distance)
    hash_only = AnchorIndex(settings.max_hamming_distance) if model_key else index
    vectors: dict[int, tuple[Burst, bytes]] = {}
    params = {
        "start": start,
        "end": end,
        "after_time": start,
        "after_id": 0,
        "model_key": model_key,
        "aesthetic_key": aesthetic_key,
        "weight": settings.aesthetic_weight,
    }
    while rows := db.execute(CANDIDATES_SQL, params).fetchall():
        pulse()
        params.update(after_time=rows[-1]["captured_at"], after_id=rows[-1]["id"])
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
                expired = active.popleft()
                index.remove(expired)
                if vectors.pop(expired.anchor.id, None) is None and model_key:
                    hash_only.remove(expired)
                _stage_burst(db, expired)

            blob = row["vector"]
            if blob is not None:
                burst = _nearest_embedding(vectors, blob, settings.min_cosine)
                if burst is None:
                    burst = hash_only.nearest(candidate.fingerprint)
            else:
                burst = index.nearest(candidate.fingerprint)
            if burst is None:
                burst = Burst(anchor=candidate)
                active.append(burst)
                index.add(burst)
                if blob is not None:
                    vectors[candidate.id] = burst, blob
                elif model_key:
                    hash_only.add(burst)
            burst.members.append(candidate)
    for burst in active:
        _stage_burst(db, burst)


def refresh_bursts(
    db: sqlite3.Connection,
    settings: BurstSettings,
    pulse: Callable[[], None],
    model_key: str | None = None,
    *,
    aesthetic_key: str | None = None,
) -> bool:
    dirty, previous = db.execute(
        "SELECT bursts_dirty, burst_settings FROM analysis_state"
    ).fetchone()
    config = f"{settings.model_dump_json()}:{model_key or ''}:{aesthetic_key or ''}"
    # A full pass is cheaper than thousands of small range seeks after a big import.
    rebuild_all = dirty or previous != config
    if not rebuild_all:
        rebuild_all = bool(db.execute("SELECT 1 FROM burst_changes LIMIT 1 OFFSET 2047").fetchone())
    if not rebuild_all and not db.execute("SELECT 1 FROM burst_changes LIMIT 1").fetchone():
        return False

    # Stage outside the writer transaction. Only the library worker can change
    # membership inputs. API ratings, flags and best picks can proceed in WAL.
    db.execute("""CREATE TEMP TABLE IF NOT EXISTS new_bursts (
        id INTEGER PRIMARY KEY, recommended_best_id INTEGER, membership_key TEXT
    )""")
    db.execute("""CREATE TEMP TABLE IF NOT EXISTS new_members (
        asset_id INTEGER PRIMARY KEY, burst_id INTEGER
    )""")
    db.execute("CREATE TEMP TABLE IF NOT EXISTS affected_bursts (id INTEGER PRIMARY KEY)")
    db.execute("DELETE FROM new_bursts")
    db.execute("DELETE FROM new_members")
    db.execute("DELETE FROM affected_bursts")

    if rebuild_all:
        _stage_range(db, "", "\uffff", settings, pulse, model_key, aesthetic_key)
    else:
        for start, end in _affected_ranges(db, settings.window_seconds, pulse):
            db.execute(
                """INSERT INTO affected_bursts
                   SELECT a.id FROM assets AS a JOIN bursts AS b ON b.id = a.id
                   WHERE a.active = 1 AND a.captured_at IS NOT NULL
                     AND a.captured_at BETWEEN ? AND ?""",
                (start, end),
            )
            _stage_range(db, start, end, settings, pulse, model_key, aesthetic_key)

    pulse()
    scope = "" if rebuild_all else "id IN (SELECT id FROM affected_bursts) AND"
    with write(db):
        before = db.total_changes
        db.execute(f"""DELETE FROM bursts
            WHERE {scope} NOT EXISTS (SELECT 1 FROM new_bursts AS n
                WHERE n.id = bursts.id AND n.membership_key = bursts.membership_key)
        """)
        db.execute("""INSERT INTO bursts SELECT * FROM new_bursts WHERE true
            ON CONFLICT (id) DO UPDATE SET recommended_best_id = excluded.recommended_best_id
            WHERE bursts.recommended_best_id <> excluded.recommended_best_id
        """)
        db.execute("""INSERT INTO burst_members
            SELECT n.asset_id, n.burst_id FROM new_members AS n
            WHERE NOT EXISTS (SELECT 1 FROM burst_members AS m WHERE m.asset_id = n.asset_id)
        """)
        if db.total_changes != before:
            mark_catalog_updated(db)
        db.execute("DELETE FROM burst_changes")
        db.execute("UPDATE analysis_state SET bursts_dirty = 0, burst_settings = ?", (config,))
    return True
