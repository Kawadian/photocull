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
from photocull.media.derivatives import VARIANTS, derivative_path
from photocull.media.exif import read_metadata
from photocull.media.images import register_heif
from photocull.worker.archive_media import Tools
from photocull.worker.location import jpeg_exif, strip_copy
from tests.archive_fixtures import synthetic_dng, synthetic_image, synthetic_video
from tests.test_derivatives import BASE, media_path, ready, scan, settings_for, version
from tests.worker_harness import start_worker, wait_for


def has_gps(data):
    with Image.open(BytesIO(data)) as image:
        return bool(image.getexif().get_ifd(34853))


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
    sidecar.write_text("Synthetic location: 35.25, 139.25")
    original = source.read_bytes()

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
        denied = {"kind": "zip", "xmp": True, "include_location": False, **options}
        assert client.post(BASE + "/insights/exports", json=denied).status_code == 422
        for override in (None, False, True):
            option = {} if override is None else {"include_location": override}
            expect = override is not False
            body = {"kind": "zip", **options, **option}
            if override is True:
                body["xmp"] = True
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
                assert any(name.endswith(".xmp") for name in archive.namelist()) is (
                    override is True
                )
                if expect:
                    assert archive.read(photo) == original
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

            confirmation = wait_for(previewed, timeout=15)["confirmation"]
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

            assert wait_for(published, timeout=15)["counts"] == {"done": 1}
            exported = archive_root / source.name
            assert has_gps(exported.read_bytes()) is expect
            exported.unlink()
            exported_sidecar = archive_root / sidecar.name
            assert exported_sidecar.exists() is expect
            if expect:
                exported_sidecar.unlink()
    assert source.read_bytes() == original
    assert "Synthetic location" in sidecar.read_text()


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


@pytest.mark.parametrize("suffix", [".heic", ".dng", ".mov"])
def test_metadata_only_copy_keeps_format_and_essence(tmp_path, suffix):
    required = {
        ".heic": ("exiftool",),
        ".dng": ("exiftool", "dnglab"),
        ".mov": ("exiftool", "ffmpeg", "ffprobe"),
    }
    if any(not shutil.which(tool) for tool in required[suffix]):
        pytest.skip("Metadata tools are unavailable")
    source = tmp_path / f"original{suffix}"
    target = tmp_path / f"stripped{suffix}"
    if suffix == ".dng":
        synthetic_dng(source)
    elif suffix == ".mov":
        synthetic_video(source)
    else:
        register_heif()
        with Image.new("RGB", (80, 60), "green") as image:
            image.save(source, format="HEIF", exif=jpeg_exif((35.25, 139.25)))
    if suffix == ".dng":
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
    original = source.read_bytes()
    tools = Tools(ArchiveSettings(), lambda: None)
    tags = ("exiftool", "-j", "-n", "-GPSLatitude", "-GPSCoordinates")
    assert len(json.loads(tools.run(*tags, source))[0]) > 1
    strip_copy(tools, source, target)
    assert len(json.loads(tools.run(*tags, target))[0]) == 1
    assert source.read_bytes() == original


def test_metadata_only_copy_unsupported_format_has_no_fallback(tmp_path):
    from photocull.archive import ArchiveError

    source = tmp_path / "original.png"
    target = tmp_path / "stripped.png"
    with pytest.raises(ArchiveError, match="explicit Archive conversion"):
        strip_copy(Tools(ArchiveSettings(), lambda: None), source, target)
    assert not target.exists()
