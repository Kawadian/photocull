from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from photocull.config import Library, Settings, WorkerSettings
from photocull.db import connect, initialize
from photocull.db.jobs import claim_jobs, progress, recover, request_scan, retry_job
from photocull.media.cache import cache_path
from photocull.worker import LibraryWorker
from photocull.worker import scan as scanner


def write_photo(path: Path, color: str) -> None:
    tags = Image.Exif()
    tags[306] = "2024:01:02 03:04:05"
    Image.new("RGB", (24, 16), color).save(path, exif=tags)


def run_queue(worker, pool):
    for _ in range(30):
        if not worker.step(pool, worker.beat):
            return
    raise AssertionError("Queue did not finish")


def test_scan_and_stage_resume_preserve_completed_work(tmp_path, monkeypatch):
    library = Library(root=tmp_path / "view")
    library.root.mkdir()
    write_photo(library.root / "a.jpg", "blue")
    write_photo(library.root / "b.jpg", "gray")
    settings = Settings(
        libraries={"photos": library},
        state_dir=tmp_path / "state",
        worker=WorkerSettings(batch_size=1),
    )
    state = settings.library_state("photos")
    path = state / "index.db"
    initialize(path)

    with connect(path) as db, ThreadPoolExecutor(1) as pool:
        scan_id = request_scan(db)["id"]
        assert request_scan(db)["id"] == scan_id
        worker = LibraryWorker(db, library, state, settings, lambda: None)
        assert worker.step(pool, worker.beat)
        assert progress(db)["scan"]["status"] == "running"
        assert progress(db)["stages"]["exif"] == {"done": 1}
        interrupted = claim_jobs(db, 1)[0]
        assert interrupted.stage == "preview"
        assert progress(db)["stages"]["preview"] == {"running": 1}

    original_hash = scanner.hash_file
    hashed = []

    def hash_file(jail, entry, pulse):
        hashed.append(entry.path)
        return original_hash(jail, entry, pulse)

    monkeypatch.setattr(scanner, "hash_file", hash_file)
    with connect(path) as db, ThreadPoolExecutor(1) as pool:
        recover(db)
        worker = LibraryWorker(db, library, state, settings, lambda: None)
        run_queue(worker, pool)
        assert progress(db)["scan"]["id"] == scan_id
        assert progress(db)["scan"]["status"] == "done"
        assert progress(db)["stages"] == {"exif": {"done": 2}, "preview": {"done": 2}}
        assert progress(db)["worker"]["heartbeat"] > 0
        assert hashed == ["b.jpg"]
        attempts = db.execute(
            "SELECT stage, attempts FROM jobs WHERE asset_id = ? ORDER BY stage",
            (interrupted.asset_id,),
        )
        assert [tuple(row) for row in attempts] == [("exif", 1), ("preview", 2)]
        preview = cache_path(state, "previews", interrupted.asset_id, interrupted.content_hash)
        assert preview.is_file()
        assert db.execute("SELECT day FROM assets WHERE id = 1").fetchone()["day"] == "2024-01-02"

        request_scan(db)
        run_queue(worker, pool)
        assert hashed == ["b.jpg"]
        assert retry_job(db, interrupted.asset_id, "exif")["status"] == "pending"
        run_queue(worker, pool)
        assert (
            db.execute(
                "SELECT attempts FROM jobs WHERE asset_id = 1 AND stage = 'exif'"
            ).fetchone()["attempts"]
            == 2
        )

        write_photo(library.root / "a.jpg", "green")
        (library.root / "b.jpg").unlink()
        request_scan(db)
        run_queue(worker, pool)
        assert hashed == ["b.jpg", "a.jpg"]
        assert not preview.exists()
        assert db.execute("SELECT active FROM assets WHERE path = 'b.jpg'").fetchone()[0] == 0
        assert progress(db)["stages"] == {"exif": {"done": 1}, "preview": {"done": 1}}


def test_worker_stage_configuration_requeues_only_disabled_jobs(tmp_path):
    library = Library(root=tmp_path)
    write_photo(tmp_path / "a.jpg", "red")
    state = tmp_path / "state"
    path = state / "index.db"
    initialize(path)
    settings = Settings(state_dir=state, worker=WorkerSettings(stages=("exif",)))
    with connect(path) as db, ThreadPoolExecutor(1) as pool:
        request_scan(db)
        worker = LibraryWorker(db, library, state, settings, lambda: None)
        run_queue(worker, pool)
        assert progress(db)["stages"] == {"exif": {"done": 1}, "preview": {"skipped": 1}}

        worker = LibraryWorker(db, library, state, Settings(state_dir=state), lambda: None)
        worker.configure()
        run_queue(worker, pool)
        assert progress(db)["stages"] == {"exif": {"done": 1}, "preview": {"done": 1}}


def test_repeated_interruption_releases_queue_for_other_jobs(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    write_photo(root / "a.jpg", "red")
    write_photo(root / "b.jpg", "blue")
    state = tmp_path / "state"
    library = Library(root=root)
    settings = Settings(state_dir=state, worker=WorkerSettings(stages=("preview",)))
    path = state / "index.db"
    initialize(path)
    with connect(path) as db, ThreadPoolExecutor(1) as pool:
        request_scan(db)
        worker = LibraryWorker(db, library, state, settings, lambda: None)
        while worker.step_scan():
            pass
        for _ in range(3):
            assert claim_jobs(db, 1)[0].asset_id == 1
            recover(db)
        run_queue(worker, pool)
        assert progress(db)["stages"]["preview"] == {"error": 1, "done": 1}
        failed = db.execute("SELECT error FROM jobs WHERE asset_id = 1 AND stage = 'preview'")
        assert "interrupted this stage three times" in failed.fetchone()["error"]
        retry_job(db, 1, "preview")
        run_queue(worker, pool)
        assert progress(db)["stages"]["preview"] == {"done": 2}
