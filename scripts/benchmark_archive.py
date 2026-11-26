"""Benchmark archive jobs using generated media, with one process per codec."""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter

from photocull.archive import ArchiveRequest, packed, preview
from photocull.config import ArchiveSettings, Library, Settings, WorkerSettings
from photocull.db import connect, initialize
from photocull.db.archive import enqueue, jobs
from photocull.db.jobs import request_scan
from photocull.media.walk import MediaJail
from photocull.worker.archive import ArchiveWork
from photocull.worker.scan import scan_batches
from tests.archive_fixtures import synthetic_dng, synthetic_image, synthetic_video

FORMATS = {
    "dng": ("raw.dng", "raw.dng"),
    "jpeg": ("still.tiff", "still.jpg"),
    "heic": ("still.tiff", "still.heic"),
    "avif": ("still.tiff", "still.avif"),
    "hevc": ("clip.mp4", "clip.mp4"),
    "av1": ("clip.mp4", "clip.mp4"),
}


def run_case(base, codec):
    source, output = FORMATS[codec]
    library = Library(root=base / "input")
    export = base / "output"
    export.mkdir(exist_ok=True)
    settings = Settings(
        libraries={"test": library},
        state_dir=base / "state",
        worker=WorkerSettings(stages=()),
        archive=ArchiveSettings(export_roots={"test": export}),
    )
    work = ArchiveWork(settings, "test", Event())
    initialize(work.db_path)
    with connect(work.db_path) as db:
        scan = request_scan(db)["id"]
        for _ in scan_batches(
            db, scan, MediaJail(library, settings.state_dir), settings.worker, lambda: None
        ):
            pass
        task = ArchiveRequest(operation=codec, filters={"extensions": [Path(source).suffix[1:]]})
        plan = preview(db, settings, "test", task)
        id_ = enqueue(db, settings, "test", task, plan["confirmation"], "synthetic-benchmark")
        row = work.claim(db, 1)[0]
        started = perf_counter()
        work.process(row)
        seconds = perf_counter() - started
        work.settle(db)
        result = jobs(db, id_)
        if result["counts"] != {"done": 1}:
            raise RuntimeError(packed(result))
    origin, converted = (library.root / source).stat().st_size, (export / output).stat().st_size
    return {
        "codec": codec,
        "seconds": round(seconds, 3),
        "source_bytes": origin,
        "output_bytes": converted,
        "reduction_percent": round(100 * (origin - converted) / origin, 2),
        "peak_child_rss_mib": round(
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1
        ),
        "peak_worker_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--case", choices=tuple(FORMATS))
    parser.add_argument("--base", type=Path)
    args = parser.parse_args()
    if args.case:
        print(packed(run_case(args.base, args.case)))
        return
    with TemporaryDirectory(prefix="photocull-archive-benchmark-") as directory:
        base = Path(directory)
        inputs = base / "input"
        inputs.mkdir()
        if args.smoke:
            synthetic_dng(inputs / "raw.dng")
            synthetic_image(inputs / "still.tiff")
            synthetic_video(inputs / "clip.mp4")
        else:
            synthetic_dng(inputs / "raw.dng", 3072, 2048)
            synthetic_image(inputs / "still.tiff", (3072, 2048))
            synthetic_video(inputs / "clip.mp4", "1280x720", 8)
        for codec in FORMATS:
            result = subprocess.check_output(
                [sys.executable, __file__, "--case", codec, "--base", os.fspath(base)],
                text=True,
            )
            print(json.dumps(json.loads(result), ensure_ascii=False), flush=True)
            (base / "output" / FORMATS[codec][1]).unlink()


if __name__ == "__main__":
    main()
