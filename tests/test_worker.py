from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from photocull.config import BurstSettings, Library, ScoringSettings, Settings, WorkerSettings
from photocull.db import connect, initialize
from photocull.db.bursts import choose_best, get_burst
from photocull.db.catalog import clear_auto_reject, get_asset
from photocull.db.jobs import NO_PREVIEW, claim_jobs, progress, recover, request_scan, retry_job
from photocull.media.cache import cache_path
from photocull.worker import LibraryWorker
from photocull.worker import scan as scanner
from photocull.worker.analysis import mark_bursts_dirty, refresh_bursts


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
        assert progress(db)["stages"] == {
            "exif": {"done": 2},
            "preview": {"done": 2},
            "score": {"done": 2},
        }
        assert progress(db)["worker"]["heartbeat"] > 0
        assert hashed == ["b.jpg"]
        attempts = db.execute(
            "SELECT stage, attempts FROM jobs WHERE asset_id = ? ORDER BY stage",
            (interrupted.asset_id,),
        )
        assert [tuple(row) for row in attempts] == [("exif", 1), ("preview", 2), ("score", 1)]
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
        assert progress(db)["stages"] == {
            "exif": {"done": 1},
            "preview": {"done": 1},
            "score": {"done": 1},
        }


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
        assert progress(db)["stages"] == {
            "exif": {"done": 1},
            "preview": {"skipped": 1},
            "score": {"skipped": 1},
        }

        worker = LibraryWorker(db, library, state, Settings(state_dir=state), lambda: None)
        worker.configure()
        run_queue(worker, pool)
        assert progress(db)["stages"] == {
            "exif": {"done": 1},
            "preview": {"done": 1},
            "score": {"done": 1},
        }


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


def test_scores_wait_for_preview_and_preserve_dismissal_on_retry(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    photo = root / "a.jpg"
    write_photo(photo, "white")
    original = photo.read_bytes()
    sidecar = photo.with_suffix(".xmp")
    sidecar.write_bytes(b"user sidecar")
    library = Library(root=root)
    state = tmp_path / "state"
    path = state / "index.db"
    initialize(path)
    with connect(path) as db, ThreadPoolExecutor(1) as pool:
        worker = LibraryWorker(
            db, library, state, Settings(worker=WorkerSettings(stages=("score",))), lambda: None
        )
        request_scan(db)
        run_queue(worker, pool)
        job = db.execute("SELECT * FROM jobs WHERE asset_id = 1 AND stage = 'score'").fetchone()
        assert (job["status"], job["error"], job["attempts"]) == ("skipped", NO_PREVIEW, 0)

        worker = LibraryWorker(db, library, state, Settings(), lambda: None)
        worker.configure()
        while worker.step_scan():
            pass
        while (job := claim_jobs(db, 1)[0]).stage != "score":
            worker.finish(job, pool.submit(worker.work, job))
        recover(db)
        run_queue(worker, pool)
        assert get_asset(db, 1)["auto_reject_reason"] == "clipped_highlights"
        assert clear_auto_reject(db, 1) == 1
        assert retry_job(db, 1, "score")["status"] == "pending"
        run_queue(worker, pool)
        assert get_asset(db, 1)["auto_reject_reason"] is None
        assert db.execute("SELECT attempts FROM jobs WHERE stage = 'score'").fetchone()[0] == 3

        worker.settings = Settings(scoring=ScoringSettings(max_clipped_highlights=1))
        worker.configure()
        assert db.execute("SELECT suggested_reject_reason FROM asset_scores").fetchone()[0] == (
            "overexposed"
        )
        assert get_asset(db, 1)["auto_reject_reason"] is None
        assert photo.read_bytes() == original
        assert sidecar.read_bytes() == b"user sidecar"


def test_bursts_match_hash_and_capture_window_and_keep_chosen_best(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    gradient = Image.linear_gradient("L").rotate(90)
    for index, (second, subsecond, image) in enumerate(
        [
            (5, "100", gradient),
            (5, "900", gradient.transpose(Image.Transpose.FLIP_LEFT_RIGHT)),
            (6, "900", gradient.point(lambda value: (value + 40) // 2)),
            (7, "200", gradient),
            (7, "300", gradient),
        ],
        start=1,
    ):
        exif = Image.Exif()
        exif[34665] = {36867: f"2024:01:02 03:04:{second:02d}", 37521: subsecond}
        image.save(root / f"{index}.jpg", exif=exif)
    Image.new("RGB", (24, 16), "black").save(root / "6.jpg")
    library = Library(root=root)
    state = tmp_path / "state"
    path = state / "index.db"
    initialize(path)
    settings = Settings(state_dir=state, burst=BurstSettings(max_hamming_distance=0))
    with connect(path) as db, ThreadPoolExecutor(1) as pool:
        worker = LibraryWorker(db, library, state, settings, lambda: None)
        request_scan(db)
        run_queue(worker, pool)
        members = db.execute(
            """SELECT a.path, b.path FROM burst_members AS m
               JOIN assets AS a ON a.id = m.asset_id
               JOIN assets AS b ON b.id = m.burst_id ORDER BY a.path"""
        ).fetchall()
        assert [tuple(row) for row in members] == [
            ("1.jpg", "1.jpg"),
            ("3.jpg", "1.jpg"),
            ("4.jpg", "4.jpg"),
            ("5.jpg", "4.jpg"),
        ]
        anchor, alternate = (
            db.execute("SELECT id FROM assets WHERE path = ?", (path,)).fetchone()[0]
            for path in ("1.jpg", "3.jpg")
        )
        burst = get_burst(db, anchor)
        assert burst["captured_at"] == "2024-01-02T03:04:05.100000"
        assert burst["recommended_best_id"] == anchor
        chosen = choose_best(db, anchor, alternate, burst["membership_key"])
        assert chosen["best_id"] == alternate
        revision = progress(db)["catalog_revision"]
        mark_bursts_dirty(db)
        assert refresh_bursts(db, settings.burst, lambda: None)
        assert get_burst(db, anchor) == chosen
        assert progress(db)["catalog_revision"] == revision
        assert not refresh_bursts(db, settings.burst, lambda: None)

        # Enlarging the window merges both groups and preserves the choice.
        assert refresh_bursts(
            db, BurstSettings(window_seconds=3, max_hamming_distance=0), lambda: None
        )
        assert get_burst(db, anchor)["member_count"] == 4
        assert get_burst(db, anchor)["best_id"] == alternate
        (root / "3.jpg").unlink()
        worker.settings = Settings(state_dir=state, burst=BurstSettings(window_seconds=3))
        request_scan(db)
        run_queue(worker, pool)
        assert get_burst(db, anchor)["member_count"] == 3
        assert get_burst(db, anchor)["user_best_id"] is None
