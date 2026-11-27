from __future__ import annotations

import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from photocull.archive import VIDEO, ArchiveError
from photocull.media.decode import VIDEO_FORMATS

EXIF_FIELDS = (
    "Make",
    "Model",
    "LensMake",
    "LensModel",
    "SerialNumber",
    "InternalSerialNumber",
    "BodySerialNumber",
    "LensSerialNumber",
    "DateTimeOriginal",
    "CreateDate",
    "ModifyDate",
    "SubSecTime",
    "SubSecTimeOriginal",
    "SubSecTimeDigitized",
    "OffsetTime",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "ExposureTime",
    "FNumber",
    "ISO",
    "FocalLength",
    "FocalLengthIn35mmFormat",
    "ExposureCompensation",
    "ExposureProgram",
    "ExposureMode",
    "ApertureValue",
    "MaxApertureValue",
    "ShutterSpeedValue",
    "BrightnessValue",
    "SubjectDistance",
    "DigitalZoomRatio",
    "SensingMethod",
    "Saturation",
    "Contrast",
    "Sharpness",
    "SceneCaptureType",
    "LightSource",
    "MeteringMode",
    "Flash",
    "WhiteBalance",
    "ColorSpace",
    "Artist",
    "Copyright",
    "ImageDescription",
    "UserComment",
    "Software",
    "Orientation",
    "Rating",
    "Subject",
    "Description",
    "Caption-Abstract",
    "Keywords",
    "Rights",
    "Title",
    "Creator",
)
MOV_FIELDS = (
    "CreationDate",
    "CreateDate",
    "MediaCreateDate",
    "TrackCreateDate",
    "GPSCoordinates",
    "LocationInformation",
    "LocationName",
    "Rotation",
)
ENCODERS = {"hevc": "libx265", "av1": "libaom-av1"}
FF_SOURCE = ["-protocol_whitelist", "file", "-format_whitelist", VIDEO_FORMATS]
XMP_STRUCTURAL = {
    "Orientation",
    "ImageWidth",
    "ImageHeight",
    "MetadataDate",
    "InstanceID",
    "NativeDigest",
    "XMPToolkit",
}
FF_GENERATED = {
    "major_brand",
    "minor_version",
    "compatible_brands",
    "encoder",
    "duration",
    "bps",
    "number_of_frames",
    "number_of_bytes",
    "vendor_id",
    "handler_name",
}
PROC_LIMIT = 16 * 1024 * 1024


class Tools:
    def __init__(self, config, pulse, *, pass_fds=()):
        self.config, self.pulse, self.pass_fds = config, pulse, pass_fds

    def run(self, *args, pass_fds=(), timeout=None) -> bytes:
        timeout = timeout or self.config.timeout_seconds
        deadline = time.monotonic() + timeout
        argv = [
            sys.executable,
            str(Path(__file__).with_name("archive_exec.py")),
            str(os.getpid()),
            str(self.config.memory_mb),
            str(self.config.threads),
            *map(str, args),
        ]
        env = {
            **os.environ,
            "OMP_NUM_THREADS": str(self.config.threads),
            "OPENBLAS_NUM_THREADS": str(self.config.threads),
            "RAYON_NUM_THREADS": str(self.config.threads),
            "TZ": "UTC",
        }
        # Files avoid pipe backpressure while polling cancellation. Read only bounded diagnostics.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            with subprocess.Popen(
                argv,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                env=env,
                pass_fds=tuple(dict.fromkeys((*self.pass_fds, *pass_fds))),
                start_new_session=True,
            ) as proc:
                try:
                    while proc.poll() is None:
                        self.pulse()
                        if time.monotonic() > deadline:
                            raise ArchiveError(f"{args[0]} timed out")
                        try:
                            proc.wait(timeout=0.15)
                        except subprocess.TimeoutExpired:
                            if max(out.tell(), err.tell()) > PROC_LIMIT:
                                raise ArchiveError(
                                    f"{args[0]} produced too much diagnostic output"
                                ) from None
                finally:
                    if proc.poll() is None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                if proc.returncode:
                    err.seek(max(0, err.tell() - 3000))
                    detail = err.read().decode(errors="replace").strip()
                    raise ArchiveError(f"{args[0]} exited {proc.returncode}: {detail}")
            if out.tell() > PROC_LIMIT:
                raise ArchiveError(f"{args[0]} produced too much output")
            out.seek(0)
            return out.read()


def metadata(tools, path, *, video=False):
    fields = MOV_FIELDS if video else EXIF_FIELDS
    data = json.loads(
        tools.run(
            "exiftool",
            "-j",
            "-n",
            "-a",
            "-G1:4",
            *(f"-{tag}" for tag in fields),
            *([] if video else ["-GPS:all", "-XMP:all", "-IPTC:all"]),
            path,
        )
    )[0]
    if data.get("ExifTool:Error") or data.get("Error"):
        raise ArchiveError("ExifTool could not read metadata")
    values = {}
    for key, value in data.items():
        if ":" not in key:
            continue
        group, *_, tag = key.split(":")
        if group in {"File", "Composite", "System", "ExifTool"}:
            continue
        if group.startswith("XMP") and tag in XMP_STRUCTURAL:
            continue
        values.setdefault(tag, set()).add(json.dumps(value, sort_keys=True))
    return values


def compare_metadata(tools, source, target, *, video=False, ignore=()):
    expected = metadata(tools, source, video=video)
    actual = metadata(tools, target, video=video)
    missing = [
        tag
        for tag, values in expected.items()
        if tag not in ignore and not values.issubset(actual.get(tag, set()))
    ]
    if missing:
        raise ArchiveError(f"Metadata differed: {', '.join(sorted(missing))}")
    a = tools.run("exiftool", "-b", "-ICC_Profile", source)
    if a and a != tools.run("exiftool", "-b", "-ICC_Profile", target):
        raise ArchiveError("Embedded ICC color profile differed")
    return bool(a)


def still_color(tools, source, target):
    fields = ("ColorPrimaries", "TransferCharacteristics")

    def profile(path):
        result = json.loads(tools.run("exiftool", "-j", "-n", *(f"-{f}" for f in fields), path))[0]
        return tuple(result.get(f, 2) for f in fields)

    before = profile(source)
    # Unspecified and sRGB signaling can use the destination's default RGB interpretation.
    if before not in {(2, 2), (1, 13)} and before != profile(target):
        raise ArchiveError("Source RGB color space needs a portable ICC profile")


def raw_parameters(tools, path):
    meta = json.loads(tools.run("dnglab", "analyze", "--meta", "--json", path))
    params = meta["data"]["metadata"]["rawParams"]
    digest = tools.run("dnglab", "analyze", "--raw-checksum", path).strip()
    if not re.fullmatch(rb"[a-f0-9]{32}", digest):
        raise ArchiveError("DNGLab did not decode raw pixels")
    return params, digest


def verify_location_free(tools, path):
    tags = json.loads(tools.run("exiftool", "-j", "-n", "-a", "-G1", "-ee", path))[0]
    for field in tags:
        group, _, name = field.partition(":")
        if group == "GPS" or name.casefold().startswith(("gps", "location", "geotag")):
            raise ArchiveError(f"Location metadata remained in the export: {name}")
        if group in {"MakerNotes", "IPTC"} or group.startswith("XMP"):
            raise ArchiveError(f"Source metadata remained in the export: {group}")
    if tags.get("ExifTool:Error") or tags.get("Error"):
        raise ArchiveError("Could not verify the exported metadata")


def dng(tools, source, target, request):
    preview = "true" if request.dng_preview == "embed" and not request.strip_location else "false"
    before, digest = raw_parameters(tools, source)
    tools.run(
        "dnglab",
        "convert",
        "--compression",
        "lossless",
        "--embed-raw",
        "false",
        "--dng-preview",
        preview,
        "--dng-thumbnail",
        preview,
        "--crop",
        "best",
        "--jobs",
        "1",
        source,
        target,
    )
    if request.strip_location:
        # ExifTool excludes the structural DNG tags when erasing descriptive metadata.
        tools.run(
            "exiftool",
            "-overwrite_original",
            "-all=",
            "-CommonIFD0=",
            "--Orientation",
            "--Make",
            "--Model",
            "--ICC_Profile",
            target,
        )
        verify_location_free(tools, target)
    else:
        # DNGLab copies several EXIF groups itself. Copy portable metadata it may omit.
        tools.run(
            "exiftool",
            "-overwrite_original",
            "-tagsFromFile",
            source,
            "-XMP:all",
            "-IPTC:all",
            "-GPS:all",
            "-ICC_Profile",
            *(f"-{field}" for field in EXIF_FIELDS),
            target,
        )
    after, result = raw_parameters(tools, target)
    shape = ("rawWidth", "rawHeight")
    if (
        not all(key in before and key in after for key in shape)
        or any(before[key] != after[key] for key in shape)
        or after["bitDepth"] < before["bitDepth"]
        or digest != result
    ):
        raise ArchiveError("Decoded RAW samples or dimensions differ")
    for key in ("blacklevels", "whitelevels", "activeArea", "cropArea", "wbCoeffs"):
        old, new = before.get(key), after.get(key)
        if key in {"cropArea", "activeArea"}:
            full = {"p": {"x": 0, "y": 0}, "d": {"w": after["rawWidth"], "h": after["rawHeight"]}}
            old, new = old or full, new or full
        if old != new:
            raise ArchiveError(f"DNG raw calibration differs: {key}")
    types = json.loads(
        tools.run(
            "exiftool",
            "-j",
            "-n",
            "-a",
            "-G1",
            "-Compression",
            "-DNGVersion",
            "-PhotometricInterpretation",
            "-SubfileType",
            "-PreviewImage",
            target,
        )
    )[0]
    raw_groups = [
        key.split(":")[0]
        for key, value in types.items()
        if key.endswith(":PhotometricInterpretation")
        and (
            value in {32803, 34892}
            or value == 2
            and types.get(key.replace("PhotometricInterpretation", "SubfileType")) == 0
        )
    ]
    if (
        not raw_groups
        or not all(types.get(f"{group}:Compression") == 7 for group in raw_groups)
        or not types.get("IFD0:DNGVersion")
    ):
        raise ArchiveError("Expected a lossless JPEG-compressed DNG")
    if preview == "true" and not any(k.endswith(":PreviewImage") for k in types):
        raise ArchiveError("The requested embedded DNG preview is missing")
    if not request.strip_location:
        compare_metadata(tools, source, target)


def still(tools, source, target, request):
    worker = [sys.executable, str(Path(__file__).with_name("archive_image.py"))]
    before = json.loads(tools.run(*worker, source))
    quality = request.quality or {"jpeg": 85, "heic": 72, "avif": 70}[request.operation]
    tools.run(
        *worker,
        source,
        target,
        request.operation,
        quality,
        tools.config.threads,
        request.preset,
        "strip" if request.strip_location else "include",
    )
    if request.strip_location:
        verify_location_free(tools, target)
    else:
        # Structural raster fields stay with the encoder. Orientation is verified separately.
        tools.run(
            "exiftool",
            "-overwrite_original",
            "-tagsFromFile",
            source,
            "-GPS:all",
            "-XMP:all",
            "-IPTC:all",
            "-ICC_Profile",
            *(f"-{tag}" for tag in EXIF_FIELDS if tag != "Orientation"),
            target,
        )
    after = json.loads(tools.run(*worker, target))
    if before != after:
        raise ArchiveError(
            f"Decoded image dimensions, alpha or orientation changed: {before} → {after}"
        )
    if request.strip_location:
        profile = tools.run("exiftool", "-b", "-ICC_Profile", source)
        if profile and profile != tools.run("exiftool", "-b", "-ICC_Profile", target):
            raise ArchiveError("Embedded ICC color profile differed")
        had_icc = bool(profile)
    else:
        had_icc = compare_metadata(tools, source, target, ignore=("Orientation",))
    if not had_icc:
        still_color(tools, source, target)


def probe_video(tools, path, *, count=False, pass_fds=(), timeout=None):
    return json.loads(
        tools.run(
            "ffprobe",
            "-threads",
            tools.config.threads,
            "-v",
            "error",
            *FF_SOURCE,
            *(["-count_frames"] if count else []),
            "-show_streams",
            "-show_format",
            "-show_chapters",
            "-of",
            "json",
            path,
            pass_fds=pass_fds,
            timeout=timeout,
        )
    )


def video_stream(stream):
    return stream.get("codec_type") == "video" and not stream.get("disposition", {}).get(
        "attached_pic"
    )


def check_tags(before, after):
    expected = {k.casefold(): v for k, v in before.get("tags", {}).items()}
    actual = {k.casefold(): v for k, v in after.get("tags", {}).items()}
    for key, value in expected.items():
        if (
            key not in FF_GENERATED
            and not key.startswith(("_statistics", "duration-"))
            and actual.get(key) != value
        ):
            raise ArchiveError(f"Video metadata differed: {key}")


def duration(data):
    value = float(data["format"]["duration"])
    if not math.isfinite(value) or value <= 0:
        raise ArchiveError("Video duration is invalid")
    return value


def compare_video(before, after, codec, *, keep_metadata=True):
    if len(before["streams"]) != len(after["streams"]):
        raise ArchiveError("Video stream count changed")
    if abs(duration(before) - duration(after)) > 0.1:
        raise ArchiveError("Video duration differs by over 100 ms")
    if keep_metadata:
        check_tags(before["format"], after["format"])
    chapters_a, chapters_b = before.get("chapters", []), after.get("chapters", [])
    if len(chapters_a) != len(chapters_b):
        raise ArchiveError("Video chapter count changed")
    for a, b in zip(chapters_a, chapters_b, strict=True):
        if any(abs(float(a[k]) - float(b[k])) > 0.01 for k in ("start_time", "end_time")):
            raise ArchiveError("Chapter timing changed")
        check_tags(a, b)
    for a, b in zip(before["streams"], after["streams"], strict=True):
        if a.get("codec_type") != b.get("codec_type") or a.get("disposition") != b.get(
            "disposition"
        ):
            raise ArchiveError("Video stream type or disposition changed")
        if keep_metadata:
            check_tags(a, b)
        if not video_stream(a):
            for field in ("codec_name", "sample_rate", "channels", "width", "height"):
                if a.get(field) != b.get(field):
                    raise ArchiveError(f"Copied stream field changed: {field}")
            continue
        fields = (
            "width",
            "height",
            "sample_aspect_ratio",
            "color_range",
            "color_space",
            "color_transfer",
            "color_primaries",
            "pix_fmt",
            "field_order",
            "chroma_location",
        )
        for key in fields:
            fallback = (
                "tv"
                if key == "color_range" and str(a.get("pix_fmt", "")).startswith("yuv")
                else "unknown"
            )
            if a.get(key, fallback) != b.get(key, fallback):
                raise ArchiveError(f"Video stream field changed: {key}")
        if b.get("codec_name") != (codec or a.get("codec_name")):
            raise ArchiveError("Video codec differs from the requested output")
        if a.get("side_data_list", []) != b.get("side_data_list", []):
            raise ArchiveError("Video rotation or other stream side data changed")
        if codec is None:
            continue  # Stream-copy callers verify encoded packet hashes separately.
        count_a, count_b = int(a.get("nb_read_frames", "0")), int(b.get("nb_read_frames", "0"))
        if min(count_a, count_b) <= 0 or abs(count_a - count_b) > 1:
            raise ArchiveError("Video frame count unavailable or differs by over one frame")


def video(tools, source, target, request):
    threads = str(tools.config.threads)
    before = probe_video(tools, source, count=True)
    streams = [stream for stream in before["streams"] if video_stream(stream)]
    if not streams:
        raise ArchiveError("No video stream to encode")
    f = request.filters
    if f.codecs and any(stream.get("codec_name") not in f.codecs for stream in streams):
        raise ArchiveError("Source codec no longer matches the filter")
    if f.min_kbps and int(before["format"].get("bit_rate", 0)) < f.min_kbps * 1000:
        raise ArchiveError("Source bitrate no longer matches the filter")
    if any(s.get("width", 0) < f.min_width or s.get("height", 0) < f.min_height for s in streams):
        raise ArchiveError("Source stream dimensions no longer match the filter")
    quality = (
        request.quality
        if request.quality is not None
        else {"hevc": 25, "av1": 31}[request.operation]
    )
    if request.operation == "hevc":
        preset = {"fast": "fast", "balanced": "medium", "slow": "slow"}[request.preset]
        options = [
            "-preset",
            preset,
            "-x265-params",
            f"pools=none:frame-threads={threads}:log-level=error",
        ]
    else:
        speed = {"fast": "8", "balanced": "6", "slow": "4"}[request.preset]
        options = ["-cpu-used", speed, "-row-mt", "1", "-b:V", "0"]
    mov = (
        ["-movflags", "+use_metadata_tags+faststart"]
        if Path(target).suffix in {".mp4", ".mov"}
        else []
    )
    stream_options = []
    for n, stream in enumerate(streams):
        # Explicit pixel formats prevent dropping HDR bit depth, chroma or alpha.
        stream_options += [f"-pix_fmt:V:{n}", f"+{stream['pix_fmt']}"]
        if request.operation == "av1" and (
            position := {"left": "vertical", "topleft": "colocated"}.get(
                stream.get("chroma_location")
            )
        ):
            stream_options += [f"-aom-params:V:{n}", f"chroma-sample-position={position}"]
        for key, option in (
            ("chroma_location", "chroma_sample_location"),
            ("color_range", "color_range"),
            ("color_space", "colorspace"),
            ("color_transfer", "color_trc"),
            ("color_primaries", "color_primaries"),
        ):
            if stream.get(key) not in (None, "unknown", "unspecified"):
                stream_options += [f"-{option}:V:{n}", stream[key]]
    if request.strip_location:
        mapping = [
            "-map",
            "0:V",
            "-map",
            "0:a?",
            "-map_metadata",
            "-1",
            "-map_metadata:s",
            "-1",
            "-map_chapters",
            "-1",
        ]
        before["streams"] = streams + [s for s in before["streams"] if s["codec_type"] == "audio"]
        before["chapters"] = []
    else:
        mapping = ["-map", "0", "-map_metadata", "0", "-map_chapters", "0"]
    tools.run(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-xerror",
        "-n",
        "-threads",
        threads,
        "-filter_threads",
        threads,
        "-noautorotate",
        *FF_SOURCE,
        "-i",
        source,
        *mapping,
        "-c",
        "copy",
        "-c:V",
        ENCODERS[request.operation],
        "-fps_mode:V",
        "passthrough",
        "-threads:V",
        threads,
        "-crf",
        quality,
        *options,
        *stream_options,
        *mov,
        target,
    )
    after = probe_video(tools, target, count=True)
    compare_video(before, after, request.operation, keep_metadata=not request.strip_location)
    tools.run(
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-threads",
        threads,
        *FF_SOURCE,
        "-i",
        target,
        "-map",
        "0:V",
        "-map",
        "0:a?",
        "-f",
        "null",
        "-",
    )
    if request.strip_location:
        verify_location_free(tools, target)
    else:
        compare_metadata(tools, source, target, video=True)


def convert(tools, source, target, request):
    if request.operation == "dng":
        dng(tools, source, target, request)
    elif request.operation in VIDEO:
        video(tools, source, target, request)
    else:
        still(tools, source, target, request)
