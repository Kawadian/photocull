"""Measure real archive planning and concurrent API requests on a generated catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, sleep

from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import ArchiveSettings, Library, Settings
from photocull.db import connect, initialize, write
from photocull.db.archive_preview import build
from photocull.media.walk import source_key

BASE = "/api/libraries/synthetic"


def seed(db_path, source, count):
    content = b"Photocull generated archive fixture\n"
    digest = hashlib.sha256(content).hexdigest()
    with connect(db_path) as db, write(db):
        db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
        for start in range(0, count, 1000):
            folder = str(start // 1000)
            parent = source / folder
            parent.mkdir()
            rows = []
            for n in range(start, min(start + 1000, count)):
                file = parent / f"{n:06d}.arw"
                file.write_bytes(content)
                info = file.stat()
                rows.append(
                    (
                        f"{folder}/{file.name}",
                        folder,
                        len(content),
                        info.st_mtime_ns,
                        digest,
                        source_key(info),
                    )
                )
            db.executemany(
                """INSERT INTO assets(path,folder,kind,size,mtime_ns,content_hash,
                                      source_key,scan_id,day)
                   VALUES(?,?,'raw',?,?,?,?,1,'2020-01-01')""",
                rows,
            )


def milliseconds(call):
    started = perf_counter()
    response = call()
    response.raise_for_status()
    return (perf_counter() - started) * 1000, response.json()


def p95(values):
    values.sort()
    return round(values[max(0, len(values) * 95 // 100 - 1)], 2)


def benchmark(base, count):
    source, output = base / "source", base / "output"
    source.mkdir()
    output.mkdir()
    settings = Settings(
        libraries={"synthetic": Library(root=source)},
        state_dir=base / "state",
        archive=ArchiveSettings(export_roots={"synthetic": output}),
    )
    path = settings.library_state("synthetic") / "index.db"
    initialize(path)
    seed(path, source, count)
    archive = f"{BASE}/archive"
    request = {"operation": "dng", "filters": {"start": "2018-01-01", "end": "2024-12-31"}}
    with TestClient(create_app(settings)) as client:
        submit_ms, queued = milliseconds(lambda: client.post(f"{archive}/preview", json=request))
        endpoint = f"{archive}/previews/{queued['id']}"
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=build, args=(path, settings, "synthetic"))
        started = perf_counter()
        process.start()
        times = {"health": [], "gallery": [], "preview": []}
        preview = {}
        try:
            while True:
                sample, preview = milliseconds(lambda: client.get(endpoint))
                times["preview"].append(sample)
                times["health"].append(milliseconds(lambda: client.get("/api/health"))[0])
                times["gallery"].append(
                    milliseconds(lambda: client.get(f"{BASE}/assets?limit=20"))[0]
                )
                if preview["status"] == "ready":
                    break
                if preview["status"] == "error" or not process.is_alive():
                    raise RuntimeError(preview)
                sleep(0.02)
        finally:
            if process.is_alive() and preview.get("status") != "ready":
                process.terminate()
            process.join(timeout=10)
        elapsed = perf_counter() - started
        assert preview["count"] == count
        assert len(preview["items"]) == min(count, 100)
        confirm_ms, job = milliseconds(
            lambda: client.post(
                f"{archive}/jobs",
                json={"plan": request, "confirmation": preview["confirmation"]},
            )
        )
        assert client.get(f"{archive}/jobs/{job['id']}").json()["counts"] == {"pending": count}
        return {
            "assets": count,
            "cpu_count": len(os.sched_getaffinity(0)),
            "planning_seconds": round(elapsed, 3),
            "queue_ms": round(submit_ms, 2),
            "confirm_ms": round(confirm_ms, 2),
            "requests_per_endpoint": len(times["health"]),
            **{f"{name}_p95_ms": p95(values) for name, values in times.items()},
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=int, default=100_000)
    args = parser.parse_args()
    if args.assets < 1:
        parser.error("--assets must be positive")
    with TemporaryDirectory(prefix="photocull-archive-preview-benchmark-") as root:
        print(json.dumps(benchmark(Path(root), args.assets)))


if __name__ == "__main__":
    main()
