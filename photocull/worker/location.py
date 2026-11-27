from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from photocull.config import Settings
from photocull.worker.archive_media import Tools

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
        degrees = abs(coordinate)
        minutes = (degrees % 1) * 60
        return (
            IFDRational(int(degrees)),
            IFDRational(int(minutes)),
            IFDRational(round((minutes % 1) * 60 * 10000), 10000),
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


@contextmanager
def clean_copy(
    settings: Settings, fd: int, kind: str, target: Path, pulse: Callable
) -> Iterator[Path]:
    """Stage a portable copy from decoded pixels or audio/video essence."""
    if kind == "video":
        from photocull.worker.insights.video import proxy

        with proxy(f"/proc/self/fd/{fd}", fd, 1, target, pulse) as output:
            yield output
        return

    from photocull.media.decode import decode_source
    from photocull.media.derivatives import VARIANTS
    from photocull.worker.derivatives import staged_image

    pixels, _ = decode_source(fd, kind, color_managed=True)
    with (
        pixels,
        staged_image(target, pixels, VARIANTS["native.jpg"], settings.derivatives) as output,
    ):
        yield output
