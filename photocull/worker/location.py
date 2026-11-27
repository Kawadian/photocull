from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from photocull.archive import ArchiveError
from photocull.config import Settings
from photocull.media.walk import open_directory
from photocull.worker.archive_media import (
    FF_SOURCE,
    Tools,
    compare_video,
    probe_video,
    raw_parameters,
    verify_location_free,
    video_stream,
)

Coordinates = tuple[float, float]


def read_coordinates(settings: Settings, fd: int, pulse: Callable) -> Coordinates | None:
    source = f"/proc/self/fd/{fd}"
    tools = Tools(settings.archive, pulse, pass_fds=(fd,))
    data = json.loads(
        tools.run("exiftool", "-j", "-n", "-GPSLatitude", "-GPSLongitude", source, timeout=45)
    )[0]
    if data.get("Error"):
        raise ValueError("Could not read source location")
    lat, lon = data.get("GPSLatitude"), data.get("GPSLongitude")
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Source location is outside valid coordinate bounds")
    return float(lat), float(lon)


def jpeg_exif(coordinates: Coordinates) -> bytes:
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational

    def dms(coordinate: float):
        ticks = round(abs(coordinate) * 3600 * 10000)
        degrees, ticks = divmod(ticks, 3600 * 10000)
        minutes, ticks = divmod(ticks, 60 * 10000)
        return (
            IFDRational(degrees),
            IFDRational(minutes),
            IFDRational(ticks, 10000),
        )

    lat, lon = coordinates
    exif = Image.Exif()
    exif[34853] = {
        0: b"\x02\x03\x00\x00",
        1: "N" if lat >= 0 else "S",
        2: dms(lat),
        3: "E" if lon >= 0 else "W",
        4: dms(lon),
    }
    return exif.tobytes()


STILLS = {".jpg", ".jpeg", ".heic", ".heif", ".hif", ".dng"}
VIDEO_CONTAINERS = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska"}


def strip_copy(tools: Tools, source: Path, output: Path) -> None:
    """Remove location metadata from a private copy and verify the media essence."""
    suffix = source.suffix.lower()
    if suffix != output.suffix.lower():
        raise ArchiveError("The export format changed; preview the operation again")
    if suffix in VIDEO_CONTAINERS:
        strip_video(tools, source, output, VIDEO_CONTAINERS[suffix])
    elif suffix in STILLS:
        with source.open("rb") as before, output.open("xb") as after:
            while chunk := before.read(1024 * 1024):
                tools.pulse()
                after.write(chunk)
        tools.run(
            "exiftool",
            "-overwrite_original",
            "-GPS:all=",
            "-XMP:all=",
            "-IPTC:all=",
            "-MakerNotes:all=",
            "-GPSCoordinates=",
            "-LocationInformation=",
            "-LocationName=",
            output,
        )
        if suffix == ".dng":
            if raw_parameters(tools, source) != raw_parameters(tools, output):
                raise ArchiveError("DNG pixel data or calibration changed")
        else:
            script = Path(__file__).with_name("archive_image.py")
            if tools.run(sys.executable, script, source, "fingerprint") != tools.run(
                sys.executable, script, output, "fingerprint"
            ):
                raise ArchiveError("Image pixels, orientation or color profile changed")
        # Metadata editors can preserve embedded JPEGs verbatim. Check those independently.
        for tag in ("ThumbnailImage", "PreviewImage", "JpgFromRaw", "OtherImage"):
            if image := tools.run("exiftool", "-b", f"-{tag}", output):
                preview = output.parent / "preview.jpg"
                with preview.open("xb") as file:
                    file.write(image)
                verify_location_free(tools, preview)
                preview.unlink()
    else:
        raise ArchiveError(
            f"Metadata-only location removal is unavailable for {suffix or 'this format'}; "
            "retain location or choose an explicit Archive conversion"
        )
    verify_location_free(tools, output)


def strip_video(tools: Tools, source: Path, output: Path, container: str) -> None:
    before = probe_video(tools, source)
    if any(not video_stream(s) and s["codec_type"] != "audio" for s in before["streams"]):
        raise ArchiveError("Metadata-only removal cannot verify extra or timed metadata streams")
    ffmpeg = ("ffmpeg", "-nostdin", "-v", "error", *FF_SOURCE)
    tools.run(
        *ffmpeg,
        "-i",
        source,
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "-1",
        "-map_metadata:s",
        "-1",
        "-map_chapters",
        "-1",
        "-f",
        container,
        output,
    )
    after = probe_video(tools, output)
    compare_video({**before, "chapters": []}, after, None, keep_metadata=False)
    hash_args = ("-map", "0", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-")
    if tools.run(*ffmpeg, "-i", source, *hash_args) != tools.run(*ffmpeg, "-i", output, *hash_args):
        raise ArchiveError("Encoded video or audio samples changed")


@contextmanager
def clean_copy(
    settings: Settings, fd: int, suffix: str, directory: Path, pulse: Callable
) -> Iterator[Path]:
    with (
        open_directory(directory) as parent,
        TemporaryDirectory(dir=f"/proc/self/fd/{parent}", prefix=".location-") as private,
    ):
        source = Path(private) / f"input{suffix}"
        with os.fdopen(os.dup(fd), "rb") as original, source.open("xb") as snapshot:
            while chunk := original.read(1024 * 1024):
                pulse()
                snapshot.write(chunk)
        output = Path(private) / f"output{suffix}"
        strip_copy(Tools(settings.archive, pulse, pass_fds=(parent,)), source, output)
        yield output
