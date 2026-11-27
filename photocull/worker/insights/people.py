from __future__ import annotations

from contextlib import closing
from itertools import islice

from photocull.db import write
from photocull.db.insights import finish

MIN_QUALITY = 0.12
INDEX_BATCH = 512


def overlap(a, b) -> float:
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    return intersection / max(
        1e-9, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    )


def match_boxes(prior, detections, key):
    # Prefer a user's assignments if several old boxes overlap a new detection.
    overlaps = sorted(
        (
            (bool(row["manual"] or row["name"]), overlap(box, row[3:7]), index, row)
            for index, (box, *_) in enumerate(detections)
            for row in prior
        ),
        key=lambda item: (item[0], item[1], -item[2], -item[3]["id"]),
        reverse=True,
    )
    matched, used = {}, set()
    for manual, iou, index, row in overlaps:
        if iou >= 0.55 and index not in matched and row["id"] not in used:
            person = row["person_id"] if manual or row["key"] == key else None
            matched[index] = row["id"], person, row["manual"] if person is not None else 0
            used.add(row["id"])
    return matched, used


class FaceGroups:
    def __init__(self):
        self.index = None
        self.key = None
        self.removed = 0

    def _add(self, rows):
        import numpy as np

        if rows:
            self.index.add(
                np.array([row["id"] for row in rows], dtype=np.uint64),
                np.frombuffer(b"".join(row["vector"] for row in rows), dtype="<f2").reshape(
                    -1, 128
                ),
                threads=1,
            )

    def prepare(self, db, key, pulse):
        from usearch.index import Index

        rebuild = (
            self.index is None or key != self.key or self.removed >= max(1024, len(self.index) // 4)
        )
        try:
            if rebuild:
                self.index = Index(ndim=128, metric="cos", dtype="f16")
                self.key, self.removed = key, 0
                with closing(
                    db.execute(
                        """SELECT f.id, f.vector FROM faces AS f
                           JOIN assets AS a ON a.id = f.asset_id
                           WHERE f.key = ? AND a.active = 1 AND f.quality >= ?
                             AND f.content_hash = a.content_hash ORDER BY f.id""",
                        (key, MIN_QUALITY),
                    )
                ) as rows:
                    while batch := list(islice(rows, INDEX_BATCH)):
                        pulse()
                        self._add(batch)
                db.execute("DELETE FROM face_index_dirty")
                return
            while batch := db.execute(
                """SELECT d.id, CASE WHEN a.active=1 AND f.content_hash=a.content_hash
                                      THEN f.vector END AS vector
                   FROM face_index_dirty AS d LEFT JOIN faces AS f
                     ON f.id = d.id AND f.key = ? AND f.quality >= ?
                   LEFT JOIN assets AS a ON a.id = f.asset_id
                   ORDER BY d.id LIMIT ?""",
                (key, MIN_QUALITY, INDEX_BATCH),
            ).fetchall():
                pulse()
                for row in batch:
                    self.removed += self.index.remove(row["id"], threads=1)
                self._add([row for row in batch if row["vector"] is not None])
                db.execute("DELETE FROM face_index_dirty WHERE id <= ?", (batch[-1]["id"],))
        except BaseException:
            self.index = None
            raise

    def nearest(self, db, asset_id, vector, threshold, reserved):
        import numpy as np

        if not len(self.index):
            return None
        hits = self.index.search(
            np.frombuffer(vector, dtype="<f2"), count=24 + len(reserved), threads=1
        )
        for hit in hits:
            if hit.distance > 1 - threshold:
                break
            row = db.execute(
                """SELECT f.person_id FROM faces AS f JOIN assets AS a ON a.id = f.asset_id
                   WHERE f.id = ? AND f.asset_id <> ? AND a.active = 1
                     AND f.content_hash=a.content_hash""",
                (int(hit.key), asset_id),
            ).fetchone()
            if row and row[0] not in reserved:
                return row[0]
        return None

    def save(self, db, key, threshold, asset, detections, pulse):
        asset_id = asset["id"]
        # An empty detection still replaces obsolete boxes. Only a photo that can
        # join an existing group needs the nearest-neighbor index.
        if any(quality >= MIN_QUALITY for _, _, quality, _ in detections):
            self.prepare(db, key, pulse)
        with write(db):
            prior = db.execute(
                """SELECT f.id, f.person_id, f.manual, f.left, f.top, f.right, f.bottom,
                          f.key, p.name
                   FROM faces AS f JOIN people AS p ON p.id = f.person_id WHERE f.asset_id = ?""",
                (asset_id,),
            ).fetchall()
            # Read assignments after acquiring the writer lock. A rename, split,
            # or merge committed during detection is authoritative here.
            matched, used = match_boxes(prior, detections, key)
            reserved = {person for _, person, _ in matched.values() if person is not None}
            db.executemany(
                "DELETE FROM faces WHERE id = ?", ((r["id"],) for r in prior if r["id"] not in used)
            )
            for index, (box, confidence, quality, vector) in enumerate(detections):
                face, person, manual = matched.get(index, (None, None, 0))
                if person is None and quality >= MIN_QUALITY:
                    person = self.nearest(db, asset_id, vector, threshold, reserved)
                if person is None:
                    person = db.execute(
                        "INSERT INTO people DEFAULT VALUES RETURNING id"
                    ).fetchone()[0]
                reserved.add(person)
                db.execute(
                    """INSERT INTO faces(id, asset_id, person_id, key, content_hash, left, top,
                                         right, bottom, confidence, quality, vector, manual)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET person_id = excluded.person_id,
                           key = excluded.key, content_hash=excluded.content_hash,
                           left = excluded.left, top = excluded.top,
                           right = excluded.right, bottom = excluded.bottom,
                           confidence = excluded.confidence, quality = excluded.quality,
                           vector = excluded.vector, manual = excluded.manual""",
                    (
                        face,
                        asset_id,
                        person,
                        key,
                        asset["content_hash"],
                        *box,
                        confidence,
                        quality,
                        vector,
                        manual,
                    ),
                )
            db.executemany(
                """DELETE FROM people WHERE id = ? AND name IS NULL
                   AND NOT EXISTS(SELECT 1 FROM faces WHERE person_id = people.id)""",
                ((person,) for person in {row["person_id"] for row in prior} - reserved),
            )
            finish(db, "faces", key, asset, payload={"count": len(detections)})
