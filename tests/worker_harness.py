"""Test helpers for running the real scheduler behind the API."""

from contextlib import contextmanager
from multiprocessing import get_context
from shutil import copyfile
from time import monotonic, sleep

from PIL import Image

from photocull.db import connect, write
from photocull.media.cache import cache_path
from photocull.worker import run


def photo(path, color, stamp, *, gradient=False):
    exif = Image.Exif()
    exif[34665] = {36867: f"2025:01:{stamp}"}
    image = Image.new("RGB", (80, 50), color)
    if gradient:
        image.putdata([(180 + x, y, y) for y in range(50) for x in reversed(range(80))])
    with image:
        image.save(path, exif=exif)


@contextmanager
def start_worker(settings, **options):
    context = get_context("spawn")
    stop = context.Event()
    worker = context.Process(target=run, args=(settings, stop), kwargs=options)
    worker.start()
    try:
        yield worker
    finally:
        stop.set()
        worker.join(4)
        if worker.is_alive():
            worker.kill()
            worker.join()
        worker.close()


def wait_for(check, *, timeout=10, interval=0.05):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if value := check():
            return value
        sleep(interval)
    raise AssertionError("Condition did not become ready")


def append_cached_previews(state, count):
    """Seed real preview jobs without hashing duplicate fixture photos."""
    with connect(state / "index.db") as db:
        sources = db.execute(
            "SELECT asset_id, content_hash FROM asset_embeddings ORDER BY asset_id"
        ).fetchall()
        model = db.execute("SELECT model_key FROM ml_state").fetchone()[0]
        scan = db.execute("SELECT max(id) FROM scan_requests").fetchone()[0]
        first = db.execute("SELECT max(id) + 1 FROM assets").fetchone()[0]
        assets = []
        for asset_id in range(first, first + count):
            source_id, source_hash = sources[(asset_id - first) % len(sources)]
            digest = f"{asset_id:064x}"
            assets.append((asset_id, f"z-pending-{asset_id}.jpg", digest, scan))
            source = cache_path(state, "previews", source_id, source_hash)
            copyfile(source, cache_path(state, "previews", asset_id, digest))
        with write(db):
            db.executemany(
                """INSERT INTO assets(id, path, folder, kind, size, mtime_ns,
                                      content_hash, scan_id, day)
                   VALUES (?, ?, '', 'image', 1, 0, ?, ?, '2025-01-01')""",
                assets,
            )
            db.executemany(
                """INSERT INTO jobs(asset_id, stage, content_hash, status, model_key) VALUES
                   (?, 'preview', ?, 'done', NULL), (?, 'embedding', ?, 'pending', ?)""",
                ((asset_id, digest, asset_id, digest, model) for asset_id, _, digest, _ in assets),
            )
