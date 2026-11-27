"""Worker-only display color profiles and sRGB conversion."""

from __future__ import annotations

from importlib.resources import files
from io import BytesIO

from PIL import Image, ImageCms

_PROFILES = files(__package__).joinpath("profiles")
SRGB = _PROFILES.joinpath("sRGB-v4.icc").read_bytes()
DISPLAY_P3 = _PROFILES.joinpath("DisplayP3Compat-v4.icc").read_bytes()


def srgb_pixels(image: Image.Image) -> Image.Image:
    profile = image.info.get("icc_profile")
    if not profile:
        nclx = image.info.get("nclx_profile") or {}
        if nclx.get("color_primaries") == 12 and nclx.get("transfer_characteristics") == 13:
            profile = DISPLAY_P3
    if not profile or profile == SRGB:
        return image.convert("RGB")

    mode = {"LA": "L", "RGBA": "RGB", "P": "RGB", "PA": "RGB"}.get(image.mode, image.mode)
    try:
        with image.convert(mode) as source:
            return ImageCms.profileToProfile(
                source,
                BytesIO(profile),
                BytesIO(SRGB),
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                outputMode="RGB",
                flags=ImageCms.Flags.BLACKPOINTCOMPENSATION,
            )
    except (ImageCms.PyCMSError, OSError, TypeError, ValueError):
        # Broken or incompatible input profiles are common in older image collections.
        return image.convert("RGB")
