from __future__ import annotations

from itertools import islice

from photocull.db import write
from photocull.db.insights import finish

COLUMNS = "left, top, right, bottom"


def overlap(a, b) -> float:
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    return intersection / max(
        1e-9, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    )


class FaceGroups:
    def __init__(self):
        self.index = None
        self.key = None
        self.last_id = 0

    def prepare(self, db, key, pulse):
        import numpy as np
        from usearch.index import Index

        if key != self.key:
            self.key, self.index, self.last_id = key, Index(ndim=128, metric="cos", dtype="f16"), 0
        source = db.execute(
            """SELECT f.id, f.vector FROM faces AS f JOIN assets AS a ON a.id = f.asset_id
               WHERE f.key = ? AND f.id > ? AND a.active = 1 AND f.quality >= 0.12 ORDER BY f.id""",
            (key, self.last_id),
        )
        while rows := list(islice(source, 512)):
            pulse()
            self.index.add(
                np.array([r["id"] for r in rows], dtype=np.uint64),
                np.frombuffer(b"".join(r["vector"] for r in rows), dtype="<f2").reshape(-1, 128),
                threads=1,
            )
            self.last_id = rows[-1]["id"]

    def save(self, db, key, threshold, asset, detections, pulse):
        import numpy as np

        self.prepare(db, key, pulse)
        with write(db):
            prior = db.execute(
                f"""SELECT f.id, f.person_id, f.manual, {COLUMNS}, f.key, p.name
                    FROM faces AS f JOIN people AS p ON p.id = f.person_id WHERE f.asset_id = ?""",
                (asset["id"],),
            ).fetchall()
            db.execute("DELETE FROM faces WHERE asset_id = ?", (asset["id"],))
            reserved = set()
            # Preserve deliberate assignments first, including on a model upgrade.
            overlaps = sorted(
                (
                    (row["manual"], overlap(box, row[3:7]), index, row["id"], row["person_id"])
                    for index, (box, *_) in enumerate(detections)
                    for row in prior
                    if row["manual"] or row["name"] or row["key"] == key
                ),
                reverse=True,
            )
            matched, used = {}, set()
            for manual, iou, index, face, person in overlaps:
                if iou >= 0.55 and index not in matched and face not in used:
                    matched[index] = person, manual
                    used.add(face)
                    reserved.add(person)
            for index, (box, confidence, quality, vector) in enumerate(detections):
                person, manual = matched.get(index, (None, 0))
                if person is None and quality >= 0.12 and len(self.index):
                    hits = self.index.search(np.frombuffer(vector, dtype="<f2"), count=24)
                    for hit in hits:
                        if hit.distance > 1 - threshold:
                            break
                        row = db.execute(
                            """SELECT f.person_id FROM faces AS f
                               JOIN assets AS a ON a.id=f.asset_id
                               WHERE f.id = ? AND a.active = 1 AND f.quality >= 0.12""",
                            (int(hit.key),),
                        ).fetchone()
                        if row and row[0] not in reserved:
                            person = row[0]
                            break
                if person is None:
                    person = db.execute(
                        "INSERT INTO people DEFAULT VALUES RETURNING id"
                    ).fetchone()[0]
                reserved.add(person)
                db.execute(
                    """INSERT INTO faces(asset_id, person_id, key, content_hash, left, top, right,
                                         bottom, confidence, quality, vector, manual)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        asset["id"],
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
            finish(db, "faces", key, asset, payload={"count": len(detections)})
