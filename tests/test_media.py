import hashlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from photocull.config import Library, Settings
from photocull.db import connect, initialize
from photocull.db.catalog import get_asset
from photocull.db.jobs import request_scan
from photocull.media import exif
from photocull.media.cache import cache_path
from photocull.media.decode import decode
from photocull.media.images import register_heif
from photocull.media.walk import MediaJail, OutsideLibrary, hash_file
from photocull.worker import LibraryWorker


def _index_media(root):
    library = Library(root=root)
    state = root / "state"
    path = state / "index.db"
    initialize(path)
    with connect(path) as db, ThreadPoolExecutor(1) as pool:
        worker = LibraryWorker(db, library, state, Settings(state_dir=state), lambda: None)
        request_scan(db)
        for _ in range(20):
            if not worker.step(pool, worker.beat):
                return get_asset(db, 1)
    raise AssertionError("Media queue did not finish")


def test_walk_filters_and_symlink_jail(tmp_path):
    root = tmp_path / "view"
    allowed = tmp_path / "originals"
    outside = tmp_path / "outside"
    state = root / "custom-state"
    for folder in (root, allowed, outside, state):
        folder.mkdir(exist_ok=True)
    for name in ("edit", "Edit", ".GIT", "__PYcache__", "node_modules", ".photocull"):
        directory = root / "nested" / name
        directory.mkdir(parents=True)
        (directory / "hidden.jpg").touch()
    (allowed / "2026").mkdir()
    (allowed / "2026/カメラ.HIF").write_bytes(b"media")
    (allowed / "edit").mkdir()
    (allowed / "edit/no.jpg").touch()
    (outside / "no.jpg").touch()
    (state / "generated.jpg").touch()
    (root / "direct.ARW").touch()
    (root / "clip.M4V").touch()
    (root / "notes.txt").touch()
    (root / "safe").symlink_to(allowed / "2026", target_is_directory=True)
    (root / "bad").symlink_to(outside, target_is_directory=True)
    (root / "bad.jpg").symlink_to(outside / "no.jpg")
    (root / "bypass").symlink_to(allowed / "edit", target_is_directory=True)
    (root / "edit-alias.jpg").symlink_to(allowed / "edit/no.jpg")
    (root / "loop").symlink_to(root, target_is_directory=True)
    (allowed / "2026/self").symlink_to(allowed / "2026", target_is_directory=True)
    (root / "dangling.jpg").symlink_to(root / "nowhere")
    (root / "cycle.jpg").symlink_to(root / "cycle.jpg")

    jail = MediaJail(Library(root=root, allowed_roots=(allowed,)), state)
    entries = list(jail.walk(lambda: None))
    assert [(entry.path, entry.kind) for entry in entries] == [
        ("clip.M4V", "video"),
        ("direct.ARW", "raw"),
        ("safe/カメラ.HIF", "heif"),
    ]
    assert hash_file(jail, entries[-1], lambda: None) == hashlib.sha256(b"media").hexdigest()
    with pytest.raises(OutsideLibrary):
        jail.locate("../outside/no.jpg")


def test_exiv2_mutes_sony2_before_open_and_retains_standard_tags(monkeypatch):
    calls = []

    class SyntheticSonyDNG:
        def __init__(self, path):
            assert calls == [4]
            calls.append(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read_exif(self, **kwargs):
            return {
                "Exif.Photo.DateTimeOriginal": "2025:03:04 05:06:07",
                "Exif.Photo.DateTimeDigitized": "2025:03:05 05:06:07",
                "Exif.Image.Make": "SONY",
                "Exif.Image.Model": "ILCE-7CM2",
                "Exif.Sony2.Unknown": "invalid Sony2 directory",
                "Exif.GPSInfo.GPSLatitude": "35/1 30/1 0/1",
                "Exif.GPSInfo.GPSLatitudeRef": "N",
                "Exif.GPSInfo.GPSLongitude": "139/1 15/1 0/1",
                "Exif.GPSInfo.GPSLongitudeRef": "W",
            }

    monkeypatch.setattr(exif.pyexiv2, "set_log_level", calls.append)
    monkeypatch.setattr(exif.pyexiv2, "Image", SyntheticSonyDNG)
    result = exif.read_metadata("synthetic.dng")
    assert calls == [4, "synthetic.dng"]
    assert result == exif.Metadata("2025-03-04T05:06:07", "SONY", "ILCE-7CM2", 35.5, -139.25)


def test_exif_falls_back_to_standard_tags_and_reports_actual_failure(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.dng"
    tags = Image.Exif()
    tags[271] = "SONY"
    tags[306] = "2020:01:02 03:04:05"
    tags[34665] = {36868: "2025:04:05 06:07:08"}
    tags[34853] = {
        1: "N",
        2: (IFDRational(35), IFDRational(30), IFDRational(0)),
        3: "E",
        4: (IFDRational(139), IFDRational(0), IFDRational(0)),
    }
    Image.new("RGB", (2, 2)).save(path, format="TIFF", exif=tags.tobytes())

    def failed_open(path):
        raise RuntimeError("unsupported Sony2 directory")

    monkeypatch.setattr(exif.pyexiv2, "Image", failed_open)
    info = exif.read_metadata(str(path))
    assert info == exif.Metadata("2025-04-05T06:07:08", "SONY", None, 35.5, 139)
    path.write_bytes(b"invalid")
    assert '"code": "exif_read_failed"' in exif.read_metadata(str(path)).metadata_error


def test_decode_jpeg_orientation_and_heif(tmp_path):
    jpg = tmp_path / "rotate.jpg"
    orientation = Image.Exif()
    orientation[274] = 6
    Image.new("RGB", (2900, 360), "blue").save(jpg, exif=orientation)
    register_heif()
    hif = tmp_path / "カメラ.HIF"
    Image.new("RGB", (16, 20), "green").save(hif, format="HEIF")

    state = tmp_path / "state"
    jail = MediaJail(Library(root=tmp_path), state)
    for index, (path, kind, dimensions) in enumerate(
        [(jpg, "image", (360, 2900)), (hif, "heif", (16, 20))], 1
    ):
        with jail.open_media(path.name) as (fd, _):
            result = decode(fd, kind, state, index, "hash")
        assert (result.width, result.height) == dimensions
        with Image.open(cache_path(state, "thumbs", index, "hash")) as thumb:
            assert max(thumb.size) <= 320
        with Image.open(cache_path(state, "previews", index, "hash")) as preview:
            assert max(preview.size) <= 2560
    with Image.open(cache_path(state, "details", 1, "hash")) as detail:
        assert detail.size == (360, 1600)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg tools unavailable"
)
def test_video_poster_and_duration(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=10",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
    )
    jail = MediaJail(Library(root=tmp_path), tmp_path / "state")
    with jail.open_media(path.name) as (fd, _):
        dimensions = decode(fd, "video", tmp_path / "state", 1, "hash")
    assert (dimensions.width, dimensions.height) == (64, 48)
    assert dimensions.duration == pytest.approx(1.0)
    assert Path(cache_path(tmp_path / "state", "thumbs", 1, "hash")).is_file()
    video = _index_media(tmp_path)
    assert video["preview_status"] == "done" and video["score_status"] == "skipped"
    assert video["burst_id"] is None and video["sharpness"] is None


def test_dng_demosaic_without_an_embedded_jpeg(tmp_path):
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    path = tmp_path / "fixture.DNG"
    tags = ImageFileDirectory_v2()
    # A small, uncompressed Bayer sensor with an identity color matrix.
    for tag, field_type, value in (
        (262, 3, 32803),
        (271, 2, "Fixture"),
        (272, 2, "Synthetic camera"),
        (306, 2, "2025:03:04 05:06:07"),
        (33421, 3, (2, 2)),
        (33422, 1, b"\x00\x01\x01\x02"),
        (50706, 1, b"\x01\x04\x00\x00"),
        (50707, 1, b"\x01\x01\x00\x00"),
        (50708, 2, "Photocull synthetic Bayer"),
        (50710, 1, b"\x00\x01\x02"),
        (50711, 3, 1),
        (50714, 3, 64),
        (50717, 4, 4095),
        (50721, 10, (1, 0, 0, 0, 1, 0, 0, 0, 1)),
        (50728, 5, (1, 1, 1)),
        (50778, 3, 21),
    ):
        tags.tagtype[tag] = field_type
        tags[tag] = value
    Image.new("I;16", (64, 64), 512).save(path, format="TIFF", tiffinfo=tags)
    state = tmp_path / "state"
    jail = MediaJail(Library(root=tmp_path), state)
    with jail.open_media(path.name) as (fd, _):
        info = exif.read_metadata(f"/proc/self/fd/{fd}")
        dimensions = decode(fd, "raw", state, 7, "fixture")
    assert info == exif.Metadata("2025-03-04T05:06:07", "Fixture", "Synthetic camera")
    assert (dimensions.width, dimensions.height) == (32, 32)
    assert cache_path(state, "thumbs", 7, "fixture").is_file()
    raw = _index_media(tmp_path)
    assert raw["kind"] == "raw" and raw["score_status"] == "done"
    assert 0 <= raw["sharpness"] <= 1 and 0 <= raw["exposure"] <= 1
