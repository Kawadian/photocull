from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.archive import ArchiveError, ArchiveRequest, Filters, preview
from photocull.config import ArchiveSettings, Library, Settings
from photocull.db import connect, initialize
from photocull.db.archive import audit_page, cancel, enqueue, jobs, retry_recovery, trash
from photocull.db.archive_report import duplicates, storage
from photocull.media.walk import MediaJail, source_key
from tests.archive_fixtures import synthetic_dng, synthetic_image, synthetic_video


@pytest.fixture
def catalog(tmp_path):
    source, output = tmp_path / "photos", tmp_path / "export"
    source.mkdir()
    output.mkdir()
    config = Settings(
        libraries={"photos": Library(root=source)},
        state_dir=tmp_path / "state",
        archive=ArchiveSettings(write_roots=(source,), export_roots={"photos": output}),
    )
    path = config.library_state("photos") / "index.db"
    initialize(path)
    with connect(path) as db:
        db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
        yield config, source, output, db


def record(db, root, path, *, kind="image", rejected=0, rating=0):
    return db.execute(
        """INSERT INTO assets(path,folder,kind,size,mtime_ns,content_hash,scan_id,day,source_key,
                              make,model,rating,rejected,width,height,duration)
           VALUES(?,?,?,?,?,?,1,'2020-04-20',?,'Sony','ILCE-7CM2',?,?,384,256,2) RETURNING id""",
        (
            str(path.relative_to(root)),
            str(path.parent.relative_to(root)).replace(".", "", 1)
            if path.parent == root
            else str(path.parent.relative_to(root)),
            kind,
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            source_key(path.stat()),
            rating,
            rejected,
        ),
    ).fetchone()[0]


def queue(catalog, request, *, actor="tester"):
    settings, _, _, db = catalog
    planned = preview(db, settings, "photos", request)
    assert planned["count"] > 0, planned
    return enqueue(db, settings, "photos", request, planned["confirmation"], actor)


def perform(catalog, job, *, wait=True):
    from photocull.worker.archive import ArchiveWork

    settings, _, _, db = catalog
    work = ArchiveWork(settings, "photos", Event())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(work.process, row) for row in work.claim(db, limit=2)]
        if wait:
            for future in futures:
                future.result(timeout=45)
    work.settle(db)
    return jobs(db, job)


def formats(*names):
    for tool in names:
        if not shutil.which(tool):
            pytest.skip(f"{tool} is unavailable")


def test_disabled_dry_run_and_stale_confirmation(catalog):
    settings, root, export, db = catalog
    source = root / "photo.jpg"
    source.write_bytes(b"test photo")
    id_ = record(db, root, source, rejected=1)
    disabled = settings.model_copy(
        update={
            "archive": ArchiveSettings(export_roots={"photos": export}),
        }
    )
    with TestClient(create_app(disabled)) as client:
        base = "/api/libraries/photos/archive"
        capability = client.get(base).json()
        assert capability["destructive"] is False and capability["export"] is True
        assert "PHOTOCULL_WRITE_ROOTS" in capability["destructive_reason"]
        assert capability["retention_days"] == 0
        result = client.post(
            f"{base}/preview", json={"operation": "trash", "filters": {"ids": [id_]}}
        )
        assert result.status_code == 409
        assert "PHOTOCULL_WRITE_ROOTS" in result.json()["detail"]
        state_changes = db.total_changes
        original = source.stat()
        plan = {
            "operation": "copy",
            "filters": {
                "selection": "rejected",
                "camera": "ilce",
                "start": "2018-01-01",
                "end": "2024-12-31",
            },
        }
        first = client.post(f"{base}/preview", json=plan)
        assert first.status_code == 200, first.text
        assert first.json()["count"] == 1
        assert db.total_changes == state_changes
        assert db.execute("SELECT count(*) FROM archive_jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM archive_audit").fetchone()[0] == 0
        assert not list(export.iterdir()) and source.stat().st_ctime_ns == original.st_ctime_ns
        source.write_bytes(b"changed out of band")
        rejected = client.post(
            f"{base}/jobs", json={"plan": plan, "confirmation": first.json()["confirmation"]}
        )
        assert rejected.status_code == 409


def test_same_filesystem_trash_restore_collision_purge_and_audit(catalog):
    settings, root, _, db = catalog
    source = root / "photo.jpg"
    source.write_bytes(b"original")
    metadata = root / "photo.XMP"
    metadata.write_bytes(b"XMP")
    original = source.stat()
    id_ = record(db, root, source, rejected=1)
    request = ArchiveRequest(operation="trash", filters=Filters(ids=(id_,)), reason="Review reject")
    job = queue(catalog, request)
    assert perform(catalog, job)["counts"] == {"done": 1}
    assert not source.exists() and not metadata.exists()
    entry = trash(db)["items"][0]
    assert entry["reason"] == "Review reject"
    assert Path(entry["files"][0]["path"]).stat().st_ino == original.st_ino
    assert entry["files"][0]["original"] == str(source)
    assert entry["files"][0]["sha256"] == hashlib.sha256(b"original").hexdigest()
    source.write_bytes(b"new photograph")
    ids = (entry["id"],)
    restore = ArchiveRequest(operation="restore", trash_ids=ids)
    assert preview(db, settings, "photos", restore)["count"] == 0
    job = queue(catalog, restore.model_copy(update={"collision": "rename"}))
    assert perform(catalog, job)["counts"] == {"done": 1}
    assert source.read_bytes() == b"new photograph"
    assert (root / "photo_1.jpg").read_bytes() == b"original"
    assert (root / "photo_1.jpg").stat().st_mtime_ns == original.st_mtime_ns
    assert (root / "photo_1.XMP").read_bytes() == b"XMP"
    id_ = record(db, root, root / "photo_1.jpg")
    assert perform(
        catalog,
        queue(
            catalog,
            ArchiveRequest(
                operation="trash",
                filters=Filters(ids=(id_,)),
            ),
        ),
    )["counts"] == {"done": 1}
    entry = trash(db)["items"][0]
    job = queue(catalog, ArchiveRequest(operation="purge", trash_ids=(entry["id"],)))
    assert perform(catalog, job)["counts"] == {"done": 1}
    assert not trash(db)["items"] and not Path(entry["files"][0]["path"]).exists()
    events = audit_page(db)["items"]
    assert {"confirmed", "prepared", "completed", "finished"} <= {e["action"] for e in events}
    assert {e["actor"] for e in events} == {"tester"}
    assert events[2]["details"]["files"][0]["before"]["sha256"] == entry["files"][0]["sha256"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM archive_audit")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE archive_audit SET actor='new'")


def test_export_and_rename_undo_follow_xmp_and_keep_raw_pair(catalog):
    _, root, export, db = catalog
    source = root / "pic.jpg"
    source.write_bytes(b"JPEG")
    (root / "pic.ARW").write_bytes(b"RAW pair")
    (root / "pic.XMP").write_bytes(b"shared")
    (root / "pic.jpg.xmp").write_bytes(b"qualified")
    id_ = record(db, root, source, rating=4)
    request = ArchiveRequest(operation="copy", filters=Filters(ids=(id_,)))
    assert perform(catalog, queue(catalog, request))["counts"] == {"done": 1}
    assert (export / "pic.xmp").read_bytes() == b"shared"
    assert (export / "pic.jpg.xmp").read_bytes() == b"qualified"
    assert source.read_bytes() == b"JPEG"
    rename = ArchiveRequest(
        operation="rename", mode="overwrite", filters=request.filters, template="{year}_{stem}"
    )
    assert perform(catalog, queue(catalog, rename))["counts"] == {"done": 1}
    assert not source.exists()
    assert (root / "pic.XMP").read_bytes() == b"shared"
    assert (root / "2020_pic.jpg.xmp").read_bytes() == b"qualified"
    assert (root / "2020_pic.xmp").read_bytes() == b"shared"
    assert db.execute("SELECT rating FROM archive_rating_transfer").fetchone()[0] == 4
    entry = trash(db)["items"][0]
    undo = ArchiveRequest(operation="undo", trash_ids=(entry["id"],))
    assert perform(catalog, queue(catalog, undo))["counts"] == {"done": 1}
    assert source.read_bytes() == b"JPEG" and (root / "pic.jpg.xmp").read_bytes() == b"qualified"
    assert not any(root.glob("2020*"))


def test_path_jail_and_foreign_output_collision(catalog, tmp_path):
    settings, root, export, db = catalog
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "foreign.jpg").write_bytes(b"foreign")
    (root / "link.jpg").symlink_to(outside / "foreign.jpg")
    record(db, root, root / "link.jpg")
    folder = root / "folder"
    folder.mkdir()
    source = folder / "source.jpg"
    source.write_bytes(b"source")
    id_ = record(db, root, source)
    assert not preview(
        db,
        settings,
        "photos",
        ArchiveRequest(
            operation="trash",
            filters=Filters(ids=(1,)),
        ),
    )["items"]
    request = ArchiveRequest(operation="copy", filters=Filters(ids=(id_,)))
    job = queue(catalog, request)
    (export / "folder").symlink_to(outside, target_is_directory=True)
    assert perform(catalog, job)["counts"] == {"error": 1}
    assert sorted(p.name for p in outside.iterdir()) == ["foreign.jpg"]
    assert source.read_bytes() == b"source"
    (export / "folder").unlink()
    source_sidecar = source.with_suffix(".xmp")
    source_sidecar.symlink_to(outside / "foreign.jpg")
    result = preview(db, settings, "photos", request)
    assert result["count"] == 0 and "regular" in result["skipped"][0]["reason"]


def test_cross_filesystem_backup_verifies_before_unlink(catalog, monkeypatch):
    from photocull.worker import archive_fs

    settings, root, _, db = catalog
    source = root / "photo.jpg"
    source.write_bytes(b"original payload")
    id_ = record(db, root, source)
    native = archive_fs.rename

    def cross_fd(src_fd, src, dst_fd, dst, flags=archive_fs.RENAME_NOREPLACE):
        if src == source.name and dst == "0.jpg":
            raise OSError(errno.EXDEV, "cross-device")
        return native(src_fd, src, dst_fd, dst, flags)

    monkeypatch.setattr(archive_fs, "rename", cross_fd)
    task = ArchiveRequest(operation="trash", filters=Filters(ids=(id_,)))
    job = queue(catalog, task)
    assert perform(catalog, job)["counts"] == {"done": 1}
    entry = Path(trash(db)["items"][0]["files"][0]["path"])
    assert not source.exists() and entry.read_bytes() == b"original payload"
    assert not [
        e for e in MediaJail(settings.libraries["photos"], settings.state_dir).walk(lambda: None)
    ]


def test_cancel_and_blocked_recovery(catalog, monkeypatch):
    from photocull.worker import archive_fs

    _, root, _, db = catalog
    source = root / "source.jpg"
    source.write_bytes(b"original")
    id_ = record(db, root, source)
    request = ArchiveRequest(
        operation="rename", mode="overwrite", filters=Filters(ids=(id_,)), template="{stem}_new"
    )
    job = queue(catalog, request)
    cancel(db, job, "cancel-test")
    assert perform(catalog, job)["counts"] == {"cancelled": 1}
    assert source.read_bytes() == b"original"
    native = archive_fs.publish

    def obstruct(*args, **kwargs):
        raise ArchiveError("Synthetic concurrent collision")

    monkeypatch.setattr(archive_fs, "publish", obstruct)
    job = queue(catalog, request)
    assert perform(catalog, job)["counts"] == {"recovery": 1}
    assert source.read_bytes() == b"original" and not trash(db)["items"]
    monkeypatch.setattr(archive_fs, "publish", native)
    retry_recovery(db, job, "recover-test")
    assert perform(catalog, job)["counts"] == {"done": 1}
    assert not source.exists() and (root / "source_new.jpg").read_bytes() == b"original"


@pytest.mark.parametrize("operation", ["dng", "jpeg", "heic", "avif", "hevc", "av1"])
def test_real_verified_conversions(catalog, operation):
    pytest.importorskip("PIL")
    pytest.importorskip("pillow_heif")
    formats("exiftool")
    _, root, export, db = catalog
    if operation == "dng":
        formats("dnglab")
        source = root / "raw.dng"
        synthetic_dng(source)
        kind, suffix = "raw", ".dng"
        mode = "overwrite"
    elif operation in {"hevc", "av1"}:
        formats("ffmpeg", "ffprobe")
        tool = "libx265" if operation == "hevc" else "libaom-av1"
        encoders = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"], text=True)
        if tool not in encoders or "libx264" not in encoders:
            pytest.skip(f"FFmpeg encoder {tool} or fixture encoder is unavailable")
        source = root / "clip.mp4"
        synthetic_video(source)
        kind, suffix = "video", ".mp4"
        mode = "remove" if operation == "hevc" else "export"
    else:
        source = root / "still.tiff"
        synthetic_image(source)
        kind, suffix = "image", {"jpeg": ".jpg", "avif": ".avif", "heic": ".heic"}[operation]
        mode = "export"
    old = source.read_bytes()
    stamp = 1_598_990_400_123_456_789
    os.utime(source, ns=(stamp, stamp))
    id_ = record(db, root, source, kind=kind)
    task = ArchiveRequest(
        operation=operation, mode=mode, preset="fast", filters=Filters(ids=(id_,))
    )
    results = perform(catalog, queue(catalog, task))
    assert results["counts"] == {"done": 1}, results
    destination = (root if mode == "overwrite" else export) / source.with_suffix(suffix).name
    assert destination.stat().st_mtime_ns == stamp and destination.stat().st_size < len(old)
    if mode == "export":
        assert source.read_bytes() == old
    else:
        entry = trash(db)["items"][0]
        assert Path(entry["files"][0]["path"]).read_bytes() == old
        assert source.exists() == (mode == "overwrite")
    assert not list(root.glob(".photocull-stage-*")) and not list(export.glob(".photocull-stage-*"))


@pytest.mark.parametrize("stage", ["conversion", "after_exchange"])
def test_sigkill_restart_recovers_without_losing_original(catalog, stage):
    pytest.importorskip("PIL")
    formats("exiftool", "dnglab")
    settings, root, _, db = catalog
    source = root / "input.dng"
    synthetic_dng(source)
    original = source.read_bytes()
    id_ = record(db, root, source, kind="raw")
    job = queue(
        catalog, ArchiveRequest(operation="dng", mode="overwrite", filters=Filters(ids=(id_,)))
    )
    marker = root.parent / "kill-marker"
    child = """
import sys, time
from pathlib import Path
from threading import Event
from photocull.config import Settings
from photocull.db import connect
from photocull.worker import archive
from photocull.worker import archive_fs

settings = Settings.model_validate_json(sys.argv[1])
marker = Path(sys.argv[2])
if sys.argv[3] == 'conversion':
    def interrupted(tools, source, target, request):
        target.write_bytes(b'partial')
        code = ('import os,sys,time; from pathlib import Path; '
                'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)')
        tools.run(sys.executable, '-c', code, marker)
    archive.convert = interrupted
else:
    native = archive_fs.rename
    def interrupted(*args, **kw):
        native(*args, **kw)
        if len(args) == 5 and args[-1] == archive_fs.RENAME_EXCHANGE:
            marker.write_text('exchanged')
            time.sleep(120)
    archive_fs.rename = interrupted
service = archive.ArchiveWork(settings, 'photos', Event())
with connect(service.db_path) as db:
    service.process(service.claim(db, 1)[0])
"""
    with subprocess.Popen(
        [sys.executable, "-c", child, settings.model_dump_json(), str(marker), stage],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    ) as proc:
        deadline = time.monotonic() + 25
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not marker.exists():
            proc.kill()
            message = proc.communicate()[1]
            pytest.fail(f"Worker did not reach interruption point: {message!r}")
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    if stage == "conversion":
        assert source.read_bytes() == original
        child_stat = Path(f"/proc/{int(marker.read_text())}/stat")
        deadline = time.monotonic() + 5
        while child_stat.is_file() and time.monotonic() < deadline:
            try:
                if child_stat.read_text().split(") ", 1)[1].startswith("Z "):
                    break
            except FileNotFoundError:
                break
            time.sleep(0.05)
        else:
            assert not child_stat.exists(), "Conversion child outlived its worker"
    else:
        assert source.stat().st_size < len(original)
    from photocull.worker.archive import ArchiveWork

    ArchiveWork(settings, "photos", Event()).recover()
    result = perform(catalog, job)
    assert result["counts"] == {"done": 1}, result
    assert Path(trash(db)["items"][0]["files"][0]["path"]).read_bytes() == original
    assert not list(root.glob(".photocull-stage-*"))


def test_duplicates_and_storage(catalog):
    _, root, _, db = catalog
    for name, kind, payload in (
        ("PAIR.jpg", "image", b"one"),
        ("pair.ARW", "raw", b"raw"),
        ("copy.jpg", "image", b"one"),
        ("similar.jpg", "image", b"two"),
    ):
        source = root / name
        source.write_bytes(payload)
        id_ = record(db, root, source, kind=kind)
        db.execute("INSERT INTO asset_scores VALUES(?,1,1,0,0,'0000000000000000',NULL)", (id_,))
    assert len(duplicates(db, "exact", 0)["items"][0]["assets"]) == 2
    assert len(duplicates(db, "near", 0)["items"][0]["assets"]) == 4
    assert len(duplicates(db, "pairs", 0)["items"][0]["assets"]) == 2
    assert storage(db, "format")["total"]["bytes"] == 12
    assert {row["dimension"] for row in storage(db, "format")["items"]} == {".arw", ".jpg"}
    assert json.dumps(storage(db, "camera")).count("ILCE-7CM2") == 1


def test_api_imports_no_media_runtime():
    code = """
import sys
from photocull.api import create_app
assert any(route.path.endswith('/archive/preview') for route in create_app().routes)
assert not any(name.startswith(('photocull.worker', 'PIL', 'numpy', 'onnxruntime', 'rawpy'))
               for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_automatic_retention_requires_config(catalog):
    from photocull.worker.archive import ArchiveWork

    settings, root, export, db = catalog
    source = root / "expired.jpg"
    source.write_bytes(b"to expire")
    id_ = record(db, root, source)
    job = queue(catalog, ArchiveRequest(operation="trash", filters=Filters(ids=(id_,))))
    assert perform(catalog, job)["counts"] == {"done": 1}
    db.execute("UPDATE archive_trash SET created_at = 1")
    work = ArchiveWork(settings, "photos", Event())
    work.maintenance(db)
    assert len(jobs(db)) == 1 and len(trash(db)["items"]) == 1
    retention = settings.model_copy(
        update={
            "archive": settings.archive.model_copy(update={"retention_days": 1}),
        }
    )
    ArchiveWork(retention, "photos", Event()).maintenance(db)
    auto = jobs(db)[0]
    assert auto["actor"] == "retention" and auto["request"]["operation"] == "purge"
    assert perform((retention, root, export, db), auto["id"])["counts"] == {"done": 1}
    assert not trash(db)["items"]


def test_worker_read_only_root_disables_destruction_but_allows_export(catalog):
    settings, root, _, db = catalog
    source = root / "locked.jpg"
    source.write_bytes(b"read-only fixture")
    id_ = record(db, root, source)
    db.execute("INSERT INTO archive_permissions VALUES(?,0,?)", (str(root), time.time()))
    with pytest.raises(ArchiveError, match="read-only"):
        preview(
            db, settings, "photos", ArchiveRequest(operation="trash", filters=Filters(ids=(id_,)))
        )
    assert preview(db, settings, "photos", ArchiveRequest(operation="copy"))["count"] == 1


def test_cli_requires_settings_preview_and_yes(catalog, tmp_path):
    settings, root, _, db = catalog
    source = root / "cli.jpg"
    source.write_bytes(b"CLI fixture")
    id_ = record(db, root, source)
    config = tmp_path / "photocull-test.yaml"
    config.write_text(settings.model_dump_json())
    plan = tmp_path / "plan.json"
    plan.write_text(
        ArchiveRequest(operation="trash", filters=Filters(ids=(id_,))).model_dump_json()
    )
    # Keep the test CLI independent of any local .env.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PHOTOCULL_")}
    environment["PHOTOCULL_CONFIG"] = str(config)
    command = [
        sys.executable,
        "-c",
        "from photocull.cli import main; main()",
        "archive",
        "photos",
    ]
    dry = subprocess.run(
        [*command, "preview", str(plan)],
        env=environment,
        text=True,
        check=True,
        capture_output=True,
        cwd=tmp_path,
    )
    digest = json.loads(dry.stdout)["confirmation"]
    args = [*command, "apply", str(plan), "--confirmation", digest]
    blocked = subprocess.run(args, env=environment, text=True, capture_output=True, cwd=tmp_path)
    assert blocked.returncode == 2 and "--yes" in blocked.stderr
    assert not jobs(db) and source.exists()
    submitted = subprocess.run(
        [*args, "--yes"], env=environment, text=True, check=True, capture_output=True, cwd=tmp_path
    )
    assert jobs(db, json.loads(submitted.stdout)["id"])["actor"].startswith("cli:")
    assert source.exists()


def test_cli_audits_numeric_container_uid(catalog, tmp_path, monkeypatch):
    from photocull.archive_cli import run

    settings, root, _, db = catalog
    source = root / "container.jpg"
    source.write_bytes(b"Container UID fixture")
    id_ = record(db, root, source)
    request = ArchiveRequest(operation="trash", filters=Filters(ids=(id_,)))
    plan = tmp_path / "container.json"
    plan.write_text(request.model_dump_json())
    confirmation = preview(db, settings, "photos", request)["confirmation"]

    def missing_uid(uid):
        raise KeyError(uid)

    monkeypatch.setattr("photocull.archive_cli.pwd.getpwuid", missing_uid)
    args = SimpleNamespace(action="apply", library="photos", plan=plan, confirmation=confirmation)
    job = run(args, settings, settings.library_state("photos") / "index.db")
    assert jobs(db, job["id"])["actor"] == f"cli:uid={os.getuid()}"
