from __future__ import annotations

import json
import shutil
import subprocess
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photocull.api import create_app
from photocull.config import ArchiveSettings, BrowserSettings, InsightSettings, load_settings
from photocull.db import MIGRATIONS, connect, initialize
from photocull.media.derivatives import VARIANTS, derivative_path
from photocull.media.exif import read_metadata
from photocull.media.export_location import LocationSkip
from photocull.media.images import register_heif
from photocull.worker.archive_media import Tools, raw_parameters
from photocull.worker.location import jpeg_exif, strip_copy
from tests.archive_fixtures import synthetic_dng, synthetic_image, synthetic_video
from tests.test_derivatives import BASE, media_path, ready, scan, settings_for, version
from tests.worker_harness import start_worker, wait_for


def has_gps(data):
    with Image.open(BytesIO(data)) as image:
        return bool(image.getexif().get_ifd(34853))


def exif_write(path, *tags):
    subprocess.run(
        ["exiftool", "-overwrite_original", *tags, path],
        check=True,
        capture_output=True,
    )


def exif_read(path, *tags):
    return json.loads(subprocess.check_output(["exiftool", "-j", "-n", *tags, path]))[0]


def xmp_fixture(path):
    subprocess.run(
        [
            "exiftool",
            "-o",
            path,
            "-XMP-exif:GPSLatitude=35.25 N",
            "-XMP-photoshop:City=Kyoto",
            "-XMP-xmp:Rating=4",
            "-XMP-dc:Title=Keep this title",
        ],
        check=True,
        capture_output=True,
    )


def check_xmp(path, include):
    data = exif_read(path, "-GPSLatitude", "-City", "-Rating", "-Title")
    assert ("GPSLatitude" in data) is include
    assert ("City" in data) is include
    assert data["Rating"] == 4
    assert data["Title"] == "Keep this title"


def test_location_config_yaml_and_env(tmp_path, monkeypatch):
    config = tmp_path / "test.yaml"
    monkeypatch.setenv("PHOTOCULL_CONFIG", str(config))
    monkeypatch.delenv("PHOTOCULL_BROWSER_STRIP_LOCATION", raising=False)
    config.write_text("{}")
    assert load_settings().browser.strip_location
    config.write_text("browser:\n  strip_location: false\n")
    assert not load_settings().browser.strip_location
    monkeypatch.setenv("PHOTOCULL_BROWSER_STRIP_LOCATION", "true")
    assert load_settings().browser.strip_location


@pytest.mark.skipif(not shutil.which("exiftool"), reason="ExifTool is required")
@pytest.mark.parametrize("strip_location", [True, False])
def test_location_delivery_derived_original_and_exports(tmp_path, strip_location):
    config = settings_for(tmp_path, original_download=True)
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    settings = config.model_copy(
        update={
            "browser": BrowserSettings(strip_location=strip_location),
            "insights": InsightSettings(export=True),
            "archive": ArchiveSettings(export_roots={"photos": archive_root}),
        }
    )
    source = settings.libraries["photos"].root / "Location.jpg"
    synthetic_image(source, (384, 256))
    sidecar = source.with_suffix(".xmp")
    xmp_fixture(sidecar)
    original, original_xmp = source.read_bytes(), sidecar.read_bytes()
    check_xmp(sidecar, True)

    with TestClient(create_app(settings)) as client, start_worker(settings):
        asset = scan(client)[source.name]
        root = f"{BASE}/assets/{asset['id']}"
        assert client.get("/api/browse/media").json()["strip_location"] is strip_location
        assert client.get(root + "/original").status_code == 422
        assert client.get(root + "/original?include_location=true").content == original
        assert client.get(root + "/original?include_location=false").status_code == 422
        assert has_gps(original)
        path = root + "/delivery/" + version(settings, asset)
        keep_url = path + "?action=download&include_location=true"
        clean_url = path + "?action=download&include_location=false"
        for action in ("share", "download"):
            for override in (None, True, False):
                query = f"?action={action}"
                if override is not None:
                    query += f"&include_location={str(override).lower()}"
                output = ready(client, path + query)
                expect = not strip_location if override is None else override
                assert has_gps(output.content) is expect
                assert output.headers["Cache-Control"] == "private, no-store"
                assert ("Content-Disposition" in output.headers) is (action == "download")
        assert ready(client, keep_url).headers["ETag"] != ready(client, clean_url).headers["ETag"]
        assert client.get(media_path(settings, asset, "location-photo.jpg")).status_code == 404
        disk = derivative_path(
            settings,
            "photos",
            asset["id"],
            VARIANTS["location-photo.jpg"],
            version(settings, asset),
        )
        assert has_gps(disk.read_bytes())
        assert not has_gps(ready(client, media_path(settings, asset, "fit-2560.jpg")).content)
        assert client.get(clean_url, headers={"Range": "bytes=0-3"}).status_code == 206

        options = {"selection": {"minimum_rating": 0}}
        for override in (None, False, True):
            option = {} if override is None else {"include_location": override}
            expect = not strip_location if override is None else override
            body = {"kind": "zip", "xmp": True, **options, **option}
            keeper_preview = client.post(BASE + "/insights/exports/preview", json=body).json()
            assert keeper_preview == {
                "total": 1,
                "skipped": 0,
                "items": [],
                "next_cursor": None,
                "include_location": expect,
            }
            start = client.post(BASE + "/insights/exports", json=body)
            start.raise_for_status()
            transfer = start.json()["id"]

            def transferred(transfer=transfer):
                rows = client.get(BASE + "/insights/transfers?kind=export").json()["items"]
                item = next(row for row in rows if row["id"] == transfer)
                assert item["status"] != "error", item
                return item if item["status"] == "done" else None

            assert wait_for(transferred, timeout=15)["include_location"] is expect
            zip_bytes = client.get(BASE + f"/insights/transfers/{transfer}/download").content
            with ZipFile(BytesIO(zip_bytes)) as archive:
                photo = next(name for name in archive.namelist() if name.endswith(".jpg"))
                assert has_gps(archive.read(photo)) is expect
                xmp = next(name for name in archive.namelist() if name.endswith(".xmp"))
                extracted = tmp_path / "keeper.xmp"
                extracted.write_bytes(archive.read(xmp))
                check_xmp(extracted, expect)
                if expect:
                    assert archive.read(photo) == original
                    assert archive.read(xmp) == original_xmp
                else:
                    with (
                        Image.open(BytesIO(original)) as before,
                        Image.open(BytesIO(archive.read(photo))) as after,
                    ):
                        assert before.tobytes() == after.tobytes()

            archive_path = BASE + "/archive"
            plan = {"operation": "copy", "filters": {"ids": [asset["id"]]}, **option}
            preview = client.post(archive_path + "/preview", json=plan)
            preview.raise_for_status()
            handle = preview.json()["id"]

            def previewed(archive_path=archive_path, handle=handle):
                status = client.get(archive_path + "/previews/" + handle).json()
                assert status["status"] not in {"error", "failed"}, status
                return status if status["status"] == "ready" else None

            preview_result = wait_for(previewed, timeout=15)
            assert preview_result["include_location"] is expect
            confirmation = preview_result["confirmation"]
            wrong_plan = plan | {"include_location": not expect}
            assert (
                client.post(
                    archive_path + "/jobs",
                    json={
                        "plan": wrong_plan,
                        "confirmation": confirmation,
                    },
                ).status_code
                == 409
            )
            job = client.post(
                archive_path + "/jobs",
                json={
                    "plan": plan,
                    "confirmation": confirmation,
                },
            )
            job.raise_for_status()
            job_id = job.json()["id"]

            def published(archive_path=archive_path, job_id=job_id):
                status = client.get(archive_path + "/jobs/" + job_id).json()
                return status if status["finished_at"] else None

            completed = wait_for(published, timeout=20)
            assert completed["counts"] == {"done": 1}
            assert completed["request"]["include_location"] is expect
            exported = archive_root / source.name
            assert has_gps(exported.read_bytes()) is expect
            exported_sidecar = archive_root / sidecar.name
            check_xmp(exported_sidecar, expect)
            if expect:
                assert exported.read_bytes() == original
                assert exported_sidecar.read_bytes() == original_xmp
            exported.unlink()
            exported_sidecar.unlink()
    assert source.read_bytes() == original
    assert sidecar.read_bytes() == original_xmp


@pytest.mark.skipif(
    not shutil.which("exiftool") or not shutil.which("ffmpeg"),
    reason="ExifTool and FFmpeg are required",
)
def test_video_location_variants_do_not_share_the_playback_cache(tmp_path):
    settings = settings_for(tmp_path)
    source = settings.libraries["photos"].root / "Location.mov"
    synthetic_video(source, duration=1)
    with TestClient(create_app(settings)) as client, start_worker(settings):
        asset = scan(client)[source.name]
        delivery = f"{BASE}/assets/{asset['id']}/delivery/{version(settings, asset)}"
        clean = media_path(settings, asset, "video-720.mp4")
        for name, url, present in (
            ("keep", delivery + "?action=share&include_location=true", True),
            ("clean", clean, False),
            ("download", delivery + "?action=download&include_location=false", False),
        ):
            output = tmp_path / (name + ".mp4")
            output.write_bytes(ready(client, url).content)
            data = json.loads(
                subprocess.check_output(
                    ["exiftool", "-j", "-n", "-GPSLatitude", "-GPSLongitude", output],
                )
            )[0]
            assert (data.get("GPSLatitude") is not None) is present
            assert (data.get("GPSLongitude") is not None) is present


@pytest.mark.parametrize(
    "coordinates", [(1.15, -1.15), (89.999999999, -179.999999999), (-35.5, 139.25)]
)
def test_jpeg_coordinates_carry_rounded_seconds(tmp_path, coordinates):
    path = tmp_path / "coordinates.jpg"
    with Image.new("RGB", (8, 8)) as image:
        image.save(path, exif=jpeg_exif(coordinates))
    result = read_metadata(str(path))
    assert result.latitude == pytest.approx(coordinates[0], abs=2e-8)
    assert result.longitude == pytest.approx(coordinates[1], abs=2e-8)
    with Image.open(path) as image:
        gps = image.getexif().get_ifd(34853)
        assert 0 <= gps[2][2] < 60 and 0 <= gps[4][2] < 60


@pytest.mark.parametrize("suffix", [".jpg", ".heic", ".tiff", ".dng", ".mov", ".mp4"])
def test_metadata_only_copy_keeps_format_metadata_and_essence(tmp_path, suffix):
    is_video = suffix in {".mov", ".mp4"}
    required = ["exiftool"]
    if suffix == ".dng":
        required.append("dnglab")
    if is_video:
        required.extend(("ffmpeg", "ffprobe"))
    if any(not shutil.which(tool) for tool in required):
        pytest.skip("Metadata tools are unavailable")
    source = tmp_path / f"original{suffix}"
    target = tmp_path / f"stripped{suffix}"
    if suffix == ".dng":
        synthetic_dng(source)
    elif is_video:
        synthetic_video(source)
        exif_write(
            source,
            "-Keys:Make=Photocull",
            "-Keys:Model=Synthetic",
            "-Keys:GPSCoordinates=35.25 139.25",
        )
    elif suffix == ".jpg":
        synthetic_image(source)
    else:
        register_heif()
        with Image.new("RGB", (80, 60), "green") as image:
            image.save(source, format="HEIF" if suffix == ".heic" else "TIFF")
    if not is_video:
        exif_write(
            source,
            "-GPSLatitude=35.25",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=139.25",
            "-GPSLongitudeRef=E",
            "-Make=Photocull",
            "-Model=Synthetic",
            "-DateTimeOriginal=2020:04:20 12:34:45",
            "-Orientation#=6",
            "-IPTC:City=Kyoto" if suffix == ".jpg" else "-XMP-photoshop:City=Kyoto",
        )
    exif_write(
        source,
        "-XMP-dc:Title=Keep this title",
        "-XMP-xmp:Rating=4",
        "-XMP-iptcExt:LocationCreatedCity=Kyoto",
        "-XMP-exif:GPSAltitude=15",
    )
    original = source.read_bytes()
    tools = Tools(ArchiveSettings(), lambda: None)
    queries = (
        "-FileType",
        "-GPS:all",
        "-GPSCoordinates",
        "-GPSAltitude",
        "-City",
        "-LocationCreatedCity",
        "-Rating",
        "-XMP-dc:Title",
        "-Make",
        "-Model",
        "-DateTimeOriginal",
        "-Orientation",
        "-QuickTime:CreateDate",
    )
    before = exif_read(source, *queries)
    strip_copy(tools, source, target)
    after = exif_read(target, *queries)
    before.pop("SourceFile")
    after.pop("SourceFile")
    for tag in (
        "GPSCoordinates",
        "GPSLatitude",
        "GPSLatitudeRef",
        "GPSLongitude",
        "GPSLongitudeRef",
        "GPSVersionID",
        "GPSAltitude",
        "City",
        "LocationCreatedCity",
    ):
        assert tag not in after
        before.pop(tag, None)
    assert before == after
    assert after["Make"] == "Photocull"
    assert after["Title"] == "Keep this title"
    assert after["Rating"] == 4
    assert source.read_bytes() == original
    if suffix == ".dng":
        assert raw_parameters(tools, source) == raw_parameters(tools, target)
    elif is_video:

        def packets(path):
            return subprocess.check_output(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    path,
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-f",
                    "streamhash",
                    "-hash",
                    "sha256",
                    "-",
                ]
            )

        assert packets(source) == packets(target)
    else:
        with Image.open(source) as original, Image.open(target) as clean:
            assert original.tobytes() == clean.tobytes()
            assert original.info.get("icc_profile") == clean.info.get("icc_profile")


@pytest.mark.skipif(not shutil.which("exiftool"), reason="ExifTool is required")
def test_raw_with_opaque_original_is_not_exported(tmp_path):
    source = tmp_path / "original.dng"
    synthetic_dng(source)
    payload = tmp_path / "embedded-original"
    payload.write_bytes(b"Synthetic embedded RAW payload with private metadata")
    exif_write(source, f"-OriginalRawFileData<={payload}", "-OriginalRawFileName=original.raw")
    assert exif_read(source, "-OriginalRawFileName")["OriginalRawFileName"] == "original.raw"
    output = tmp_path / "stripped.dng"
    with pytest.raises(LocationSkip, match="opaque private block or embedded original"):
        strip_copy(Tools(ArchiveSettings(), lambda: None), source, output)
    assert not output.exists()


def test_metadata_only_copy_unsupported_format_has_no_fallback(tmp_path):
    source = tmp_path / "original.png"
    target = tmp_path / "stripped.png"
    with pytest.raises(LocationSkip, match="unavailable for .png"):
        strip_copy(Tools(ArchiveSettings(), lambda: None), source, target)
    assert not target.exists()


@pytest.mark.skipif(not shutil.which("exiftool"), reason="ExifTool is required")
def test_read_only_rename_and_keeper_skip_reports(tmp_path):
    config = settings_for(tmp_path)
    archive_root, keepers = tmp_path / "archive", tmp_path / "keepers"
    archive_root.mkdir()
    keepers.mkdir()
    settings = config.model_copy(
        update={
            "insights": InsightSettings(export=True, export_roots={"keepers": keepers}),
            "archive": ArchiveSettings(export_roots={"photos": archive_root}),
        }
    )
    root = settings.libraries["photos"].root
    synthetic_image(root / "Safe.jpg")
    xmp_fixture(root / "Safe.jpg.xmp")
    synthetic_image(root / "Bad.jpg")
    (root / "Bad.xmp").write_bytes(b"Not a parseable XMP sidecar")
    with Image.new("RGB", (48, 48), "blue") as image:
        image.save(root / "Unsupported.png")
    originals = {
        entry.name: (entry.read_bytes(), entry.stat().st_mtime_ns) for entry in root.iterdir()
    }

    with TestClient(create_app(settings)) as client, start_worker(settings):
        assets = scan(client)
        assert client.get(BASE + "/archive").json()["destructive"] is False
        archive = BASE + "/archive"
        plan = {
            "operation": "rename",
            "template": "exported_{stem}",
            "filters": {"ids": [asset["id"] for asset in assets.values()]},
        }
        start = client.post(archive + "/preview", json=plan)
        start.raise_for_status()
        preview_url = archive + "/previews/" + start.json()["id"]

        def ready_preview():
            result = client.get(preview_url).json()
            assert result["status"] != "error", result
            return result if result["status"] == "ready" else None

        preview = wait_for(ready_preview)
        assert preview["include_location"] is False
        assert preview["skipped_count"] == 1
        [skipped] = preview["skipped"]
        assert skipped["path"] == "Unsupported.png"
        assert ".png" in skipped["reason"]
        submit = client.post(
            archive + "/jobs", json={"plan": plan, "confirmation": preview["confirmation"]}
        )
        submit.raise_for_status()
        job_url = archive + "/jobs/" + submit.json()["id"]

        def finished_archive():
            result = client.get(job_url).json()
            return result if result["finished_at"] else None

        job = wait_for(finished_archive, timeout=20)
        assert job["counts"] == {"done": 1, "skipped": 2}
        assert {item["path"] for item in job["items"] if item["status"] == "skipped"} == {
            "Unsupported.png",
            "Bad.jpg",
        }
        assert all(item["error"] for item in job["items"] if item["status"] == "skipped")
        assert sorted(path.name for path in archive_root.iterdir()) == [
            "exported_Safe.jpg",
            "exported_Safe.jpg.xmp",
        ]
        assert not has_gps((archive_root / "exported_Safe.jpg").read_bytes())
        check_xmp(archive_root / "exported_Safe.jpg.xmp", False)

        export = BASE + "/insights/exports"
        options = {
            "kind": "copy",
            "destination": "keepers",
            "xmp": True,
            "selection": {"minimum_rating": 0},
        }
        preview = client.post(export + "/preview", json=options).json()
        assert preview["total"] == 3 and preview["skipped"] == 1
        assert preview["items"] == [{"path": "Unsupported.png", "reason": skipped["reason"]}]
        assert preview["include_location"] is False
        job_id = client.post(export, json=options).json()["id"]
        transfers = BASE + "/insights/transfers"

        def finished_export():
            result = next(
                row
                for row in client.get(transfers + "?kind=export").json()["items"]
                if row["id"] == job_id
            )
            assert result["status"] != "error", result
            return result if result["status"] == "done" else None

        completed = wait_for(finished_export, timeout=20)
        assert (completed["processed"], completed["total"], completed["skipped"]) == (3, 3, 2)
        report = client.get(transfers + f"/{job_id}/skips").json()["items"]
        assert {item["path"] for item in report} == {"Unsupported.png", "Bad.jpg"}
        assert all(item["reason"] for item in report)
        outputs = sorted((keepers / job_id).iterdir())
        assert len(outputs) == 2
        assert not has_gps(outputs[0].read_bytes())
        check_xmp(outputs[1], False)
    assert {
        entry.name: (entry.read_bytes(), entry.stat().st_mtime_ns) for entry in root.iterdir()
    } == originals


def test_skip_migration_preserves_completed_transfers(tmp_path):
    path = tmp_path / "index.db"
    initialize(path, MIGRATIONS[:-1])
    with connect(path) as db:
        db.execute(
            "INSERT INTO transfers(id,kind,status,request,created_at,finished_at) "
            "VALUES(?,?,?,?,?,?)",
            ("legacy", "zip", "done", '{"kind":"zip","include_location":true}', 1, 2),
        )
    initialize(path)
    initialize(path)
    with connect(path) as db:
        old = db.execute("SELECT status, request, skipped FROM transfers").fetchone()
        assert tuple(old) == ("done", '{"kind":"zip","include_location":true}', 0)
        assert db.execute("SELECT count(*) FROM transfer_skips").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
