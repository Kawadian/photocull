"""Check cross-library timeline latency with a generated SQLite catalog."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from statistics import quantiles
from tempfile import TemporaryDirectory
from time import perf_counter

from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import Library, Settings
from photocull.db import connect, initialize, write

TIMELINE = "/api/browse/timeline"


def seed(path: Path, size: int) -> None:
    first = date(2018, 1, 1)
    days = [(first + timedelta(days=i)).isoformat() for i in range(9 * 365)]

    def entries():
        for index in range(size):
            day = days[(index * 5) % len(days)]
            yield f"{day}/{index:08}.jpg", day, day + "T14:30:00", "a" * 64

    initialize(path)
    with connect(path) as db, write(db):
        db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
        db.executemany(
            """INSERT INTO assets(path,folder,kind,size,mtime_ns,scan_id,
                                   day,captured_at,content_hash,width,height)
               VALUES(?,'photos','image',10000,0,1,?,?,?,1500,1000)""",
            entries(),
        )
        db.execute("""
            INSERT INTO jobs(asset_id,stage,content_hash,status)
            SELECT id,'preview',content_hash,'done' FROM assets
        """)


def sample(client: TestClient, path: str, params: dict) -> tuple[float, dict]:
    start = perf_counter()
    response = client.get(path, params=params)
    response.raise_for_status()
    data = response.json()
    return (perf_counter() - start) * 1_000, data


def benchmark(root: Path, count: int, iterations: int) -> dict[str, float]:
    libraries = {str(year): Library(root=root / str(year)) for year in range(2018, 2027)}
    settings = Settings(libraries=libraries, state_dir=root / "state")
    size, extra = divmod(count, len(libraries))
    for index, name in enumerate(libraries):
        seed(settings.library_state(name) / "index.db", size + (index < extra))

    with TestClient(create_app(settings)) as client:
        _, calendar = sample(client, "/api/browse/months", {})
        months = calendar["items"]
        if len(months) < 4:
            raise ValueError("Catalog must contain at least four capture months")
        anchors = [{}, {"month": months[len(months) // 2]}, {"month": months[-3]}]
        cases = {}
        for anchor in anchors:
            params = anchor.copy()
            for _ in range(5):
                _, page = sample(client, TIMELINE, params)
                if not page["next_cursor"]:
                    break
                params["cursor"] = page["next_cursor"]
            if "cursor" not in params:
                raise ValueError("Fixture has too few rows to measure cursor performance")
            cases.update(
                {
                    ("jump_" if anchor else "first_") + str(len(cases)): anchor,
                    "cursor_" + str(len(cases)): params,
                }
            )

        timings = {name: [] for name in cases}
        calendar_times = []
        for _ in range(iterations):
            for name, params in cases.items():
                elapsed, page = sample(client, TIMELINE, params)
                if not page["items"]:
                    raise AssertionError(params)
                timings[name].append(elapsed)
            calendar_times.append(sample(client, "/api/browse/months", {})[0])

    def p95(samples):
        return round(quantiles(samples, n=100, method="inclusive")[94], 2)

    return {
        "assets": count,
        "libraries": len(libraries),
        "requests": sum(map(len, timings.values())),
        "timeline_p95_ms": p95([time for times in timings.values() for time in times]),
        "month_list_p95_ms": p95(calendar_times),
        **{name + "_p95_ms": p95(times) for name, times in timings.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--budget-ms", type=float, default=100)
    args = parser.parse_args()
    if args.assets < 3_000 or args.iterations < 2:
        parser.error("Use at least 3,000 assets and two iterations")
    with TemporaryDirectory(prefix="photocull-browser-benchmark-") as directory:
        result = benchmark(Path(directory), args.assets, args.iterations)
    print(json.dumps(result))
    if result["timeline_p95_ms"] >= args.budget_ms:
        raise SystemExit(f"Timeline p95 exceeded {args.budget_ms:g} ms")


if __name__ == "__main__":
    main()
