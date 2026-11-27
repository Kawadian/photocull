from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from threading import Lock

import pyexiv2
from PIL import Image

from photocull.media.images import register_heif

EXIV2_LOCK = Lock()
DATES = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class Metadata:
    captured_at: str | None = None
    make: str | None = None
    model: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    metadata_error: str | None = None
    lens: str | None = None
    shutter_seconds: float | None = None
    aperture: float | None = None
    focal_length_mm: float | None = None
    iso: int | None = None


def _date(*values: tuple[str | None, str | None]) -> str | None:
    for value, subsecond in values:
        if value:
            for layout in DATES:
                try:
                    captured = datetime.strptime(value.strip("\0 "), layout)
                    if subsecond and subsecond.isascii() and subsecond.isdecimal():
                        captured = captured.replace(microsecond=int(subsecond[:6].ljust(6, "0")))
                    return captured.isoformat()
                except ValueError:
                    continue
    return None


def _coordinate(values, ref: str | None, positive: str, negative: str) -> float | None:
    if not values or ref not in {positive, negative}:
        return None
    try:
        if isinstance(values, str):
            values = values.split()
        degrees, minutes, seconds = (float(Fraction(str(value))) for value in values)
        limit = 90 if positive == "N" else 180
        if not (0 <= minutes < 60 and 0 <= seconds < 60 and 0 <= degrees <= limit):
            return None
        point = degrees + minutes / 60 + seconds / 3600
        return (-point if ref == negative else point) if point <= limit else None
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _pillow_tags(path: str) -> dict:
    register_heif()
    with Image.open(path) as image:
        root = image.getexif()
        photo = root.get_ifd(34665)
        gps = root.get_ifd(34853)
        return {
            "Exif.Photo.DateTimeOriginal": photo.get(36867) or root.get(36867),
            "Exif.Photo.DateTimeDigitized": photo.get(36868) or root.get(36868),
            "Exif.Image.DateTime": root.get(306),
            "Exif.Photo.SubSecTimeOriginal": photo.get(37521) or root.get(37521),
            "Exif.Photo.SubSecTimeDigitized": photo.get(37522) or root.get(37522),
            "Exif.Photo.SubSecTime": photo.get(37520) or root.get(37520),
            "Exif.Image.Make": root.get(271),
            "Exif.Image.Model": root.get(272),
            "Exif.Photo.LensModel": photo.get(42036),
            "Exif.Photo.ExposureTime": photo.get(33434) or root.get(33434),
            "Exif.Photo.FNumber": photo.get(33437) or root.get(33437),
            "Exif.Photo.FocalLength": photo.get(37386) or root.get(37386),
            "Exif.Photo.ISOSpeedRatings": photo.get(34855) or root.get(34855),
            "Exif.Photo.ISOSpeed": photo.get(34867),
            "Exif.GPSInfo.GPSLatitudeRef": gps.get(1),
            "Exif.GPSInfo.GPSLatitude": gps.get(2),
            "Exif.GPSInfo.GPSLongitudeRef": gps.get(3),
            "Exif.GPSInfo.GPSLongitude": gps.get(4),
        }


def _text(value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value.strip("\0 ") or None) if isinstance(value, str) else None


def _positive(value) -> float | None:
    try:
        number = float(Fraction(str(value)))
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _photo_number(tags: dict, name: str) -> float | None:
    return _positive(tags.get(f"Exif.Photo.{name}")) or _positive(tags.get(f"Exif.Image.{name}"))


def read_metadata(path: str) -> Metadata:
    try:
        with EXIV2_LOCK:
            pyexiv2.set_log_level(4)
            with pyexiv2.Image(path) as image:
                # Ignore the optional maker-note fields returned alongside standard tags.
                try:
                    tags = image.read_exif()
                except UnicodeError:
                    tags = image.read_exif(encoding="latin-1")
    except (OSError, RuntimeError, ValueError) as error:
        try:
            tags = _pillow_tags(path)
        except (OSError, RuntimeError, ValueError, SyntaxError):
            return Metadata(
                metadata_error=json.dumps({"code": "exif_read_failed", "message": str(error)})
            )

    iso = _photo_number(tags, "ISOSpeed") or _photo_number(tags, "ISOSpeedRatings")
    return Metadata(
        captured_at=_date(
            *(
                (_text(tags.get(date)), _text(tags.get(subsecond)))
                for date, subsecond in (
                    ("Exif.Photo.DateTimeOriginal", "Exif.Photo.SubSecTimeOriginal"),
                    ("Exif.Photo.DateTimeDigitized", "Exif.Photo.SubSecTimeDigitized"),
                    ("Exif.Image.DateTime", "Exif.Photo.SubSecTime"),
                )
            )
        ),
        make=_text(tags.get("Exif.Image.Make")),
        model=_text(tags.get("Exif.Image.Model")),
        latitude=_coordinate(
            tags.get("Exif.GPSInfo.GPSLatitude"),
            _text(tags.get("Exif.GPSInfo.GPSLatitudeRef")),
            "N",
            "S",
        ),
        longitude=_coordinate(
            tags.get("Exif.GPSInfo.GPSLongitude"),
            _text(tags.get("Exif.GPSInfo.GPSLongitudeRef")),
            "E",
            "W",
        ),
        lens=_text(tags.get("Exif.Photo.LensModel")),
        shutter_seconds=_photo_number(tags, "ExposureTime"),
        aperture=_photo_number(tags, "FNumber"),
        focal_length_mm=_photo_number(tags, "FocalLength"),
        iso=int(iso) if iso and iso.is_integer() and iso < 2**31 else None,
    )
