from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageOps

from photocull.media.cache import cache_path
from photocull.media.images import register_heif

PREVIEW_EDGE = 2560
THUMB_EDGE = 320
DETAIL_EDGE = 1600
MAX_PIXELS = 100_000_000
VIDEO_FORMATS = "mov,matroska,webm,m4v"


@dataclass(frozen=True)
class Dimensions:
    width: int
    height: int
    duration: float | None


def _rgb(image: Image.Image) -> Image.Image:
    if image.width * image.height > MAX_PIXELS:
        raise ValueError(f"Image exceeds the {MAX_PIXELS:,} pixel decode limit")
    image = ImageOps.exif_transpose(image)
    if "A" in image.getbands() or "transparency" in image.info:
        alpha = image.convert("RGBA")
        backdrop = Image.new("RGB", alpha.size, "#151719")
        backdrop.paste(alpha, mask=alpha.getchannel("A"))
        return backdrop
    return image.convert("RGB")


def _raw(path: str) -> Image.Image:
    import rawpy

    with rawpy.imread(path) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(BytesIO(thumb.data)) as embedded:
                    has_orientation = embedded.getexif().get(274) in range(2, 9)
                    image = _rgb(embedded)
                if not has_orientation:
                    rotation = {
                        3: Image.Transpose.ROTATE_180,
                        5: Image.Transpose.ROTATE_90,
                        6: Image.Transpose.ROTATE_270,
                    }.get(raw.sizes.flip)
                    if rotation is not None:
                        with image:
                            return image.transpose(rotation)
                return image
        except (
            rawpy.LibRawNoThumbnailError,
            rawpy.LibRawUnsupportedThumbnailError,
            OSError,
        ):
            pass
        if raw.sizes.raw_width * raw.sizes.raw_height > MAX_PIXELS:
            raise ValueError(f"RAW sensor exceeds the {MAX_PIXELS:,} pixel decode limit")
        return Image.fromarray(raw.postprocess(half_size=True, output_bps=8, use_camera_wb=True))


def _video(fd: int, path: str) -> tuple[Image.Image, float | None]:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            VIDEO_FORMATS,
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ],
        pass_fds=(fd,),
        capture_output=True,
        timeout=30,
        check=True,
    )
    duration = json.loads(probe.stdout).get("format", {}).get("duration")
    seconds = None if duration in (None, "N/A") else float(duration)
    if seconds is not None and (not math.isfinite(seconds) or seconds < 0):
        seconds = None
    poster = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            VIDEO_FORMATS,
            "-i",
            path,
            "-map",
            "0:V:0",
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({PREVIEW_EDGE},iw)':'min({PREVIEW_EDGE},ih)'"
            ":force_original_aspect_ratio=decrease",
            "-threads",
            "1",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
        ],
        pass_fds=(fd,),
        capture_output=True,
        timeout=60,
        check=True,
    )
    with Image.open(BytesIO(poster.stdout)) as image:
        return _rgb(image), seconds


def _save(image: Image.Image, target: Path, quality: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, prefix=f".{target.stem}-", delete=False) as temp:
        temporary = Path(temp.name)
        try:
            image.save(temp, format="JPEG", quality=quality)
            temp.close()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def decode(fd: int, kind: str, state: Path, asset_id: int, content_hash: str) -> Dimensions:
    path = f"/proc/self/fd/{fd}"
    duration = None
    if kind == "video":
        image, duration = _video(fd, path)
    elif kind == "raw":
        image = _raw(path)
    else:
        if kind == "heif":
            register_heif()
        with Image.open(path) as source:
            image = _rgb(source)

    with image:
        dimensions = Dimensions(image.width, image.height, duration)
        if max(image.size) > PREVIEW_EDGE:
            # Keep a center crop at one decoded pixel per CSS pixel for sharpness checks.
            width, height = min(image.width, DETAIL_EDGE), min(image.height, DETAIL_EDGE)
            x, y = (image.width - width) // 2, (image.height - height) // 2
            with image.crop((x, y, x + width, y + height)) as detail:
                _save(detail, cache_path(state, "details", asset_id, content_hash), 90)
        image.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
        _save(image, cache_path(state, "previews", asset_id, content_hash), 88)
        image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
        _save(image, cache_path(state, "thumbs", asset_id, content_hash), 82)
        return dimensions
