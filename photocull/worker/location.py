from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from photocull.archive import ArchiveError
from photocull.config import Settings
from photocull.media.export_location import (
    RAW,
    STILLS,
    VIDEO_CONTAINERS,
    LocationSkip,
    unsupported_reason,
)
from photocull.worker.archive_media import (
    FF_SOURCE,
    Tools,
    compare_video,
    probe_video,
    raw_parameters,
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


EXIFTOOL = ("exiftool", "-config", "")
LOCATION_NAMES = {
    "city",
    "country",
    "countrycode",
    "state",
    "province",
    "provincestate",
    "worldregion",
    "sublocation",
    "latitude",
    "longitude",
    "altitude",
    "coordinates",
    "iso6709",
    "loci",
    "xyz",
    "©xyz",
}
LOCATION_PREFIXES = ("gps", "geotag", "geolocation", "location", "sublocation", "countryprimary")
OPAQUE_RAW_FIELDS = ("OriginalRawFileData", "OriginalRawFileName", "DNGPrivateData")
RELOCATABLE = {
    "ExifOffset",
    "GPSInfo",
    "InteropOffset",
    "MakerNoteOffset",
    "SubIFD",
    "StripOffsets",
    "TileOffsets",
    "ThumbnailOffset",
    "PreviewImageStart",
    "JpgFromRawStart",
    "OtherImageStart",
    "ApplicationRecordVersion",
    "XMPToolkit",
}
PRESERVED_GROUPS = {"EXIF", "XMP", "IPTC", "MakerNotes", "ICC_Profile", "Photoshop", "JFIF"}
VIDEO_METADATA_GROUPS = {"ItemList", "UserData", "Keys"}
SAFE_TAG = re.compile(r"[-A-Za-z0-9_]+")


def is_location(name):
    key = re.sub(r"[ _-]", "", name.casefold()).removeprefix("com.apple.quicktime.")
    return key in LOCATION_NAMES or key.startswith(LOCATION_PREFIXES)


def fields(tools, path):
    data = json.loads(tools.run(*EXIFTOOL, "-j", "-n", "-s", "-a", "-G0:1:4", "-ee3", path))[0]
    if "ExifTool:Error" in data or "Error" in data or "File:FileType" not in data:
        raise ArchiveError("Could not read the exported metadata")
    return data


def tag_parts(key):
    parts = key.split(":")
    family, name = parts[0], parts[-1]
    group = parts[1] if len(parts) > 2 and not parts[1].startswith("Copy") else family
    return family, group, name


def verify_no_location(tags):
    for key in tags:
        family, group, name = tag_parts(key)
        if group == "GPS" or family == "GeoTiff" or is_location(name):
            raise ArchiveError(f"Location metadata remained: {family}:{name}")


def remove_location(tools, output, tags):
    delete = set()
    for key in tags:
        family, group, name = tag_parts(key)
        if family == "Composite":
            continue  # These are calculated from underlying tags.
        if group == "GPS":
            delete.add("GPS:all")
        elif is_location(name) and SAFE_TAG.fullmatch(group) and SAFE_TAG.fullmatch(name):
            if group == "XMP-iptcExt":
                # Remove complete structured locations, including their IDs.
                name = next(
                    (key for key in ("LocationCreated", "LocationShown") if name.startswith(key)),
                    name,
                )
            delete.add(f"{group}:{name}")
    if delete:
        tools.run(*EXIFTOOL, "-overwrite_original", *(f"-{tag}=" for tag in sorted(delete)), output)


def verify_preserved(tools, source, output, before, after, *, video=False):
    def relevant(tags):
        values = Counter()
        for key, value in tags.items():
            family, group, name = tag_parts(key)
            if group == "GPS" or is_location(name) or name in RELOCATABLE:
                continue
            if family in PRESERVED_GROUPS or (family == "File" and name == "Comment"):
                values[(group, name, json.dumps(value, sort_keys=True))] += 1
            elif (
                video
                and family == "QuickTime"
                and group in VIDEO_METADATA_GROUPS
                and name not in {"Encoder", "SoftwareVersion"}
            ):
                # A remux can move an album/title/etc. between QuickTime atom families.
                values[(family, name, json.dumps(value, sort_keys=True))] += 1
        return values

    if missing := relevant(before) - relevant(after):
        names = ", ".join(sorted({key[1] for key in missing}))
        raise ArchiveError(f"Non-location metadata changed: {names}")
    if tools.run(*EXIFTOOL, "-b", "-ICC_Profile", source) != tools.run(
        *EXIFTOOL, "-b", "-ICC_Profile", output
    ):
        raise ArchiveError("Embedded ICC profile changed")


def verify_still(tools, source, output, suffix):
    if suffix in RAW:
        if raw_parameters(tools, source) != raw_parameters(tools, output):
            raise ArchiveError("RAW pixel data or calibration changed")
    elif suffix != ".xmp":
        script = Path(__file__).with_name("archive_image.py")
        if tools.run(sys.executable, script, source, "fingerprint") != tools.run(
            sys.executable, script, output, "fingerprint"
        ):
            raise ArchiveError("Image pixels, orientation or color profile changed")

    # Metadata editors can preserve an embedded JPEG verbatim. Check every known preview.
    if suffix != ".xmp":
        for tag in ("ThumbnailImage", "PreviewImage", "JpgFromRaw", "OtherImage"):
            if not (image := tools.run(*EXIFTOOL, "-b", f"-{tag}", output)):
                continue
            preview = output.with_name(output.name + "-preview.jpg")
            try:
                with preview.open("xb") as file:
                    file.write(image)
                verify_no_location(fields(tools, preview))
            finally:
                preview.unlink(missing_ok=True)


def strip_copy(tools: Tools, source: Path, output: Path) -> None:
    """Remove location metadata from an export snapshot and verify the copy."""
    suffix = source.suffix.lower()
    if suffix != output.suffix.lower():
        raise LocationSkip("The export format changed; preview the operation again")
    if suffix not in STILLS | RAW | VIDEO_CONTAINERS.keys() | {".xmp"}:
        raise LocationSkip(unsupported_reason(suffix))
    try:
        before = fields(tools, source)
        if suffix == ".xmp" and before["File:FileType"] != "XMP":
            raise ArchiveError("Could not read this XMP sidecar")
        if suffix in RAW:
            raw_tags = json.loads(
                tools.run(*EXIFTOOL, "-j", *(f"-{tag}" for tag in OPAQUE_RAW_FIELDS), source)
            )[0]
            if raw_tags.keys() & set(OPAQUE_RAW_FIELDS):
                raise ArchiveError(
                    "Cannot verify a RAW file with an opaque private block or embedded original"
                )
        if suffix in VIDEO_CONTAINERS:
            expected = strip_video(tools, source, output, VIDEO_CONTAINERS[suffix])
            if any(tag_parts(key)[0] == "XMP" for key in before):
                tools.run(
                    *EXIFTOOL, "-overwrite_original", "-tagsFromFile", source, "-XMP:all", output
                )
            output_tags = fields(tools, output)
        else:
            with source.open("rb") as original, output.open("xb") as copy:
                while chunk := original.read(1024 * 1024):
                    tools.pulse()
                    copy.write(chunk)
            output_tags = before

        remove_location(tools, output, output_tags)
        after = fields(tools, output)
        if before["File:FileType"] != after["File:FileType"]:
            raise ArchiveError("Output container format changed")
        verify_no_location(after)
        verify_preserved(tools, source, output, before, after, video=suffix in VIDEO_CONTAINERS)
        if suffix in VIDEO_CONTAINERS:
            verify_video(tools, source, output, expected)
        else:
            verify_still(tools, source, output, suffix)
    except (ArchiveError, KeyError, json.JSONDecodeError) as error:
        raise LocationSkip(str(error)) from error


def strip_video(tools, source, output, container):
    before = probe_video(tools, source)
    if any(not video_stream(s) and s["codec_type"] != "audio" for s in before["streams"]):
        raise ArchiveError("Cannot verify additional or timed metadata streams")

    def clean(scope, item):
        args = []
        tags = {}
        for key, value in item.get("tags", {}).items():
            if is_location(key):
                args.extend((f"-metadata{scope}", f"{key}="))
            else:
                tags[key] = value
        return args, {**item, "tags": tags}

    args, expected_format = clean("", before["format"])
    expected = {**before, "format": expected_format}
    for label, scope in (("streams", ":s:"), ("chapters", ":c:")):
        expected[label] = []
        for i, item in enumerate(before.get(label, [])):
            deletion, filtered = clean(f"{scope}{i}", item)
            args.extend(deletion)
            expected[label].append(filtered)

    ffmpeg = ("ffmpeg", "-nostdin", "-v", "error", *FF_SOURCE)
    mov = ("-movflags", "+use_metadata_tags") if container in {"mov", "mp4"} else ()
    tools.run(
        *ffmpeg,
        "-i",
        source,
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        *args,
        *mov,
        "-f",
        container,
        output,
    )
    return expected


def verify_video(tools, source, output, expected):
    after = probe_video(tools, output)
    for item in [after["format"], *after["streams"], *after.get("chapters", [])]:
        if any(is_location(key) for key in item.get("tags", {})):
            raise ArchiveError("Location metadata remained in the video container")
    compare_video(expected, after, None)
    ffmpeg = ("ffmpeg", "-nostdin", "-v", "error", *FF_SOURCE)
    hash_args = ("-map", "0", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-")
    if tools.run(*ffmpeg, "-i", source, *hash_args) != tools.run(*ffmpeg, "-i", output, *hash_args):
        raise ArchiveError("Encoded video or audio samples changed")


@contextmanager
def clean_copy(
    settings: Settings, fd: int, suffix: str, parent: int, pulse: Callable
) -> Iterator[Path]:
    with TemporaryDirectory(dir=f"/proc/self/fd/{parent}", prefix=".location-") as private:
        source = Path(private) / f"input{suffix}"
        with os.fdopen(os.dup(fd), "rb") as original, source.open("xb") as snapshot:
            while chunk := original.read(1024 * 1024):
                pulse()
                snapshot.write(chunk)
        output = Path(private) / f"output{suffix}"
        strip_copy(Tools(settings.archive, pulse, pass_fds=(parent,)), source, output)
        yield output
