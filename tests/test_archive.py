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
from photocull.archive import ArchiveError, ArchiveRequest, Filters, capabilities, preview
from photocull.config import ArchiveSettings, Library, Settings
from photocull.db import connect, initialize
from photocull.db.archive import audit_page, cancel, enqueue, jobs, retry_recovery, trash
from photocull.db.archive_preview import build, page
from photocull.db.archive_preview import queue as queue_preview
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
        from photocull.worker.archive import ArchiveWork

        ArchiveWork(config, "photos", Event()).update_permissions(db)
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
    id_ = queue_preview(db, settings, "photos", request)
    build(settings.library_state("photos") / "index.db", settings, "photos", id_=id_)
    planned = page(db, id_)
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
        assert first.status_code == 202, first.text
        id_ = first.json()["id"]
        assert client.get(f"{base}/previews/{id_}").json()["status"] == "pending"
        build(settings.library_state("photos") / "index.db", disabled, "photos")
        snapshot = client.get(f"{base}/previews/{id_}").json()
        assert snapshot["count"] == 1
        assert db.total_changes == state_changes
        assert db.execute("SELECT count(*) FROM archive_jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM archive_audit").fetchone()[0] == 0
        assert not list(export.iterdir()) and source.stat().st_ctime_ns == original.st_ctime_ns
        source.write_bytes(b"changed out of band")
        submitted = client.post(
            f"{base}/jobs", json={"plan": plan, "confirmation": snapshot["confirmation"]}
        )
        assert submitted.status_code == 202
        result = perform((disabled, root, export, db), submitted.json()["id"])
        assert result["counts"] == {"skipped": 1}
        assert audit_page(db)["items"][1]["action"] == "skipped"


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
    assert db.execute("SELECT asset_id FROM archive_reindex WHERE ready=1").fetchone()[0] == id_
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
        db.execute(
            """INSERT INTO asset_scores
               (asset_id, sharpness, exposure, clipped_highlights, clipped_shadows, dhash)
               VALUES (?,1,1,0,0,'0000000000000000')""",
            (id_,),
        )
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
    db.execute("INSERT OR REPLACE INTO archive_permissions VALUES(?,0,?)", (str(root), time.time()))
    with pytest.raises(ArchiveError, match="self-test"):
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
    snapshot = queue_preview(db, settings, "photos", request)
    build(settings.library_state("photos") / "index.db", settings, "photos")
    confirmation = page(db, snapshot)["confirmation"]

    def missing_uid(uid):
        raise KeyError(uid)

    monkeypatch.setattr("photocull.archive_cli.pwd.getpwuid", missing_uid)
    args = SimpleNamespace(action="apply", library="photos", plan=plan, confirmation=confirmation)
    job = run(args, settings, settings.library_state("photos") / "index.db")
    assert jobs(db, job["id"])["actor"] == f"cli:uid={os.getuid()}"


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "dng", "mode": "overwrite"},
        {"operation": "hevc", "mode": "remove"},
        {"operation": "trash", "filters": {"ids": [1]}},
        {"operation": "rename", "mode": "overwrite"},
        {"operation": "rename"},
        {"operation": "purge", "trash_before": "2026-01-01"},
        {"operation": "restore", "trash_ids": ["a" * 32 + "-1"]},
        {"operation": "undo", "trash_ids": ["a" * 32 + "-1"]},
    ],
)
def test_no_write_roots_rejects_direct_http_and_cli(catalog, tmp_path, body):
    from photocull.archive_cli import run

    settings, _, output, db = catalog
    settings = settings.model_copy(
        update={"archive": ArchiveSettings(export_roots={"photos": output})}
    )
    with TestClient(create_app(settings)) as client:
        for path, payload in (
            ("preview", body),
            ("jobs", {"plan": body, "confirmation": "0" * 64}),
        ):
            response = client.post(f"/api/libraries/photos/archive/{path}", json=payload)
            assert response.status_code == 409
            assert "PHOTOCULL_WRITE_ROOTS" in response.json()["detail"]
    plan = tmp_path / "disabled.json"
    plan.write_text(json.dumps(body))
    for action in ("preview", "apply"):
        args = SimpleNamespace(action=action, library="photos", plan=plan, confirmation="0" * 64)
        with pytest.raises(ArchiveError, match="PHOTOCULL_WRITE_ROOTS"):
            run(args, settings, settings.library_state("photos") / "index.db")
    assert not jobs(db) and not list(output.iterdir())


def test_whole_manifest_confirmation_pages_and_conflicting_jobs(catalog):
    from photocull.db.archive import materialize

    settings, root, export, db = catalog
    for number in range(521):
        source = root / f"{number:04d}.jpg"
        source.write_bytes(b"snapshot fixture")
        record(db, root, source)
    plan = ArchiveRequest(operation="copy")
    job = queue(catalog, plan)
    saved = db.execute("SELECT id FROM archive_previews WHERE status='ready'").fetchone()[0]
    detail = page(db, saved)
    assert detail["count"] == detail["matched_count"] == 521
    assert detail["source_bytes"] == 521 * len(b"snapshot fixture")
    assert detail["next_cursor"] == 100 and len(detail["items"]) == 100
    assert page(db, saved, 500)["items"][-1]["relative"] == "0520.jpg"
    assert db.execute("SELECT count(*) FROM archive_items").fetchone()[0] == 0
    assert jobs(db, job)["counts"] == {"pending": 521}
    changed = root / "0000.jpg"
    changed.write_bytes(b"changed after confirmation")
    extra = root / "new.jpg"
    extra.write_bytes(b"new after confirmation")
    record(db, root, extra)
    assert enqueue(db, settings, "photos", plan, detail["confirmation"], "tester") == job
    second = queue_preview(db, settings, "photos", plan)
    build(settings.library_state("photos") / "index.db", settings, "photos")
    with pytest.raises(ArchiveError, match="active archive job"):
        enqueue(db, settings, "photos", plan, page(db, second)["confirmation"], "tester")
    materialize(db, size=3)
    result = perform(catalog, job)
    assert result["counts"] == {"done": 1, "pending": 519, "skipped": 1}
    assert (export / "0001.jpg").exists()
    assert not (export / "0000.jpg").exists() and not (export / "new.jpg").exists()
    assert audit_page(db)["items"][-1]["action"] in {"completed", "skipped"}
    cancel(db, job, "tester")
    result = perform(catalog, job)
    assert result["finished_at"] and result["counts"] == {"done": 1, "skipped": 1, "cancelled": 519}


def test_failed_probe_disables_writes_and_leaves_probe_directory_clean(catalog, monkeypatch):
    from photocull.worker import archive_selftest
    from photocull.worker.archive import ArchiveWork

    settings, root, _, db = catalog

    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "Synthetic unsupported atomic rename")

    monkeypatch.setattr(archive_selftest, "rename", unsupported)
    db.execute("DELETE FROM archive_write_checks")
    ArchiveWork(settings, "photos", Event()).update_permissions(db)
    with TestClient(create_app(settings)) as client:
        access = client.get("/api/libraries/photos/archive").json()
        assert not access["destructive"]
        result = access["write_checks"][0]
        assert not result["passed"] and "unsupported" in result["error"]
        assert result["trash"].endswith("/.photocull-trash")
    assert not list(root.iterdir())


def test_real_cross_mount_trash_and_missing_vault(catalog):
    from tempfile import TemporaryDirectory

    from photocull.worker.archive import ArchiveWork
    from photocull.worker.archive_selftest import describe

    settings, root, output, db = catalog
    shm = Path("/dev/shm")
    if not shm.is_dir() or shm.stat().st_dev == root.stat().st_dev or not os.access(shm, os.W_OK):
        pytest.skip("No separate writable temporary filesystem")
    with TemporaryDirectory(prefix="photocull-test-vault-", dir=shm) as tmp:
        vault_parent = Path(tmp)
        vault = vault_parent / "vault"
        settings = settings.model_copy(
            update={
                "archive": ArchiveSettings(
                    write_roots=(root, vault_parent),
                    export_roots={"photos": output},
                    trash_roots={"photos": vault},
                )
            }
        )
        ArchiveWork(settings, "photos", Event()).update_permissions(db)
        assert describe(settings, "photos", root)["trash_same_mount"] is False
        catalog = settings, root, output, db
        source = root / "original.jpg"
        source.write_bytes(b"cross mount fixture")
        id_ = record(db, root, source)
        job = queue(catalog, ArchiveRequest(operation="trash", filters=Filters(ids=(id_,))))
        assert perform(catalog, job)["counts"] == {"done": 1}
        entry = trash(db)
        assert entry["total_count"] == 1 and entry["total_bytes"] == len(b"cross mount fixture")
        assert not source.exists() and vault.is_dir()
        shutil.rmtree(vault)
        planned = preview(
            db,
            settings,
            "photos",
            ArchiveRequest(
                operation="restore",
                trash_ids=(entry["items"][0]["id"],),
            ),
        )
        assert planned["count"] == 0 and "missing" in planned["skipped"][0]["reason"]
        assert trash(db)["total_count"] == 1


def test_purge_cutoff_previews_oldest_first_and_requires_confirmation(catalog):
    from datetime import date

    settings, root, _, db = catalog
    for n in range(3):
        source = root / f"{n}.jpg"
        source.write_bytes(bytes([n]) * 11)
        id_ = record(db, root, source)
        job = queue(catalog, ArchiveRequest(operation="trash", filters=Filters(ids=(id_,))))
        assert perform(catalog, job)["counts"] == {"done": 1}
        db.execute("UPDATE archive_trash SET created_at=? WHERE job_id=?", (320 + (2 - n), job))
    result = trash(db)
    assert result["total_count"] == 3 and result["total_bytes"] == 33
    plan = ArchiveRequest(operation="purge", trash_before=date(1970, 1, 2))
    id_ = queue_preview(db, settings, "photos", plan)
    build(settings.library_state("photos") / "index.db", settings, "photos")
    result = page(db, id_)
    assert [Path(row["relative"]).name for row in result["items"]] == ["2.jpg", "1.jpg", "0.jpg"]
    assert result["estimated_after_purge_bytes"] == 33 and trash(db)["total_count"] == 3


def test_rule_scheduler_only_prepares_previews(catalog):
    from photocull.db.archive_rules import ArchiveRule, action, rules, save, schedule

    settings, root, _, db = catalog
    source = root / "clip.mp4"
    source.write_bytes(b"rule candidate")
    record(db, root, source, kind="video")
    rule = ArchiveRule(name="Older video", plan=ArchiveRequest(operation="hevc", mode="overwrite"))
    id_ = save(db, rule)
    schedule(db, settings, "photos")
    assert rules(db)[0]["preview_id"] is None and not jobs(db)
    action(db, id_, "preview")
    schedule(db, settings, "photos")
    build(settings.library_state("photos") / "index.db", settings, "photos")
    queued = rules(db)[0]
    assert queued["status"] == "ready" and queued["unread"] and queued["summary"]["count"] == 1
    action(db, id_, "seen")
    assert not rules(db)[0]["unread"] and not jobs(db) and source.read_bytes() == b"rule candidate"
    scheduled = save(db, rule.model_copy(update={"name": "Scheduled", "interval_hours": 24}))
    schedule(db, settings, "photos")
    next_run = next(row for row in rules(db) if row["id"] == scheduled)
    assert next_run["next_at"] > time.time() + 86000 and next_run["status"] == "pending"
    action(db, scheduled, "pause")
    assert next(row for row in rules(db) if row["id"] == scheduled)["next_at"] is None


@pytest.mark.parametrize("embedded", ["embed", "omit"])
def test_dng_preview_policy_is_verified(catalog, embedded):
    formats("dnglab", "exiftool")
    pytest.importorskip("PIL")
    _, root, export, db = catalog
    source = root / "preview.dng"
    synthetic_dng(source)
    id_ = record(db, root, source, kind="raw")
    task = ArchiveRequest(operation="dng", dng_preview=embedded, filters=Filters(ids=(id_,)))
    outcome = perform(catalog, queue(catalog, task))
    assert outcome["counts"] == {"done": 1}, outcome
    result = subprocess.check_output(["exiftool", "-b", "-PreviewImage", export / source.name])
    assert bool(result) == (embedded == "embed")
    if result:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(result)) as image:
            image.load()
            assert image.format == "JPEG"


def test_reindex_overwrite_preserves_review_and_refreshes_analysis(catalog):
    formats("dnglab", "exiftool")
    pytest.importorskip("rawpy")
    pytest.importorskip("usearch")
    from photocull.config import InsightSettings, MLSettings, WorkerSettings
    from photocull.db.jobs import request_scan
    from tests.fake_embedding import FakeEmbedding
    from tests.fake_faces import FakeFaces
    from tests.worker_harness import start_worker, wait_for

    settings, source, _, db = catalog
    settings = settings.model_copy(
        update={
            "worker": WorkerSettings(memory_mb=3072),
            "insights": InsightSettings(
                faces=True, watch=True, watch_mode="poll", watch_interval=1, watch_debounce=0.2
            ),
            "ml": MLSettings(enabled=True, offline=True, tags_enabled=False),
        }
    )
    for n in range(2):
        path = source / f"{n}.dng"
        synthetic_dng(path)
        subprocess.run(
            ["exiftool", "-overwrite_original", f"-DateTimeOriginal=2022:01:01 12:00:0{n}", path],
            capture_output=True,
            check=True,
        )
    request_scan(db)
    with start_worker(settings, model_factory=FakeEmbedding, face_factory=FakeFaces):
        asset = wait_for(lambda: db.execute("SELECT * FROM assets WHERE path='0.dng'").fetchone())
        id_ = asset["id"]
        old_hash = asset["content_hash"]
        face = wait_for(
            lambda: db.execute("SELECT * FROM faces WHERE asset_id=?", (id_,)).fetchone()
        )
        wait_for(
            lambda: db.execute("SELECT 1 FROM asset_embeddings WHERE asset_id=?", (id_,)).fetchone()
        )
        # Persist the user's review decisions before the archive snapshot.
        db.execute("UPDATE people SET name='Archive fixture' WHERE id=?", (face["person_id"],))
        db.execute("UPDATE faces SET manual=1 WHERE id=?", (face["id"],))
        db.execute("UPDATE assets SET rating=4,revision=revision+1 WHERE id=?", (id_,))
        db.execute("INSERT INTO burst_choices VALUES(?,100)", (id_,))
        wait_for(lambda: capabilities(db, settings, "photos")["destructive"])
        plan = ArchiveRequest(operation="dng", mode="overwrite", filters=Filters(ids=(id_,)))
        job = queue((settings, source, None, db), plan)
        assert wait_for(
            lambda: value if (value := jobs(db, job))["finished_at"] else None, timeout=30
        )["counts"] == {"done": 1}
        replacement = wait_for(
            lambda: db.execute(
                "SELECT * FROM assets WHERE path='0.dng' AND content_hash<>?",
                (old_hash,),
            ).fetchone()
        )
        assert replacement["id"] == id_ and replacement["rating"] == 4
        assert db.execute("SELECT 1 FROM burst_choices WHERE asset_id=?", (id_,)).fetchone()
        assert wait_for(
            lambda: db.execute(
                "SELECT 1 FROM asset_embeddings WHERE asset_id=? AND content_hash=?",
                (id_, replacement["content_hash"]),
            ).fetchone(),
            timeout=20,
        )
        refreshed = wait_for(
            lambda: db.execute(
                "SELECT * FROM faces WHERE asset_id=? AND content_hash=?",
                (id_, replacement["content_hash"]),
            ).fetchone(),
            timeout=20,
        )
        assert refreshed["id"] == face["id"] and refreshed["person_id"] == face["person_id"]
        assert refreshed["manual"] == 1
        assert (
            db.execute("SELECT name FROM people WHERE id=?", (refreshed["person_id"],)).fetchone()[
                0
            ]
            == "Archive fixture"
        )
        assert (
            db.execute("SELECT 1 FROM assets WHERE path LIKE '%.photocull-trash/%'").fetchone()
            is None
        )


def test_archive_directories_are_excluded_from_watch_and_scan(catalog, monkeypatch):
    events = pytest.importorskip("watchdog.events")
    observers = pytest.importorskip("watchdog.observers")
    from photocull.config import InsightSettings
    from photocull.worker.insights.watch import Watch

    settings, root, _, db = catalog
    settings = settings.model_copy(update={"insights": InsightSettings(watch=True)})
    source = root / "original.jpg"
    source.write_bytes(b"index fixture")
    record(db, root, source)
    captured = []

    class Observer:
        def schedule(self, handler, root, recursive):
            captured.append(handler)

        def start(self):
            pass

    monkeypatch.setattr(observers, "Observer", Observer)
    worker = SimpleNamespace(settings=settings, library=settings.libraries["photos"], db=db)
    watcher = Watch(worker)
    watcher.start()
    for directory in (".PhotoCull-Trash", ".photocull-stage-selftest-fixture"):
        silo = root / directory
        silo.mkdir()
        hidden = silo / "photo.jpg"
        hidden.write_bytes(b"excluded synthetic content")
        captured[0].on_any_event(events.FileCreatedEvent(str(hidden)))
    assert watcher.generation == 0 and not watcher.differs(lambda: None)
    captured[0].on_any_event(events.FileModifiedEvent(str(source)))
    assert watcher.generation == 1


def test_publish_waits_for_scan_and_relinks_verified_rename(catalog, monkeypatch):
    from threading import Lock

    from photocull.db.jobs import request_scan
    from photocull.worker import LibraryWorker
    from photocull.worker.archive import ArchiveWork

    settings, root, _, db = catalog
    source = root / "original.jpg"
    source.write_bytes(b"interleaved scan fixture")
    id_ = record(db, root, source, rating=4)
    job = queue(
        catalog,
        ArchiveRequest(
            operation="rename",
            mode="overwrite",
            template="{stem}_renamed",
            filters=Filters(ids=(id_,)),
        ),
    )
    gate = Lock()
    archive = ArchiveWork(settings, "photos", Event(), gate)
    index = LibraryWorker(
        db,
        settings.libraries["photos"],
        settings.library_state("photos"),
        settings,
        lambda: None,
        archive_gate=gate,
    )
    request_scan(db)
    assert index.step_scan() and gate.locked()
    staged = Event()
    native = archive.prepare

    def signal_staged(*args):
        plan = native(*args)
        staged.set()
        return plan

    monkeypatch.setattr(archive, "prepare", signal_staged)
    target = root / "original_renamed.jpg"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(archive.process, archive.claim(db, limit=1)[0])
        try:
            assert staged.wait(8)
            assert source.exists() and not target.exists()
            assert [event["action"] for event in audit_page(db)["items"]] == ["confirmed"]
        finally:
            while index.scan is not None:
                index.step_scan()
        future.result(timeout=8)
    assert not source.exists() and target.exists()
    archive.settle(db)
    while index.step_scan():
        pass
    renamed = db.execute("SELECT id,path,rating FROM assets WHERE active=1").fetchall()
    assert [tuple(row) for row in renamed] == [(id_, target.name, 4)]
    assert jobs(db, job)["counts"] == {"done": 1}


@pytest.mark.parametrize(
    ("operation", "name", "kind", "fixture"),
    [
        ("jpeg", "photo.tiff", "image", synthetic_image),
        ("hevc", "clip.mov", "video", synthetic_video),
        ("dng", "camera.dng", "raw", synthetic_dng),
    ],
)
def test_location_free_conversion_preserves_format(catalog, operation, name, kind, fixture):
    formats("exiftool", *({"hevc": ("ffmpeg", "ffprobe"), "dng": ("dnglab",)}.get(operation, ())))
    _, root, export, db = catalog
    source = root / name
    fixture(source)
    if kind == "raw":
        subprocess.run(
            [
                "exiftool",
                "-overwrite_original",
                "-GPSLatitude=35.25",
                "-GPSLatitudeRef=N",
                "-GPSLongitude=139.25",
                "-GPSLongitudeRef=E",
                source,
            ],
            check=True,
            capture_output=True,
        )
    assert subprocess.check_output(["exiftool", "-n", "-GPSLatitude", source]).strip()
    body = source.read_bytes()
    id_ = record(db, root, source, kind=kind)
    plan = ArchiveRequest(operation=operation, filters=Filters(ids=(id_,)), include_location=False)
    result = perform(catalog, queue(catalog, plan))
    assert result["counts"] == {"done": 1}, result
    [output] = export.iterdir()
    assert not subprocess.check_output(
        ["exiftool", "-n", "-GPSLatitude", "-GPSLongitude", output]
    ).strip()
    assert source.read_bytes() == body
