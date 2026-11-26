import hashlib
import os
import subprocess
import sys
from pathlib import Path
from xml.dom import minidom

import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import Library, Settings
from photocull.db import connect, initialize
from photocull.db.catalog import set_rating
from photocull.db.jobs import request_scan
from photocull.media import xmp
from photocull.media.walk import source_key
from photocull.media.xmp import RDF, XMP, XMPConflict, XMPError, export_xmp


@pytest.fixture
def indexed_photo(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    physical = tmp_path / "originals"
    physical.mkdir()
    (root / "2025").symlink_to(physical, target_is_directory=True)
    original = physical / "夕焼け.DNG"
    original.write_bytes(b"original fixture")
    settings = Settings(
        libraries={"sony": Library(root=root, allowed_roots=(physical,))},
        state_dir=tmp_path / "state",
    )
    path = settings.library_state("sony") / "index.db"
    initialize(path)
    with connect(path) as db:
        scan = request_scan(db)
        info = original.stat()
        db.execute(
            """INSERT INTO assets
               (path, folder, kind, day, size, mtime_ns, scan_id, content_hash, source_key)
               VALUES (?, '2025', 'raw', '2025-01-01', ?, ?, ?, ?, ?)""",
            (
                f"2025/{original.name}",
                info.st_size,
                info.st_mtime_ns,
                scan["id"],
                hashlib.sha256(original.read_bytes()).hexdigest(),
                source_key(info),
            ),
        )
        set_rating(db, 1, 5, False, 0)
    return settings, original, path


def test_xmp_dry_run_write_idempotence_and_original_bytes(indexed_photo):
    settings, original, _ = indexed_photo
    sidecar = original.with_suffix(".xmp")
    before = original.read_bytes()
    plan = export_xmp(settings, "sony", 1, dry_run=True)
    assert plan.sidecar == str(sidecar)
    assert plan.rating == 5
    assert plan.action == "create"
    assert not sidecar.exists()

    export_xmp(settings, "sony", 1, dry_run=False, confirmation=plan.confirmation)
    doc = minidom.parse(str(sidecar))
    desc = doc.getElementsByTagNameNS(RDF, "Description")[0]
    assert desc.getAttributeNS(XMP, "Rating") == "5"
    assert original.read_bytes() == before
    before_mtime = sidecar.stat().st_mtime_ns
    assert export_xmp(settings, "sony", 1, dry_run=False).action == "unchanged"
    assert sidecar.stat().st_mtime_ns == before_mtime


def test_xmp_preserves_lightroom_tags_and_element_rating(indexed_photo):
    settings, original, index = indexed_photo
    sidecar = original.with_suffix(".xmp")
    sidecar.write_text(
        f'''<?xpacket begin=" "?>
        <m:xmpmeta xmlns:m="adobe:ns:meta/" xmlns:r="{RDF}" xmlns:p="{XMP}">
        <r:RDF><r:Description r:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
                             crs:Exposure2012="+0.35">
        <!-- Keep this -->
        <p:Rating>3</p:Rating><p:Label>赤</p:Label></r:Description></r:RDF>
        </m:xmpmeta><?xpacket end="w"?>''',
        encoding="utf-8",
    )
    sidecar.chmod(0o640)
    with connect(index) as db:
        set_rating(db, 1, 0, True, 1)
    assert export_xmp(settings, "sony", 1, dry_run=False).action == "update"
    with minidom.parse(str(sidecar)) as doc:
        assert doc.getElementsByTagNameNS(XMP, "Rating")[0].firstChild.data == "1"
        assert doc.getElementsByTagNameNS(XMP, "Label")[0].firstChild.data == "赤"
    assert "<!-- Keep this -->" in sidecar.read_text()
    assert 'crs:Exposure2012="+0.35"' in sidecar.read_text()
    assert sidecar.stat().st_mode & 0o777 == 0o640

    with connect(index) as db:
        set_rating(db, 1, 0, False, 2)
    export_xmp(settings, "sony", 1, dry_run=False)
    assert "<p:Rating>0</p:Rating>" in sidecar.read_text()


def test_xmp_collision_read_only_and_sidecar_symlink(indexed_photo, monkeypatch):
    settings, original, _ = indexed_photo
    sidecar = original.with_suffix(".xmp")
    collision = original.with_suffix(".JPG")
    collision.touch()
    with pytest.raises(XMPError, match="shares the same sidecar"):
        export_xmp(settings, "sony", 1, dry_run=False)
    collision.unlink()

    sidecar.symlink_to(original)
    with pytest.raises(XMPError, match="symbolic link"):
        export_xmp(settings, "sony", 1, dry_run=False)
    sidecar.unlink()

    monkeypatch.setattr(xmp.os, "access", lambda *_args, **_kwargs: False)
    with pytest.raises(XMPError, match="directory is not writable"):
        export_xmp(settings, "sony", 1, dry_run=False)
    assert not sidecar.exists()


def test_xmp_requires_fresh_confirmation_and_refuses_changed_original(indexed_photo):
    settings, original, index = indexed_photo
    url = "/api/libraries/sony/assets/1/xmp"
    with TestClient(create_app(settings)) as client:
        plan = client.post(url, json={"dry_run": True}).json()
        assert client.post(url, json={"dry_run": False}).status_code == 422
        with connect(index) as db:
            set_rating(db, 1, 2, False, 1)
        assert (
            client.post(
                url, json={"dry_run": False, "confirmation": plan["confirmation"]}
            ).status_code
            == 409
        )
        assert not original.with_suffix(".xmp").exists()

        plan = client.post(url, json={}).json()
        assert (
            client.post(
                url, json={"dry_run": False, "confirmation": plan["confirmation"]}
            ).status_code
            == 200
        )
    original.write_bytes(b"new content")
    with pytest.raises(XMPConflict, match="Media changed after indexing"):
        export_xmp(settings, "sony", 1, dry_run=False)


def test_xmp_cli_explicit_write(indexed_photo, tmp_path):
    settings, original, _ = indexed_photo
    library = settings.libraries["sony"]
    env = {
        **os.environ,
        "PHOTOCULL_LIBRARY_ROOT": str(library.root),
        "PHOTOCULL_LIBRARY_NAME": "sony",
        "PHOTOCULL_STATE_DIR": str(settings.state_dir),
        "PHOTOCULL_ALLOWED_ROOTS": str(library.allowed_roots[0]),
    }
    for flag in ("--dry-run", "--write"):
        result = subprocess.run(
            [str(Path(sys.executable).with_name("photocull")), "xmp", "sony", "1", flag],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "夕焼け.xmp" in result.stdout
        assert original.with_suffix(".xmp").exists() == (flag == "--write")
