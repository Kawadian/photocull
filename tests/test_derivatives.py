from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import time
from contextlib import ExitStack
from io import BytesIO
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageCms
from PIL.TiffImagePlugin import IFDRational

from photocull.api import create_app
from photocull.api.media import CacheAccess
from photocull.config import DerivativeSettings, Library, ServerSettings, Settings, load_settings
from photocull.db import connect, initialize, write
from photocull.db.derivatives import DerivativeJob
from photocull.media.cache import cache_path
from photocull.media.color import DISPLAY_P3, SRGB
from photocull.media.derivatives import VARIANTS, derivative_path, recipe
from photocull.media.images import register_heif
from photocull.media.walk import source_key
from photocull.worker import derivatives as encoder
from photocull.worker.derivatives import Derivatives, _generate
from tests.archive_fixtures import synthetic_dng
from tests.worker_harness import start_worker, wait_for

BASE = "/api/libraries/photos"


def settings_for(tmp_path, **options):
    photos = tmp_path / "photos"
    photos.mkdir()
    return Settings(
        state_dir=tmp_path / "state",
        libraries={"photos": Library(root=photos)},
        derivatives=DerivativeSettings(
            hot_dir=tmp_path / "ssd-cache", large_dir=tmp_path / "hdd-cache", prewarm=0, **options
        ),
    )


def source_jpeg(path):
    orientation = Image.Exif()
    orientation[274] = 6
    orientation[34665] = {
        42036: "35 mm Prime",
        33434: IFDRational(1, 250),
        33437: IFDRational(28, 10),
        37386: IFDRational(35),
        34855: 200,
    }
    orientation[34853] = {1: "N", 2: (IFDRational(35), IFDRational(30), IFDRational(0))}
    with Image.new("RGB", (800, 480), (210, 58, 50)) as image:
        image.save(path, exif=orientation, icc_profile=DISPLAY_P3, quality=95)


def scan(client):
    assert client.post(BASE + "/scans").status_code == 202

    def assets():
        response = client.get(BASE + "/assets")
        response.raise_for_status()
        items = response.json()["items"]
        return (
            items if items and all(asset["preview_status"] == "done" for asset in items) else None
        )

    return {row["path"]: row for row in wait_for(assets, timeout=25)}


def version(settings, asset):
    return asset["content_hash"] + "-" + recipe(settings.derivatives)


def media_path(settings, asset, name):
    return f"{BASE}/assets/{asset['id']}/media/{name}/{version(settings, asset)}"


def ready(client, path):
    def check():
        response = client.get(path)
        assert response.status_code in (200, 202), response.text
        return response if response.status_code == 200 else None

    return wait_for(check, timeout=30)


def test_oriented_srgb_formats_placeholder_download_and_recovery(tmp_path):
    settings = settings_for(tmp_path, original_download=True)
    source = settings.libraries["photos"].root / "旅行の写真.jpg"
    source_jpeg(source)
    original = source.read_bytes()

    with TestClient(create_app(settings)) as client, start_worker(settings):
        asset = scan(client)[source.name]
        metadata = client.get(f"{BASE}/assets/{asset['id']}?details=browser").json()
        assert metadata["lens"] == "35 mm Prime"
        assert metadata["shutter_seconds"] == pytest.approx(0.004)
        assert metadata["aperture"] == pytest.approx(2.8)
        assert metadata["focal_length_mm"] == 35 and metadata["iso"] == 200
        paths = {
            name: media_path(settings, asset, name)
            for name in VARIANTS
            if name in ("grid-256.avif", "grid-512.webp", "fit-1920.jpg", "native.jpg")
        }
        for path in paths.values():
            response = client.get(path)
            assert response.status_code == 202
            assert response.headers["Cache-Control"] == "private, no-store"
            assert response.headers["Retry-After"] == "2"

        expected = {
            "grid-256.avif": ("AVIF", (154, 256), "image/avif"),
            "grid-512.webp": ("WEBP", (307, 512), "image/webp"),
            "fit-1920.jpg": ("JPEG", (480, 800), "image/jpeg"),
            "native.jpg": ("JPEG", (480, 800), "image/jpeg"),
        }
        for name, (format_, dims, mime) in expected.items():
            response = ready(client, paths[name])
            assert response.headers["Content-Type"] == mime
            assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
            assert response.headers["Accept-Ranges"] == "bytes"
            with Image.open(BytesIO(response.content)) as image:
                assert (image.format, image.size) == (format_, dims)
                assert image.getexif() == {}
                assert image.info["icc_profile"] == SRGB
                assert "xmp" not in image.info
                with Image.new("RGB", (1, 1), (210, 58, 50)) as src:
                    expected_pixel = ImageCms.profileToProfile(
                        src, BytesIO(DISPLAY_P3), BytesIO(SRGB), outputMode="RGB"
                    ).getpixel((0, 0))
                assert image.getpixel((0, 0)) == pytest.approx(expected_pixel, abs=8)

        path = paths["grid-256.avif"]
        response = ready(client, path)
        etag, modified = response.headers["ETag"], response.headers["Last-Modified"]
        assert client.get(path, headers={"If-None-Match": "W/" + etag}).status_code == 304
        assert client.get(path, headers={"If-Modified-Since": modified}).status_code == 304
        assert (
            client.get(
                path,
                headers={
                    "If-None-Match": '"different"',
                    "If-Modified-Since": modified,
                },
            ).status_code
            == 200
        )
        assert client.head(path).headers["Content-Length"] == str(len(response.content))
        assert client.get(path, headers={"Range": "bytes=1-8"}).content == response.content[1:9]
        assert (
            client.get(
                path.replace(
                    version(settings, asset), "a" * 64 + "-" + recipe(settings.derivatives)
                )
            ).status_code
            == 404
        )
        item = client.get("/api/browse/timeline").json()["items"][0]
        assert len(item["blurhash"]) == 28
        disk = derivative_path(
            settings, "photos", asset["id"], VARIANTS["grid-256.avif"], version(settings, asset)
        )
        disk.unlink()
        assert client.get(path).status_code == 202
        assert ready(client, path).content == response.content

        download = f"{BASE}/assets/{asset['id']}/original"
        chunk = client.get(download + "?include_location=true", headers={"Range": "bytes=4-17"})
        assert chunk.status_code == 206
        assert chunk.content == original[4:18]
        assert "filename*=utf-8''" in chunk.headers["Content-Disposition"]
        assert chunk.headers["Cache-Control"] == "private, no-store"
        assert source.read_bytes() == original
        source.write_bytes(b"updated after the scan")
        assert client.get(download + "?include_location=true").status_code == 404


def test_raw_heif_and_video_poster_and_both_faststart_proxies(tmp_path):
    settings = settings_for(tmp_path, original_streaming=True)
    root = settings.libraries["photos"].root
    synthetic_dng(root / "sensor.DNG")
    register_heif()
    with Image.new("RGB", (96, 60), (22, 100, 177)) as image:
        image.save(
            root / "camera.HIF", format="HEIF", color_primaries=12, transfer_characteristics=13
        )
    clip = root / "clip.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=1280x720:r=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=400",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            "-threads",
            "1",
            "-c:a",
            "aac",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )

    with TestClient(create_app(settings)) as client, start_worker(settings):
        assets = scan(client)
        for name in assets:
            poster = ready(client, media_path(settings, assets[name], "grid-256.webp"))
            with Image.open(BytesIO(poster.content)) as image:
                assert image.format == "WEBP"
                assert max(image.size) <= 256
                assert image.info["icc_profile"] == SRGB
        row = assets[clip.name]
        for target_height in (480, 720):
            url = media_path(settings, row, f"video-{target_height}.mp4")
            output = ready(client, url)
            assert output.headers["Content-Type"] == "video/mp4"
            assert output.content.index(b"moov") < output.content.index(b"mdat")
            target = tmp_path / f"proxy-{target_height}.mp4"
            target.write_bytes(output.content)
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name,width,height",
                    "-of",
                    "json",
                    str(target),
                ],
                check=True,
                capture_output=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            assert streams[0]["codec_name"] == "h264" and streams[0]["height"] == target_height
            assert streams[1]["codec_name"] == "aac"
            part = client.get(url, headers={"Range": "bytes=0-31"})
            assert part.status_code == 206 and part.content == output.content[:32]

        url = f"{BASE}/assets/{row['id']}/original"
        assert client.get(url).status_code == 404
        original = client.get(
            url + "?mode=stream&include_location=true", headers={"Range": "bytes=0-31"}
        )
        assert original.status_code == 206
        assert original.content == clip.read_bytes()[:32]


def test_batched_touches_global_lru_and_disk_accounting(tmp_path):
    config = DerivativeSettings(hot_mib=16, large_mib=16, prewarm=0)
    settings = Settings(
        state_dir=tmp_path / "state",
        libraries={name: Library(root=tmp_path / name) for name in ("one", "two")},
        derivatives=config,
    )
    variant = VARIANTS["grid-256.webp"]
    version = "a" * 64 + "-" + recipe(config)
    targets = {}
    hits = CacheAccess()
    with ExitStack() as stack:
        workers = []
        for number, library in enumerate(settings.libraries, 1):
            state = settings.library_state(library)
            path = state / "index.db"
            initialize(path)
            db = stack.enter_context(connect(path))
            workers.append(SimpleNamespace(db=db, state=state))
            with write(db):
                db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
                db.execute(
                    """
                    INSERT INTO assets(id,path,folder,kind,size,mtime_ns,content_hash,scan_id,day)
                    VALUES (1,'dummy.jpg','','image',0,0,?,1,'2025-01-01')
                """,
                    ("a" * 64,),
                )
                db.execute(
                    """
                    INSERT INTO derivative_files
                    (asset_id,variant,version,tier,status,priority,requested_at,last_access,byte_size)
                    VALUES (1,?,?,'hot','ready',1,0,?,?)
                """,
                    (variant.name, version, number, 9 * 1024 * 1024),
                )
            target = derivative_path(settings, library, 1, variant, version)
            target.parent.mkdir(parents=True)
            with target.open("wb") as file:
                file.truncate(9 * 1024 * 1024)
            targets[library] = target

        hits.touch(settings.library_state("one") / "index.db", 1, variant.name, version)
        assert workers[0].db.execute("SELECT last_access FROM derivative_files").fetchone()[0] == 1
        hits.flush()
        assert (
            workers[0].db.execute("SELECT last_access FROM derivative_files").fetchone()[0]
            > time.time() - 5
        )

        cache = Derivatives(settings, workers, stack, Event())
        cache.step(available=True, idle=True)
        assert targets["one"].is_file()
        assert not targets["two"].exists()
        assert cache._usage("hot") == 9 * 1024 * 1024


def test_cache_roots_stay_outside_the_photo_library(tmp_path):
    root = tmp_path / "originals"
    with pytest.raises(ValueError, match="outside media"):
        Settings(
            libraries={"photos": Library(root=root)},
            state_dir=tmp_path / "state",
            derivatives=DerivativeSettings(hot_dir=root / "cache"),
        )
    with pytest.raises(ValueError, match="separate directories"):
        Settings(
            state_dir=tmp_path / "state",
            derivatives=DerivativeSettings(
                hot_dir=tmp_path / "cache", large_dir=tmp_path / "cache/large"
            ),
        )


def test_cached_media_rejects_symlinks_and_preserves_missing_detail_fallback(tmp_path):
    settings = settings_for(tmp_path)
    source = settings.libraries["photos"].root / "camera.jpg"
    source_jpeg(source)
    with TestClient(create_app(settings)) as client, start_worker(settings):
        asset = scan(client)[source.name]
        state = settings.library_state("photos")
        thumbnail = cache_path(state, "thumbs", asset["id"], asset["content_hash"])
        asset_url = f"{BASE}/assets/{asset['id']}/media/"
        preview = client.get(asset_url + "preview")
        assert preview.status_code == 200
        assert client.get(asset_url + "detail").content == preview.content
        thumbnail.unlink()
        thumbnail.symlink_to(source)
        assert client.get(asset_url + "thumb").status_code == 404


def test_cache_generation_and_cleanup_stay_inside_the_cache_tree(tmp_path):
    settings = settings_for(tmp_path)
    library = "photos"
    root = settings.libraries[library].root
    source = root / "camera.jpg"
    source_jpeg(source)
    variant = VARIANTS["grid-256.jpg"]
    revision = "a" * 64 + "-" + recipe(settings.derivatives)
    target = derivative_path(settings, library, 1, variant, revision)
    target.parent.parent.mkdir(parents=True)
    target.parent.symlink_to(root, target_is_directory=True)
    legacy_dir = settings.library_state(library)
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "thumbs").symlink_to(root, target_is_directory=True)
    protected = (root / ".derivative-existing", root / target.name)
    for file in protected:
        file.write_bytes(b"original fixture")
    before = sorted(file.name for file in root.iterdir())
    job = DerivativeJob(
        1, source.name, "image", source_key(source.stat()), revision, variant.name, 1
    )
    assert not _generate(settings, library, [job], lambda: None)[0]

    path = legacy_dir / "index.db"
    initialize(path)
    with ExitStack() as stack:
        db = stack.enter_context(connect(path))
        with write(db):
            db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
            db.execute(
                """INSERT INTO assets(id,path,folder,kind,size,mtime_ns,content_hash,scan_id,day)
                   VALUES (1,'camera.jpg','','image',0,0,?,1,'2025-01-01')""",
                ("a" * 64,),
            )
            db.execute(
                """INSERT INTO derivative_files
                   (asset_id,variant,version,tier,status,priority,requested_at,last_access,byte_size)
                   VALUES (1,?,?,'hot','running',1,0,0,0)""",
                (variant.name, revision),
            )
        Derivatives(settings, [SimpleNamespace(db=db, state=legacy_dir)], stack, Event())
    assert sorted(file.name for file in root.iterdir()) == before
    assert all(file.read_bytes() == b"original fixture" for file in protected)


def test_browser_configuration_defaults_and_external_state(tmp_path):
    settings = Settings()
    assert settings.browser.strip_location is True
    assert settings.derivatives.hot_mib == 1536
    assert settings.derivatives.large_mib == 4096
    assert settings.derivatives.prewarm == 64
    assert settings.derivatives.original_download is False
    assert settings.derivatives.original_streaming is False
    media = tmp_path / "library"
    with pytest.raises(ValueError, match="State directory must be outside media roots"):
        Settings(
            libraries={"photos": Library(root=media)},
            state_dir=media / ".photocull",
            derivatives=DerivativeSettings(enabled=False),
        )


def test_configured_cache_aliases_resolve_before_use(tmp_path):
    locations = {}
    for name in ("state", "hot", "large"):
        actual = tmp_path / f"actual-{name}"
        actual.mkdir()
        alias = tmp_path / name
        alias.symlink_to(actual, target_is_directory=True)
        locations[name] = alias
    photos = tmp_path / "photos"
    photos.mkdir()
    settings = Settings(
        state_dir=locations["state"],
        libraries={"photos": Library(root=photos)},
        derivatives=DerivativeSettings(
            hot_dir=locations["hot"], large_dir=locations["large"], prewarm=0
        ),
    )
    assert settings.state_dir == locations["state"].resolve()
    for tier in ("hot", "large"):
        assert settings.derivative_root(tier) == locations[tier].resolve()
    source_jpeg(photos / "fixture.jpg")
    with TestClient(create_app(settings)) as client, start_worker(settings):
        asset = scan(client)["fixture.jpg"]
        for name in ("grid-256.jpg", "fit-1280.jpg"):
            url = media_path(settings, asset, name)
            output = ready(client, url)
            assert client.get(url).content == output.content
    assert list(photos.iterdir()) == [photos / "fixture.jpg"]


def test_host_allowlist_covers_reads_with_tokens(tmp_path, monkeypatch):
    path = tmp_path / "example.yaml"
    path.write_text("{}")
    monkeypatch.setenv("PHOTOCULL_CONFIG", str(path))
    monkeypatch.setenv("PHOTOCULL_ALLOWED_HOSTS", "gallery.example, 127.0.0.1, ::1")
    server = load_settings().server
    assert server.allowed_hosts == ("gallery.example", "127.0.0.1", "::1")
    with pytest.raises(ValueError, match="without ports"):
        ServerSettings(allowed_hosts=("gallery.example:443",))
    token = tmp_path / "token"
    token.write_text("fixture-only")
    settings = settings_for(tmp_path).model_copy(update={"server": server, "token_file": token})
    headers = {"X-Photocull-Token": "fixture-only"}
    with TestClient(create_app(settings)) as client:
        for path in (
            "/api/health",
            "/api/browse/timeline",
            BASE + "/assets/1/original",
            BASE + "/assets/1/media/thumb",
            BASE + "/assets/1/delivery/" + "a" * 81,
            BASE + "/insights/transfers/" + "c" * 32 + "/download",
        ):
            assert (
                client.get(path, headers=headers | {"Host": "foreign.example"}).status_code == 421
            )
            assert client.get(path, headers={"Host": "gallery.example"}).status_code == 401
        for host in ("Gallery.EXAMPLE.:443", "127.0.0.1:8000", "[::1]:8000"):
            assert client.get("/api/health", headers=headers | {"Host": host}).status_code == 200
        for host in ("gallery.example@foreign.example", "gallery.example,foreign.example"):
            assert client.get("/api/health", headers=headers | {"Host": host}).status_code == 421


def test_api_reports_live_worker_recipe_mismatch(tmp_path, caplog):
    worker = settings_for(tmp_path)
    api = worker.model_copy(
        update={"derivatives": worker.derivatives.model_copy(update={"jpeg_quality": 74})}
    )
    source_jpeg(worker.libraries["photos"].root / "fixture.jpg")
    with TestClient(create_app(api)) as client, start_worker(worker):
        asset = scan(client)["fixture.jpg"]
        health = client.get("/api/health").json()
        assert health["derivative_recipe"] == recipe(api.derivatives)
        assert health["libraries"]["photos"]["derivatives"] == {
            "worker_recipe": recipe(worker.derivatives),
            "matches": False,
        }
        response = client.get(media_path(api, asset, "grid-256.jpg"))
        assert response.status_code == 503
        assert response.json()["status"] == "recipe_mismatch"
        assert "Derivative recipe mismatch" in caplog.text
        with connect(api.library_state("photos") / "index.db") as db:
            assert db.execute("SELECT count(*) FROM derivative_files").fetchone()[0] == 0
        with TestClient(create_app(worker)) as matching:
            health = matching.get("/api/health").json()
            assert health["libraries"]["photos"]["derivatives"]["matches"] is True
            assert ready(matching, media_path(worker, asset, "grid-256.jpg")).status_code == 200


def test_cache_access_loop_survives_a_failed_flush(monkeypatch, caplog):
    async def run():
        access = CacheAccess()
        loop = asyncio.get_running_loop()
        completed = asyncio.Event()
        calls = 0

        def flush():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("fixture write failure")
            loop.call_soon_threadsafe(completed.set)

        sleep = asyncio.sleep

        async def tick(_):
            await sleep(0)

        monkeypatch.setattr(access, "flush", flush)
        monkeypatch.setattr(asyncio, "sleep", tick)
        task = asyncio.create_task(access.run())
        await asyncio.wait_for(completed.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert "Could not flush derivative cache access timestamps" in caplog.text


def test_derivative_batch_keeps_accountable_outputs_and_rejects_an_oversized_file(
    tmp_path, monkeypatch
):
    settings = settings_for(tmp_path, hot_mib=16)
    source = settings.libraries["photos"].root / "fixture.jpg"
    source_jpeg(source)
    variants = ("grid-256.jpg", "grid-256.avif", "grid-256.webp")
    version = "a" * 64 + "-" + recipe(settings.derivatives)
    jobs = [
        DerivativeJob(1, source.name, "image", source_key(source.stat()), version, name, 1)
        for name in variants
    ]

    def encode(pixels, variant, settings, file, *, coordinates):
        if variant.extension == "avif":
            raise TypeError("unexpected encoder failure")
        if variant.extension == "webp":
            file.truncate(2 * 1024**2 + 1)
        else:
            file.write(b"fixture")

    monkeypatch.setattr(encoder, "encode_image", encode)
    sizes, _ = _generate(settings, "photos", jobs, lambda: None)
    assert sizes == {"grid-256.jpg": len(b"fixture")}
    for name in variants:
        target = derivative_path(settings, "photos", 1, VARIANTS[name], version)
        assert target.exists() is (name in sizes)
    assert not list(settings.derivative_root("hot").rglob(".derivative-*"))
