"""Time the real indexing worker on synthetic, uncompressed TIFF libraries."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from PIL import Image

WORKER = """\
from threading import Event
from photocull.config import load_settings
from photocull.worker import run
import signal
stop = Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
raise SystemExit(run(load_settings(), stop))
"""


def measure(args, checkout, directory, *, parallelism):
    state = directory / "state"
    config = directory / "config.json"
    sources = [directory.parent / f"source-{i}" for i in range(args.libraries)]
    if args.cold_source:
        for source in sources:
            for path in source.iterdir():
                with path.open("rb") as media:
                    os.posix_fadvise(media.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("PHOTOCULL_")},
        "PHOTOCULL_CONFIG": str(config),
        "PYTHONPATH": str(checkout),
    }
    config.write_text(
        json.dumps(
            {
                "state_dir": str(state),
                "worker": {"parallelism": parallelism, "memory_mb": 3072},
                "libraries": {
                    f"library-{i}": {"root": str(source)} for i, source in enumerate(sources)
                },
            }
        )
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from photocull.config import load_settings
from photocull.db import connect, initialize
from photocull.db.jobs import request_scan
settings = load_settings()
for name in settings.libraries:
    path = settings.library_state(name) / "index.db"
    initialize(path)
    with connect(path) as db:
        request_scan(db)
""",
        ],
        env=env,
        cwd=directory,
        check=True,
    )
    from photocull.db import connect
    from photocull.db.jobs import progress

    paths = sorted(state.glob("libraries/*/index.db"))
    assert len(paths) == args.libraries
    start = monotonic()
    scan_seconds = None
    with subprocess.Popen([sys.executable, "-c", WORKER], env=env, cwd=directory) as worker:
        try:
            while monotonic() - start < args.timeout:
                reports = []
                for path in paths:
                    with connect(path) as db:
                        reports.append(progress(db))
                if worker.poll() is not None:
                    raise RuntimeError(f"Worker exited with status {worker.returncode}")
                if any(p["scan"]["status"] == "error" for p in reports):
                    raise RuntimeError(f"Scan failed: {reports}")
                if all(p["scan"]["status"] == "done" for p in reports):
                    scan_seconds = scan_seconds or monotonic() - start
                    if all(
                        stage.get("done") == args.files
                        for p in reports
                        for stage in (p["stages"][s] for s in ("exif", "preview", "score"))
                    ):
                        return scan_seconds, monotonic() - start
                    if any(stage.get("error") for p in reports for stage in p["stages"].values()):
                        raise RuntimeError(f"Stage failed: {reports}")
                sleep(0.025)
            raise TimeoutError("Synthetic ingest did not finish")
        finally:
            if worker.poll() is None:
                worker.terminate()
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, help="Filesystem for fixtures and state")
    parser.add_argument("--baseline", type=Path, help="Checkout of the baseline revision")
    parser.add_argument("--libraries", type=int, default=8)
    parser.add_argument("--files", type=int, default=24, help="Files per library")
    parser.add_argument("--width", type=int, default=3072)
    parser.add_argument("--height", type=int, default=2048)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--parallelism", type=int, default=2)
    parser.add_argument(
        "--cold-source",
        action="store_true",
        help="Ask Linux to evict fixture pages before each run",
    )
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[1]
    cases = [("current", checkout, 1)]
    if args.baseline:
        cases.insert(0, ("baseline", args.baseline.resolve(), 1))
    if args.parallelism != 1:
        if args.baseline:
            cases.append(("baseline tuned", args.baseline.resolve(), args.parallelism))
        cases.append(("current tuned", checkout, args.parallelism))
    with TemporaryDirectory(prefix="photocull-ingest-", dir=args.directory) as scratch:
        root = Path(scratch).resolve()
        size = 0
        tags = Image.Exif()
        tags[306] = "2023:05:10 08:00:00"
        with Image.linear_gradient("L").resize((args.width, args.height)).convert("RGB") as image:
            for library in range(args.libraries):
                source = root / f"source-{library}"
                source.mkdir()
                for index in range(args.files):
                    target = source / f"frame-{index:05}.tiff"
                    image.putpixel((index % args.width, 0), (index % 256, library % 256, 255))
                    image.save(target, exif=tags, compression="raw")
                    if args.cold_source:
                        with target.open("rb") as media:
                            os.fsync(media.fileno())
                    size += target.stat().st_size
        count = args.libraries * args.files
        print(f"{count} TIFFs, {size / 2**30:.2f} GiB in {args.libraries} libraries", flush=True)
        for attempt in range(args.repeat):
            for index, (label, source, parallelism) in enumerate(
                cases if attempt % 2 == 0 else reversed(cases)
            ):
                directory = root / f"run-{attempt}-{index}"
                directory.mkdir()
                scan, full = measure(args, source, directory, parallelism=parallelism)
                minutes = full * 10000 / count / 60
                print(
                    f"{label}, stage workers={parallelism}: scan {scan:.2f}s, total {full:.2f}s, "
                    f"scan {size / 2**20 / scan:.1f} MiB/s, 10k files {minutes:.1f} min",
                    flush=True,
                )


if __name__ == "__main__":
    main()
