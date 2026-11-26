"""Measure browse, burst refresh, and optional vector retrieval on a synthetic catalog."""

from __future__ import annotations

import argparse
import math
import random
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import InsightSettings, Library, MLSettings, Settings
from photocull.db import connect, initialize, write
from photocull.db.bursts import burst_page_query
from photocull.db.catalog import ScoreFilters, asset_page_query
from photocull.db.insights import register
from photocull.db.transfers import Selection, select_assets
from photocull.insights import models
from photocull.worker.bursts import CANDIDATES_SQL, refresh_bursts
from photocull.worker.insights.story import Stories

BASE = "/api/libraries/benchmark"
PAGE_SIZE = 100
NO_FILTERS = ScoreFilters()
SqlQuery = tuple[str, tuple]


@dataclass(frozen=True)
class Case:
    name: str
    endpoint: str
    params: dict
    query: SqlQuery
    expect_empty: bool = False


def seed(db: sqlite3.Connection, size: int) -> None:
    rng = random.Random(42)
    start = datetime(2025, 1, 1)
    with write(db):
        db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES (1, 'done', 0)")
        for first in range(1, size + 1, 1000):
            assets, scores, jobs = [], [], []
            for asset_id in range(first, min(size + 1, first + 1000)):
                group, frame = divmod(asset_id - 1, 5)
                captured = start + timedelta(seconds=6 * group, milliseconds=250 * frame)
                folder = f"shoots/{group % 20:02}"
                digest = f"{asset_id:064x}"
                assets.append(
                    (
                        asset_id,
                        f"{folder}/{asset_id}.jpg",
                        folder,
                        digest,
                        captured.date().isoformat(),
                        captured.isoformat(),
                    )
                )
                if frame == 0:
                    fingerprint = rng.getrandbits(64)
                reason = "blur" if asset_id % 19 == 0 else None
                scores.append(
                    (
                        asset_id,
                        rng.random(),
                        0.1 + 0.8 * rng.random(),
                        0.02,
                        0.03,
                        f"{fingerprint ^ (1 << frame):016x}",
                        reason,
                    )
                )
                jobs.extend((asset_id, stage, digest) for stage in ("exif", "preview", "score"))
            db.executemany(
                """INSERT INTO assets (id, path, folder, kind, size, mtime_ns, content_hash,
                                       scan_id, day, captured_at)
                   VALUES (?, ?, ?, 'image', 1024, 0, ?, 1, ?, ?)""",
                assets,
            )
            db.executemany(
                """INSERT INTO asset_scores (asset_id, sharpness, exposure, clipped_highlights,
                                             clipped_shadows, dhash, suggested_reject_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                scores,
            )
            db.executemany(
                "INSERT INTO jobs (asset_id, stage, content_hash, status) VALUES (?, ?, ?, 'done')",
                jobs,
            )


def browse_cases(size: int) -> list[Case]:
    mid = size // 2
    mixed = ScoreFilters(min_sharpness=0.65, min_exposure=0.3, max_exposure=0.7, auto_reject=False)
    flagged = ScoreFilters(min_sharpness=0.4, auto_reject=True)
    empty = ScoreFilters(min_exposure=0.95)

    def asset_case(
        name: str,
        scores: ScoreFilters = NO_FILTERS,
        cursor: int = 0,
        expect_empty: bool = False,
        **scope: str | int,
    ) -> Case:
        return Case(
            name,
            "assets",
            {**asdict(scores), **scope, "cursor": cursor},
            asset_page_query(cursor, PAGE_SIZE, scores, **scope),
            expect_empty,
        )

    def burst_case(
        name: str, scores: ScoreFilters, cursor: int = 0, expect_empty: bool = False
    ) -> Case:
        return Case(
            name,
            "bursts",
            {**asdict(scores), "cursor": cursor},
            burst_page_query(cursor, PAGE_SIZE, scores),
            expect_empty,
        )

    return [
        asset_case("assets / first page"),
        asset_case("assets / filtered deep cursor", mixed, mid),
        asset_case("assets / filtered folder", mixed, folder="shoots/03"),
        asset_case("assets / filtered day", mixed, day="2025-01-01"),
        asset_case("assets / no matches", empty, expect_empty=True),
        asset_case("burst members / cursor", cursor=2, burst_id=1),
        burst_case("bursts / deep cursor", ScoreFilters(), mid),
        burst_case("bursts / filtered", flagged),
        burst_case("bursts / no matches", empty, expect_empty=True),
    ]


def measure(client: TestClient, case: Case, repeats: int) -> tuple[float, float]:
    params = {name: value for name, value in case.params.items() if value is not None}
    params["limit"] = PAGE_SIZE
    samples = []
    for sample in range(repeats + 3):
        started = perf_counter()
        response = client.get(f"{BASE}/{case.endpoint}", params=params)
        elapsed = (perf_counter() - started) * 1000
        response.raise_for_status()
        items = response.json()["items"]
        if bool(items) == case.expect_empty:
            raise RuntimeError(f"{case.name} returned {len(items)} records")
        if sample >= 3:
            samples.append(elapsed)
    return percentiles(samples)


def percentiles(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    return ordered[math.ceil(len(ordered) * 0.5) - 1], ordered[math.ceil(len(ordered) * 0.95) - 1]


def measure_vectors(
    db: sqlite3.Connection,
    path: Path,
    settings: MLSettings,
    size: int,
    repeats: int,
) -> None:
    import numpy as np

    from photocull.worker.ml.grouping import SimilarityGroups
    from photocull.worker.ml.vectors import STORAGE_DTYPE, exact_search, normalized

    rng = np.random.default_rng(42)
    centers = normalized(rng.normal(size=(size // 5, 768)))
    key = settings.model_key
    with write(db):
        db.execute(
            "INSERT INTO embedding_models VALUES (?, ?, ?, '{}')",
            (key, settings.model_id, settings.model_revision),
        )
        db.execute("UPDATE ml_state SET enabled = 1, model_key = ?", (key,))
        for first in range(1, size + 1, 512):
            ids = np.arange(first, min(first + 512, size + 1))
            vectors = normalized(
                centers[(ids - 1) % len(centers)] + rng.normal(0, 0.004, (len(ids), 768))
            ).astype(STORAGE_DTYPE)
            db.executemany(
                """INSERT INTO asset_embeddings(asset_id, model_key, content_hash, vector)
                   VALUES (?, ?, ?, ?)""",
                (
                    (i, key, f"{i:064x}", v.tobytes())
                    for i, v in zip(map(int, ids), vectors, strict=True)
                ),
            )
    started = perf_counter()
    group_index = SimilarityGroups(path, key, settings.similar_threshold)
    while group_index.step(db):
        pass
    groups = db.execute("SELECT count(*) FROM similar_groups").fetchone()[0]
    complete = db.execute("SELECT count(*) FROM similar_groups WHERE member_count = 5").fetchone()[
        0
    ]
    print(f"Similar groups: {groups:,} in {perf_counter() - started:.2f} s (HNSW, 768-D)")
    if size % 5 == 0 and complete < 0.98 * len(centers):
        raise RuntimeError(f"Only {complete:,} complete 5-photo groups out of {len(centers):,}")
    print(f"Complete 5-photo groups: {complete:,} / {len(centers):,} planted")
    samples = []
    for sample in range(repeats + 3):
        center = sample % len(centers)
        started = perf_counter()
        hits = exact_search(db, key, centers[center], 60)
        if sample >= 3:
            samples.append((perf_counter() - started) * 1000)
        if not hits or (hits[0][0] - 1) % len(centers) != center:
            raise RuntimeError("Nearest vector did not match its planted embedding cluster")
    p50, p95 = percentiles(samples)
    print(f"Exact cosine search (top 60)      p50 {p50:7.2f} ms  p95 {p95:7.2f} ms")

    def append(asset_id: int, vector) -> None:
        with write(db):
            db.execute(
                """INSERT INTO assets(id, path, folder, kind, size, mtime_ns, content_hash,
                                      scan_id, day)
                   VALUES (?, ?, '', 'image', 1, 0, ?, 1, '2025-01-01')""",
                (asset_id, f"{asset_id}.jpg", f"{asset_id:064x}"),
            )
            db.execute(
                "INSERT INTO asset_embeddings(asset_id, model_key, content_hash, vector) "
                "VALUES (?, ?, ?, ?)",
                (asset_id, key, f"{asset_id:064x}", vector.astype(STORAGE_DTYPE).tobytes()),
            )

    append(size + 1, centers[0])
    started = perf_counter()
    group_index = SimilarityGroups(path, key, settings.similar_threshold)
    while group_index.step(db):
        pass
    matched = db.execute(
        "SELECT group_id FROM similar_members WHERE asset_id = ?", (size + 1,)
    ).fetchone()
    if not matched or (matched[0] - 1) % len(centers) != 0:
        raise RuntimeError("Incremental grouping missed the planted anchor")
    print(f"Append matching photo after restart: {(perf_counter() - started) * 1000:.2f} ms")
    append(size + 2, normalized(rng.normal(size=768)))
    started = perf_counter()
    while group_index.step(db):
        pass
    assert db.execute("SELECT 1 FROM similar_anchors WHERE asset_id = ?", (size + 2,)).fetchone()
    print(f"Append a new anchor and checkpoint: {(perf_counter() - started) * 1000:.2f} ms")
    checkpoint_bytes = sum(file.stat().st_size for file in path.parent.glob("*.usearch"))
    print(f"Anchor checkpoint: {checkpoint_bytes / 2**20:.1f} MiB")


def seed_insights(db, settings, size):
    identities = models(settings)
    state = SimpleNamespace(
        db=db, settings=settings, insights=SimpleNamespace(identities=identities)
    )
    with write(db):
        key = register(db, identities["aesthetic"])
        face = register(db, identities["faces"])
        db.execute(
            """INSERT INTO insight_results(stage, asset_id, key, content_hash, score)
               SELECT 'aesthetic', id, ?, content_hash, (id % 10001) / 10000.0 FROM assets""",
            (key,),
        )
        db.executemany("INSERT INTO people (id) VALUES (?)", ((group,) for group in range(1, 1201)))
        db.execute(
            """INSERT INTO faces(asset_id, person_id, key, content_hash, left, top, right,
                                 bottom, confidence, quality, vector)
               SELECT id, (id % 1200) + 1, ?, content_hash, 0.2, 0.2, 0.7, 0.7,
                      0.98, 0.93, zeroblob(256) FROM assets""",
            (face,),
        )
    story = Stories(state)

    def pulse():
        pass

    start = perf_counter()
    story.configure(pulse)
    while story.step(pulse):
        pass
    print(f"Full story build: {perf_counter() - start:,.2f} s")
    query, arguments = select_assets(Selection(minimum_rating=0, person_id=2), {"faces": face})
    start = perf_counter()
    assert len(db.execute(query, arguments).fetchall()) >= 1
    print(f"Keeper selection by person: {(perf_counter() - start) * 1000:,.2f} ms")

    deep = min(8500, size // 2)
    previous = (deep % 10001) / 10000
    return [
        Case(
            "aesthetic / deep cursor",
            "assets",
            {"sort": "aesthetic", "cursor": deep, "min_aesthetic": 0.01},
            asset_page_query(
                deep,
                PAGE_SIZE,
                NO_FILTERS,
                sort_score=("aesthetic", key, previous),
                aesthetic_key=key,
                min_aesthetic=0.01,
            ),
        ),
        Case(
            "people / first page",
            "insights/people",
            {},
            ("SELECT id FROM people ORDER BY id LIMIT ?", (PAGE_SIZE,)),
        ),
        Case(
            "person assets / cursor",
            "assets",
            {"person_id": 2},
            asset_page_query(0, PAGE_SIZE, NO_FILTERS, person_id=2, face_key=face),
        ),
        Case(
            "stories / days",
            "insights/stories",
            {},
            (
                """SELECT d.day FROM story_days AS d JOIN story_state AS s USING(key)
                 WHERE d.day < ? ORDER BY d.day DESC LIMIT ?""",
                ("9999-99-99", PAGE_SIZE),
            ),
        ),
        Case(
            "stories / stops",
            "insights/stories/2025-01-01",
            {},
            (
                """SELECT * FROM story_segments WHERE day=? AND position > ?
                 ORDER BY position LIMIT ?""",
                ("2025-01-01", 0, PAGE_SIZE),
            ),
        ),
    ]


def run(assets: int, repeats: int, budget: float, ml: bool = False, insights: bool = False) -> bool:
    with TemporaryDirectory(prefix="photocull-benchmark-") as directory:
        root = Path(directory)
        settings = Settings(
            libraries={"benchmark": Library(root=root / "media")},
            state_dir=root / "state",
            ml=MLSettings(enabled=ml or insights),
            insights=InsightSettings(aesthetic=insights, faces=insights, story=insights),
        )
        path = settings.library_state("benchmark") / "index.db"
        initialize(path)
        print(f"SQLite {sqlite3.sqlite_version}, {assets:,} assets, {repeats} requests per case")
        with connect(path) as db:
            seed(db, assets)
            start = perf_counter()
            refresh_bursts(db, settings.burst, lambda: None)
            print(f"Full group build: {(perf_counter() - start) * 1000:,.1f} ms")
            print(f"Bursts: {db.execute('SELECT count(*) FROM bursts').fetchone()[0]:,}")
            db.execute("UPDATE asset_scores SET sharpness = 1 WHERE asset_id = 2")
            start = perf_counter()
            refresh_bursts(db, settings.burst, lambda: None)
            print(f"One-photo group update: {(perf_counter() - start) * 1000:,.2f} ms")
            plans = db.execute(
                "EXPLAIN QUERY PLAN " + CANDIDATES_SQL,
                {
                    "start": "",
                    "end": "\uffff",
                    "after_time": "",
                    "after_id": 0,
                    "model_key": None,
                    "aesthetic_key": None,
                    "weight": 0,
                },
            )
            print("Grouping plan: " + " | ".join(row["detail"] for row in plans))
            if ml:
                measure_vectors(db, path, settings.ml, assets, repeats)
            insight_cases = seed_insights(db, settings, assets) if insights else []
        print(f"Catalog footprint: {path.stat().st_size / 2**20:.1f} MiB (SQLite file)")

        passed = True
        with TestClient(create_app(settings)) as client, connect(path) as db:
            cases = browse_cases(assets) + insight_cases
            if ml:
                cases.extend(
                    [
                        Case(
                            "similar groups / first page",
                            "similar-groups",
                            {},
                            (
                                "SELECT id FROM similar_groups WHERE id > ? ORDER BY id LIMIT ?",
                                (0, PAGE_SIZE),
                            ),
                        ),
                        Case(
                            "similar members / cursor",
                            "assets",
                            {"similar_group_id": 1, "cursor": 1},
                            asset_page_query(1, PAGE_SIZE, NO_FILTERS, similar_group_id=1),
                        ),
                    ]
                )
            for case in cases:
                sql, params = case.query
                plan = db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
                p50, p95 = measure(client, case, repeats)
                print(f"{case.name:34s} p50 {p50:7.2f} ms  p95 {p95:7.2f} ms")
                print("    " + " | ".join(row["detail"] for row in plan))
                passed = passed and p95 < budget
        return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=int, default=50_000)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--max-p95-ms", type=float, default=200)
    parser.add_argument(
        "--ml", action="store_true", help="include 768-D cosine search and similarity groups"
    )
    parser.add_argument(
        "--insights", action="store_true", help="include aesthetic ranks, people and day stories"
    )
    args = parser.parse_args()
    if args.assets < 1000 or args.requests < 1 or args.max_p95_ms <= 0:
        parser.error("Use at least 1000 assets, one request and a positive latency budget.")
    return 0 if run(args.assets, args.requests, args.max_p95_ms, args.ml, args.insights) else 1


if __name__ == "__main__":
    raise SystemExit(main())
