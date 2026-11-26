from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice

from photocull.config import _fingerprint
from photocull.db import write
from photocull.db.catalog import mark_catalog_updated

EARTH_RADIUS = 6371
REPRESENTATIVES = 6
CANDIDATES = 80


def unit(latitude, longitude):
    lat, lon = math.radians(latitude), math.radians(longitude)
    return math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)


def distance(a, b):
    chord = math.dist(unit(*a), unit(*b))
    return 2 * EARTH_RADIUS * math.asin(min(1, chord / 2))


def timestamp(value):
    try:
        # EXIF capture time is the camera's wall clock. Day boundaries preserve it.
        return datetime.fromisoformat(value).replace(tzinfo=None) if value else None
    except ValueError:
        return None


def gps(latitude, longitude):
    if (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        return latitude, longitude
    return None


class Places:
    def __init__(self, path, pulse):
        import numpy as np

        self.names = []
        coordinates = []
        with path.open(encoding="utf-8") as source:
            for index, line in enumerate(source):
                if index % 4096 == 0:
                    pulse()
                record = line.rstrip("\n").split("\t")
                if len(record) < 9 or record[6] != "P":
                    continue
                try:
                    location = gps(float(record[4]), float(record[5]))
                except ValueError:
                    continue
                if location:
                    self.names.append(f"{record[1]}, {record[8]}")
                    coordinates.append(unit(*location))
        if not coordinates:
            raise ValueError("GeoNames file has no populated places")
        self.coordinates = np.array(coordinates, dtype=np.float32)

    def nearby(self, point):
        import numpy as np

        scores = np.einsum("ij,j->i", self.coordinates, unit(*point))
        index = int(scores.argmax())
        if scores[index] < math.cos(75 / EARTH_RADIUS):
            return None
        return self.names[index]


@dataclass
class Segment:
    first: datetime | None = None
    last: datetime | None = None
    anchor: tuple[float, float] | None = None
    count: int = 0
    best: list = field(default_factory=list)

    def add(self, row, clock, point):
        self.count += 1
        self.first = self.first or clock
        self.last = clock or self.last
        self.anchor = self.anchor or point
        if row["ready"] and not row["rejected"]:
            item = (row["quality"], -row["id"], row["burst_id"], row["dhash"], row["vector"], clock)
            if len(self.best) < CANDIDATES:
                heapq.heappush(self.best, item)
            elif item > self.best[0]:
                heapq.heapreplace(self.best, item)

    def covers(self):
        import numpy as np

        candidates = sorted(self.best, reverse=True)
        vectors = {
            item[1]: np.frombuffer(item[4], dtype="<f2").astype(np.float32)
            for item in candidates
            if item[4]
        }

        def similarity(a, b):
            if a[1] in vectors and b[1] in vectors:
                return max(0, float(vectors[a[1]] @ vectors[b[1]]))
            if a[3] and b[3]:
                return 1 - (int(a[3], 16) ^ int(b[3], 16)).bit_count() / 64
            if a[5] and b[5]:
                return max(0, 1 - abs((a[5] - b[5]).total_seconds()) / 300)
            return 0

        selected = []
        bursts = set()
        while candidates and len(selected) < REPRESENTATIVES:
            index = max(
                range(len(candidates)),
                key=lambda i: (
                    candidates[i][0]
                    - 0.55
                    * max((similarity(candidates[i], other) for other in selected), default=0)
                ),
            )
            item = candidates.pop(index)
            if item[2] is not None and item[2] in bursts:
                continue
            bursts.add(item[2])
            selected.append(item)
        return [-item[1] for item in selected]


class Stories:
    def __init__(self, worker):
        self.worker = worker
        self.db = worker.db
        self.config = worker.settings.insights
        self.places = None
        self.model_key = worker.settings.ml.model_key if worker.settings.ml.enabled else None
        self.aesthetic = worker.insights.identities.get("aesthetic")
        self.source_key = None

    def configure(self, pulse):
        key = None
        error = None
        if self.config.story:
            path = self.config.story_geonames
            identity = None
            if path:
                try:
                    info = path.stat()
                    identity = (str(path), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    if self.source_key != identity:
                        self.places = Places(path, pulse)
                        self.source_key = identity
                except (OSError, UnicodeError, ValueError, ImportError) as exc:
                    self.places = None
                    self.source_key = None
                    error = f"GeoNames: {exc}"
            key = _fingerprint(
                (
                    1,
                    self.config.story_gap_minutes,
                    self.config.story_distance_km,
                    identity,
                    error,
                    self.model_key,
                    self.aesthetic.key if self.aesthetic else None,
                )
            )
        previous, previous_error = self.db.execute("SELECT key, error FROM story_state").fetchone()
        if (previous, previous_error) == (key, error):
            return
        with write(self.db):
            self.db.execute("UPDATE story_state SET key = ?, error = ?", (key, error))
            self.db.execute("DELETE FROM story_dirty")
            if key is not None:
                self.db.execute(
                    "INSERT INTO story_dirty(day) SELECT day FROM assets WHERE active=1"
                    " UNION SELECT day FROM story_days"
                )
            mark_catalog_updated(self.db)

    def step(self, pulse):
        if not self.config.story:
            return False
        dirty = self.db.execute(
            "SELECT day, generation FROM story_dirty ORDER BY day LIMIT 1"
        ).fetchone()
        if dirty is None:
            return False
        day, generation = dirty
        key = self.db.execute("SELECT key FROM story_state").fetchone()[0]
        cursor = self.db.execute(
            """SELECT a.id, a.captured_at, a.latitude, a.longitude, a.rejected, m.burst_id,
                      s.dhash, e.vector, p.status = 'done' AS ready,
                      0.27 * COALESCE(s.sharpness, 0.5)
                      + 0.13 * COALESCE(1 - 2 * abs(s.exposure - 0.5), 0.5)
                      + 0.30 * COALESCE(i.score, 0.5) + 0.30 * a.rating / 5 AS quality
               FROM assets AS a
               LEFT JOIN burst_members AS m ON m.asset_id = a.id
               LEFT JOIN asset_scores AS s ON s.asset_id = a.id
               LEFT JOIN asset_embeddings AS e ON e.asset_id = a.id AND e.model_key = :model
               LEFT JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
               LEFT JOIN insight_results AS i ON i.asset_id = a.id AND i.stage = 'aesthetic'
                   AND i.key = :aesthetic
               WHERE a.active = 1 AND a.day = :day
               ORDER BY a.captured_at IS NULL, a.captured_at, a.id""",
            {
                "day": day,
                "model": self.model_key,
                "aesthetic": self.aesthetic.key if self.aesthetic else None,
            },
        )
        segments = []
        segment = Segment()
        while rows := list(islice(cursor, 256)):
            pulse()
            for row in rows:
                clock = timestamp(row["captured_at"])
                point = gps(row["latitude"], row["longitude"])
                gap = (clock - segment.last).total_seconds() if clock and segment.last else 0
                if segment.count and (
                    gap > 60 * self.config.story_gap_minutes
                    or (
                        point
                        and segment.anchor
                        and distance(point, segment.anchor) > self.config.story_distance_km
                    )
                    or (clock is None and segment.last is not None)
                ):
                    segments.append(self.summarize(segment))
                    segment = Segment()
                segment.add(row, clock, point)
        if segment.count:
            segments.append(self.summarize(segment))
        with write(self.db):
            if not self.db.execute(
                "DELETE FROM story_dirty WHERE day = ? AND generation = ?", (day, generation)
            ).rowcount:
                return True
            self.db.execute("DELETE FROM story_days WHERE day = ?", (day,))
            if segments:
                self.db.execute(
                    "INSERT INTO story_days VALUES (?, ?, ?, ?)",
                    (day, sum(row[-2] for row in segments), len(segments), key),
                )
                self.db.executemany(
                    "INSERT INTO story_segments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ((day, index, *values) for index, values in enumerate(segments, start=1)),
                )
            mark_catalog_updated(self.db)
        return True

    def summarize(self, segment):
        point = segment.anchor
        return (
            self.places.nearby(point) if point and self.places else None,
            segment.first.isoformat() if segment.first else None,
            segment.last.isoformat() if segment.last else None,
            *(point or (None, None)),
            segment.count,
            json.dumps(segment.covers()),
        )
