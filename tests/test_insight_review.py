from __future__ import annotations

import errno
import os
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photocull.api import create_app
from photocull.db import connect, initialize, write
from photocull.db.insights import finish, register
from photocull.db.jobs import claim_scan, finish_scan, request_scan
from photocull.db.transfers import ExportRequest, ReportRequest, Selection, cancel, enqueue
from photocull.insights import models
from photocull.media.walk import MediaJail, SourceChanged, hash_file, source_key
from photocull.worker.insights import transfers as transfer_worker
from photocull.worker.insights import watch as watch_worker
from photocull.worker.insights.people import FaceGroups
from photocull.worker.insights.transfers import Transfers
from photocull.worker.insights.watch import Watch
from photocull.worker.scan import scan_batches
from tests.test_insights import URL, get, make_settings, payload, scan
from tests.worker_harness import photo, start_worker, wait_for


def noop():
    pass


def insert_asset(db, scan_id, path, key="", size=1, *, day="2025-01-01", captured=None):
    return db.execute(
        """INSERT INTO assets(path, folder, kind, size, mtime_ns, content_hash, scan_id, day,
                              source_key, captured_at)
           VALUES(?, '', 'image', ?, 0, 'test', ?, ?, ?, ?) RETURNING *""",
        (path, size, scan_id, day, key, captured),
    ).fetchone()


def fixture_worker(settings, db):
    return SimpleNamespace(
        settings=settings,
        db=db,
        state=settings.library_state("test"),
        library=settings.libraries["test"],
        scan=None,
    )


def test_scan_coalesces_watch_hints_and_detects_changes_after_failed_scan(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, watch=True, watch_debounce=3, watch_interval=10)
    root = settings.libraries["test"].root
    path = settings.library_state("test") / "index.db"
    initialize(path)
    clock = SimpleNamespace(now=100)
    monkeypatch.setattr(watch_worker, "time", SimpleNamespace(monotonic=lambda: clock.now))
    with connect(path) as db:
        worker = fixture_worker(settings, db)
        watch = Watch(worker)
        (root / "日本.jpg").write_bytes(b"fixture")
        watch.notify()
        clock.now = 102
        watch.step(noop)
        assert db.execute("SELECT count(*) FROM scan_requests").fetchone()[0] == 0

        manual = request_scan(db)["id"]
        clock.now = 103
        watch.step(noop)
        assert claim_scan(db) == manual
        worker.scan = (manual, iter(()))
        watch.notify()
        watch.step(noop)
        insert_asset(db, manual, "日本.jpg", source_key((root / "日本.jpg").stat()))
        finish_scan(db, manual)
        worker.scan = None
        clock.now = 107
        watch.step(noop)
        assert db.execute("SELECT count(*) FROM scan_requests").fetchone()[0] == 1

        (root / "日本.jpg").write_bytes(b"changed")
        watch.notify()
        clock.now = 110
        watch.step(noop)
        watched = claim_scan(db)
        assert watched > manual
        finish_scan(db, watched, "Transient I/O failure")
        clock.now = 120
        watch.step(noop)
        clock.now = 123
        watch.step(noop)
        assert claim_scan(db) > watched


def test_partial_watch_scan_preserves_prior_hash_and_detects_replacement(tmp_path):
    settings = make_settings(tmp_path, watch=True)
    source = settings.libraries["test"].root / "日本.jpg"
    source.write_bytes(b"original")
    path = settings.library_state("test") / "index.db"
    initialize(path)
    jail = MediaJail(settings.libraries["test"], settings.state_dir)
    with connect(path) as db:
        job = request_scan(db)["id"]
        assert claim_scan(db) == job
        list(scan_batches(db, job, jail, settings.worker, noop))
        old = db.execute("SELECT content_hash FROM assets").fetchone()[0]
        finish_scan(db, job)
        source.write_bytes(b"still writing")
        job = request_scan(db)["id"]
        assert claim_scan(db) == job
        deferred = []
        list(
            scan_batches(
                db,
                job,
                jail,
                settings.worker,
                noop,
                settle_seconds=60,
                defer=lambda: deferred.append(True),
            )
        )
        assert deferred == [True]
        assert tuple(db.execute("SELECT active, content_hash FROM assets").fetchone()) == (1, old)
        finish_scan(db, job)
        entry = next(jail.walk(noop))
        replacement = source.with_suffix(".partial")
        replacement.write_bytes(b"new file")
        with pytest.raises(SourceChanged):
            hash_file(jail, entry, lambda: replacement.replace(source))
        job = request_scan(db)["id"]
        assert claim_scan(db) == job
        list(scan_batches(db, job, jail, settings.worker, noop))
        assert db.execute("SELECT content_hash FROM assets").fetchone()[0] != old


def test_many_faces_keep_ids_and_split_assignment_during_recalculation(tmp_path):
    settings = make_settings(tmp_path, faces=True)
    path = settings.library_state("test") / "index.db"
    groups = FaceGroups()
    with TestClient(create_app(settings)) as client, connect(path) as db:
        scan_id = request_scan(db)["id"]
        asset = insert_asset(db, scan_id, "集合.jpg")
        model = models(settings)["faces"]
        key = register(db, model)
        faces = []
        for i in range(40):
            x, y = (i % 8) / 8, (i // 8) / 5
            vector = bytearray(256)
            vector[2 * i : 2 * i + 2] = b"\x00\x3c"
            faces.append(((x, y, x + 0.08, y + 0.12), 0.9, 0.8, bytes(vector)))
        groups.save(db, key, settings.insights, asset, faces, noop)
        original = db.execute("SELECT id, person_id FROM faces ORDER BY id").fetchall()
        assert len(original) == 40
        assert len({row["person_id"] for row in original}) == 40
        first, second = original[:2]
        result = payload(client.post(f"{URL}/people/{first[1]}/merge", json={"source": second[1]}))
        assert result["moved"] == 1
        assert (
            client.patch(
                f"{URL}/people/{first[1]}",
                json={
                    "name": "Stale name",
                    "expected_revision": 0,
                },
            ).status_code
            == 409
        )

        changed = []

        def split_during_index_refresh():
            if not changed:
                changed.append(
                    payload(
                        client.post(
                            f"{URL}/people/{first[1]}/split",
                            json={
                                "faces": [second[0]],
                                "name": "分割",
                            },
                        )
                    )["id"]
                )

        groups.save(db, key, settings.insights, asset, faces, split_during_index_refresh)
        assignments = dict(db.execute("SELECT id, person_id FROM faces"))
        assert set(assignments) == {row[0] for row in original}
        assert assignments[second[0]] == changed[0]
        assert db.execute("SELECT manual FROM faces WHERE id=?", (first[0],)).fetchone()[0] == 1
        groups.save(db, key, settings.insights, asset, [], noop)
        groups.prepare(db, key, settings.insights.face_min_quality, noop)
        assert len(groups.index) == 0
        assert get(client, f"{URL}/people")["items"] == []


def test_missing_gps_and_capture_time_render_as_separate_stops(tmp_path):
    settings = make_settings(tmp_path, story=True)
    root = settings.libraries["test"].root
    photo(root / "timed.jpg", "green", "01 10:00:00")
    path = root / "日付なし.jpg"
    with Image.new("RGB", (80, 50), "red") as image:
        image.save(path)
    os.utime(path, (1735689600, 1735689600))  # 2025-01-01 UTC
    with TestClient(create_app(settings)) as client, start_worker(settings):
        scan(client)

        def ready():
            items = get(client, f"{URL}/stories/2025-01-01")["items"]
            return (
                items if len(items) == 2 and all(row["representatives"] for row in items) else None
            )

        timed, undated = wait_for(ready)
        assert all(
            row["latitude"] is row["longitude"] is row["title"] is None for row in (timed, undated)
        )
        assert timed["started"] and undated["started"] is None


@pytest.fixture
def staged_worker(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    settings = make_settings(
        tmp_path, export=True, story=True, report=True, export_roots={"archive": root}
    )
    state = settings.library_state("test")
    index = state / "index.db"
    initialize(index)
    media = settings.libraries["test"].root / "折り返し\\..\\保管.jpg"
    media.write_bytes(b"keeper")
    with connect(index) as db:
        scan = request_scan(db)["id"]
        insert_asset(db, scan, media.name, source_key(media.stat()), size=media.stat().st_size)
        yield Transfers(fixture_worker(settings, db)), root, media


def queue_export(db, kind):
    return enqueue(
        db,
        kind,
        ExportRequest(
            kind=kind,
            destination="archive" if kind == "copy" else None,
            selection=Selection(minimum_rating=0),
        ),
    )


@pytest.mark.parametrize("kind,number", [("copy", errno.EACCES), ("zip", errno.ENOSPC)])
def test_failed_output_removes_private_staging(staged_worker, monkeypatch, kind, number):
    worker, root, media = staged_worker
    job = queue_export(worker.db, kind)

    def fail(_config, _job, _pulse, output):
        with output("part.jpg") as stream:
            stream.write(b"unfinished")
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(worker, "copy_files", fail)
    worker.step(noop)
    row = worker.db.execute("SELECT status, error FROM transfers WHERE id=?", (job,)).fetchone()
    assert tuple(row) == ("error", f"[Errno {number}] {os.strerror(number)}")
    assert not list(root.iterdir())
    assert not list((worker.state / "transfers").glob("*"))
    assert media.read_bytes() == b"keeper"


@pytest.mark.parametrize("kind", ["copy", "zip"])
def test_restart_after_publication_checks_output_identity(staged_worker, monkeypatch, kind):
    worker, root, media = staged_worker
    job = queue_export(worker.db, kind)
    publish = transfer_worker.rename_output

    def interrupted(*args):
        # SQLite is writable by the API while the worker publishes into a folder.
        with connect(worker.state / "index.db") as api:
            assert cancel(api, job) is None
            api.execute("UPDATE catalog_status SET revision = revision + 1")
        publish(*args)
        raise SystemExit

    with monkeypatch.context() as patch:
        patch.setattr(transfer_worker, "rename_output", interrupted)
        with pytest.raises(SystemExit):
            worker.step(noop)
    assert cancel(worker.db, job) is None
    worker.recover()
    assert (
        worker.db.execute("SELECT status FROM transfers WHERE id=?", (job,)).fetchone()[0] == "done"
    )
    if kind == "zip":
        with ZipFile(worker.state / "transfers" / f"{job}.zip") as archive:
            (name,) = archive.namelist()
            assert name.endswith(".jpg") and "/" not in name and "\\" not in name
            assert archive.read(name) == media.read_bytes()
    else:
        assert next((root / job).iterdir()).read_bytes() == media.read_bytes()

    unrelated = queue_export(worker.db, "copy")
    (root / unrelated).mkdir()
    worker.db.execute("UPDATE transfers SET status='running' WHERE id=?", (unrelated,))
    worker.recover()
    assert (
        worker.db.execute(
            "SELECT status FROM transfers WHERE id=?",
            (unrelated,),
        ).fetchone()[0]
        == "error"
    )
    assert (root / unrelated).is_dir()


def test_report_directory_and_download_do_not_follow_symlinks(staged_worker, tmp_path):
    worker, _, _ = staged_worker
    remote = tmp_path / "outside"
    remote.mkdir()
    artifact_dir = worker.state / "transfers"
    artifact_dir.symlink_to(remote, target_is_directory=True)
    job = enqueue(worker.db, "report", ReportRequest())
    worker.step(noop)
    assert (
        worker.db.execute("SELECT status FROM transfers WHERE id=?", (job,)).fetchone()[0]
        == "error"
    )
    assert not list(remote.iterdir())

    artifact_dir.unlink()
    artifact_dir.mkdir()
    (remote / "private.html").write_text("outside", encoding="utf-8")
    (artifact_dir / f"{job}.html").symlink_to(remote / "private.html")
    worker.db.execute("UPDATE transfers SET status='done' WHERE id=?", (job,))
    with TestClient(create_app(worker.settings)) as client:
        response = client.get(f"{URL}/transfers/{job}/download")
        assert response.status_code == 404 and "outside" not in response.text


def test_labeled_aesthetic_result_queues_a_new_taste_fit(tmp_path):
    from photocull.config import MLSettings

    settings = make_settings(tmp_path, aesthetic=True, taste=True).model_copy(
        update={"ml": MLSettings(enabled=True)}
    )
    path = settings.library_state("test") / "index.db"
    initialize(path)
    with connect(path) as db:
        asset = insert_asset(db, request_scan(db)["id"], "rated.jpg")
        db.execute("UPDATE assets SET rating=5 WHERE id=?", (asset["id"],))
        key = register(db, models(settings)["aesthetic"])
        db.execute("UPDATE taste_state SET completed=requested")
        with write(db):
            finish(db, "aesthetic", key, asset, score=0.75)
        requested, completed = db.execute("SELECT requested, completed FROM taste_state").fetchone()
        assert requested == completed + 1


def test_face_upgrade_and_manual_assignment_preserve_identity(tmp_path, monkeypatch):
    from photocull import db as db_module

    settings = make_settings(tmp_path, faces=True)
    path = settings.library_state("test") / "index.db"
    with monkeypatch.context() as old_schema:
        old_schema.setattr(db_module, "MIGRATIONS", db_module.MIGRATIONS[:-2])
        initialize(path)
    box = (0.1, 0.2, 0.4, 0.5)
    vector = b"\x00\x3c" + bytes(254)
    with connect(path) as db:
        key = register(db, models(settings)["faces"])
        scan_id = request_scan(db)["id"]
        assets = [insert_asset(db, scan_id, f"{i}.jpg") for i in range(6)]
        db.executemany(
            "INSERT INTO people(id, name) VALUES (?, ?)",
            [(1, None), (2, "Mika"), (3, None), (4, None)],
        )
        db.executemany(
            """INSERT INTO faces(id, asset_id, person_id, key, content_hash, left, top,
                                 right, bottom, confidence, quality, vector, manual)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.9, ?, ?, ?)""",
            [
                (i, asset["id"], person, key, asset["content_hash"], *box, quality, vector, manual)
                for i, (asset, person, quality, manual) in enumerate(
                    zip(assets[:4], (1, 2, 3, 4), (0.1, 0.1, 0.1, 0.12), (0, 0, 1, 0), strict=True),
                    1,
                )
            ],
        )
    initialize(path)

    with TestClient(create_app(settings)) as client, connect(path) as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert dict(db.execute("SELECT id, person_id FROM faces")) == {1: None, 2: 2, 3: 3, 4: 4}
        assert not get(client, f"{URL}/people")["items"]
        assert [p["id"] for p in get(client, f"{URL}/people?include_small=true")["items"]] == [
            2,
            3,
            4,
        ]
        groups = FaceGroups()
        low_quality = [(box, 0.9, 0.1, vector)]
        groups.save(db, key, settings.insights, assets[4], low_quality, noop)
        groups.save(db, key, settings.insights, assets[0], low_quality, noop)
        assert [f["id"] for f in get(client, f"{URL}/faces/unassigned")["items"]] == [1, 5]
        url = f"{URL}/faces/unassigned/assign"
        result = payload(client.post(url, json={"faces": [1], "person_id": 2}))
        assert result == {"id": 2, "moved": 1}
        assert client.post(url, json={"faces": [1, 5], "name": "Stale"}).status_code == 409
        assert [f["id"] for f in get(client, f"{URL}/faces/unassigned")["items"]] == [5]
        created = payload(client.post(url, json={"faces": [5], "name": "New"}))["id"]
        assert get(client, f"{URL}/people/{created}/faces")["person"]["name"] == "New"
        assert [p["id"] for p in get(client, f"{URL}/people")["items"]] == [2]
        groups.save(db, key, settings.insights, assets[0], low_quality, noop)
        groups.save(db, key, settings.insights, assets[5], [(box, 0.9, 0.2, vector)], noop)
        assert dict(db.execute("SELECT id, person_id FROM faces")) == {
            1: 2,
            2: 2,
            3: 3,
            4: 4,
            5: created,
            6: 4,
        }
