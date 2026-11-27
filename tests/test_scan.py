from __future__ import annotations

import errno
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import Library, Settings, WorkerSettings
from photocull.db import connect, initialize
from photocull.db.jobs import claim_scan, progress, recover, request_scan
from photocull.media import walk
from photocull.worker import LibraryWorker, run
from photocull.worker.insights.watch import Watch
from photocull.worker.scan import scan_batches
from tests.worker_harness import wait_for

BASE = "/api/libraries/photos"


def noop():
    pass


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    library = Library(root=root)
    settings = Settings(
        libraries={"photos": library},
        state_dir=tmp_path / "state",
        worker=WorkerSettings(stages=(), scan_batch_size=1),
    )
    state = settings.library_state("photos")
    initialize(state / "index.db")
    with connect(state / "index.db") as db:
        yield root, LibraryWorker(db, library, state, settings, noop)


def scan(worker):
    scan_id = request_scan(worker.db)["id"]
    while worker.step_scan():
        pass
    return scan_id


def test_local_read_errors_are_listed_and_do_not_abort_scan(library, monkeypatch):
    root, worker = library
    for folder in ("nested", "nested0"):
        (root / folder).mkdir()
        (root / folder / "known.jpg").write_bytes(b"reviewed")
    (root / "a-denied.nef").write_bytes(b"older media")
    scan(worker)
    old_hash = worker.db.execute(
        "SELECT content_hash FROM assets WHERE path='a-denied.nef'"
    ).fetchone()[0]
    worker.db.execute("UPDATE assets SET rating=4")

    (root / "a-denied.nef").write_bytes(b"changed media")
    (root / "b-vanishes.jpg").write_bytes(b"disappearing media")
    (root / "c-io.jpg").write_bytes(b"failing media")
    (root / "z-readable.jpg").write_bytes(b"new media")
    (root / "nested0" / "known.jpg").unlink()
    open_directory = walk.open_directory
    open_media = walk.MediaJail.open_media
    blocked = True

    @contextmanager
    def folders(path):
        if blocked and path == root / "nested":
            raise PermissionError(errno.EACCES, "fixture", path)
        with open_directory(path) as fd:
            yield fd

    @contextmanager
    def media(jail, path):
        if blocked and path == "a-denied.nef":
            raise PermissionError(errno.EACCES, "fixture", path)
        if blocked and path == "b-vanishes.jpg":
            (root / path).unlink()
        if blocked and path == "c-io.jpg":
            raise OSError(errno.EIO, "fixture", path)
        with open_media(jail, path) as result:
            yield result

    monkeypatch.setattr(walk, "open_directory", folders)
    monkeypatch.setattr(walk.MediaJail, "open_media", media)
    current = scan(worker)

    with TestClient(create_app(worker.settings)) as client:
        latest = client.get(f"{BASE}/jobs").json()["scan"]
        assert (latest["status"], latest["error"]) == ("done", None)
        assert (latest["file_errors"], latest["directory_errors"]) == (3, 1)
        assert (latest["files_indexed"], latest["bytes_hashed"]) == (1, len(b"new media"))
        url = f"{BASE}/scans/{current}/errors"
        first = client.get(url, params={"limit": 2}).json()
        second = client.get(url, params={"limit": 2, "cursor": first["next_cursor"]}).json()
        assert second["next_cursor"] is None
        issues = {item["path"]: item for item in first["items"] + second["items"]}
        assert {
            path: (i["kind"], i["operation"], i["code"], i["errno"]) for path, i in issues.items()
        } == {
            "a-denied.nef": ("file", "read", "permission_denied", errno.EACCES),
            "b-vanishes.jpg": ("file", "read", "missing", errno.ENOENT),
            "c-io.jpg": ("file", "read", "io_error", errno.EIO),
            "nested": ("directory", "list", "permission_denied", errno.EACCES),
        }
        assert client.get(f"{BASE}/scans/{current + 1}/errors").status_code == 404
        assert client.get(url, params={"limit": 201}).status_code == 422

    rows = {
        row["path"]: row
        for row in worker.db.execute("SELECT path,active,rating,content_hash FROM assets")
    }
    assert rows["a-denied.nef"]["content_hash"] == old_hash
    assert rows["a-denied.nef"]["rating"] == 4
    assert rows["nested/known.jpg"]["active"] == 1
    assert rows["nested0/known.jpg"]["active"] == 0
    assert rows["z-readable.jpg"]["active"] == 1

    scan(worker)  # The disappeared file is absent from this complete walk.
    assert not Watch(worker).differs(noop)  # Unchanged faults do not trigger endless retries.
    blocked = False
    assert Watch(worker).differs(noop)
    scan(worker)
    assert progress(worker.db)["scan"]["file_errors"] == 0
    assert progress(worker.db)["scan"]["directory_errors"] == 0
    assert not Watch(worker).differs(noop)


def test_scan_cannot_retire_existing_assets_if_the_root_is_unreadable(library, monkeypatch):
    root, worker = library
    (root / "reviewed.jpg").write_bytes(b"reviewed")
    scan(worker)
    open_directory = walk.open_directory

    @contextmanager
    def folders(path):
        if path == root:
            raise PermissionError(errno.EACCES, "fixture", path)
        with open_directory(path) as fd:
            yield fd

    monkeypatch.setattr(walk, "open_directory", folders)
    scan(worker)
    latest = progress(worker.db)["scan"]
    assert (latest["status"], latest["directory_errors"]) == ("error", 1)
    assert worker.db.execute("SELECT active FROM assets").fetchone()[0] == 1
    assert tuple(worker.db.execute("SELECT path, code FROM scan_errors").fetchone()) == (
        ".",
        "permission_denied",
    )


def test_scan_restart_reconciles_the_new_walk_even_with_the_same_request_id(library):
    root, worker = library
    db = worker.db
    gone = root / "a.jpg"
    gone.write_bytes(b"removed during restart")
    (root / "b.jpg").write_bytes(b"still available")
    scan_id = request_scan(db)["id"]
    assert claim_scan(db) == scan_id
    jail = walk.MediaJail(worker.library, worker.settings.state_dir)
    with closing(scan_batches(db, scan_id, jail, worker.settings.worker, noop)) as batches:
        next(batches)
        assert progress(db)["scan"]["files_indexed"] == 1
    gone.unlink()
    recover(db)
    assert scan(worker) == scan_id
    latest = progress(db)["scan"]
    assert latest["attempt"] == 2
    assert latest["files_indexed"] == 1
    assert [tuple(row) for row in db.execute("SELECT path, active FROM assets ORDER BY path")] == [
        ("a.jpg", 0),
        ("b.jpg", 1),
    ]


@pytest.mark.parametrize("scan_parallelism", [1, 2])
def test_worker_scans_whole_libraries_before_starting_parallel_media(
    tmp_path, monkeypatch, scan_parallelism
):
    settings = Settings(
        libraries={name: Library(root=tmp_path / name) for name in ("a", "b", "c")},
        state_dir=tmp_path / "state",
        worker=WorkerSettings(
            stages=("exif",), parallelism=2, batch_size=1, scan_parallelism=scan_parallelism
        ),
    )
    for name, library in settings.libraries.items():
        library.root.mkdir()
        for i in range(3):
            (library.root / f"{i}.jpg").write_bytes(bytes([i]))
        path = settings.library_state(name) / "index.db"
        initialize(path)
        with connect(path) as db:
            request_scan(db)

    run_scan = LibraryWorker.run_scan
    starting: list[str] = []
    completed: set[str] = set()
    decoding: list[tuple[str, int]] = []
    lock = Lock()
    release_scans = Event()
    release_media = Event()

    def record_scan(worker, scan_id, pulse):
        with lock:
            starting.append(worker.state.name)
        assert release_scans.wait(5)
        run_scan(worker, scan_id, pulse)
        with lock:
            completed.add(worker.state.name)

    def record_media(worker, job):
        with lock:
            assert completed == {"a", "b", "c"}
            decoding.append((worker.state.name, job.asset_id))
        assert release_media.wait(5)
        return {}, None

    monkeypatch.setattr(LibraryWorker, "run_scan", record_scan)
    monkeypatch.setattr(LibraryWorker, "work", record_media)
    stop = Event()
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run, settings, stop)
        try:
            wait_for(lambda: len(starting) >= scan_parallelism)
            assert sorted(starting) == ["a", "b"][:scan_parallelism]
            assert not decoding
            release_scans.set()
            wait_for(lambda: len(decoding) >= 2)
            assert sorted(decoding) == [("a", 1), ("a", 2)]
        finally:
            release_scans.set()
            release_media.set()
            stop.set()
        assert task.result(timeout=5) == 0


def test_overlap_allows_only_other_libraries_to_process_media(tmp_path, monkeypatch):
    settings = Settings(
        libraries={name: Library(root=tmp_path / name) for name in ("scanning", "ready")},
        state_dir=tmp_path / "state",
        worker=WorkerSettings(stages=("exif",), scan_overlap=True),
    )
    for name, library in settings.libraries.items():
        library.root.mkdir()
        (library.root / "frame.jpg").write_bytes(b"media")
        state = settings.library_state(name)
        initialize(state / "index.db")
        with connect(state / "index.db") as db:
            scan(LibraryWorker(db, library, state, settings, noop))
            if name == "scanning":
                request_scan(db)

    scanning = Event()
    hold = Event()
    other_processed = Event()
    processed: list[str] = []
    run_scan = LibraryWorker.run_scan

    def pause_scan(worker, scan_id, pulse):
        scanning.set()
        assert hold.wait(5)
        run_scan(worker, scan_id, pulse)

    def record_media(worker, job):
        processed.append(worker.state.name)
        other_processed.set()
        return {}, None

    monkeypatch.setattr(LibraryWorker, "run_scan", pause_scan)
    monkeypatch.setattr(LibraryWorker, "work", record_media)
    stop = Event()
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(run, settings, stop)
        try:
            assert scanning.wait(10)
            assert other_processed.wait(5)
            assert processed == ["ready"]
        finally:
            stop.set()
            hold.set()
        assert task.result(timeout=5) == 0
