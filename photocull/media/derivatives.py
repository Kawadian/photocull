from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

from photocull.config import DerivativeSettings, Settings

Tier = Literal["hot", "large"]
ENCODING_REVISION = "1"
VERSION_PATTERN = r"^[0-9a-f]{64}-[0-9a-f]{16}$"
VIDEO_LIMITS = {
    "video-480.mp4": (854, 480, "850k", "64k"),
    "video-720.mp4": (1280, 720, "2M", "96k"),
}


@dataclass(frozen=True)
class Variant:
    name: str
    tier: Tier
    mime: str
    edge: int | None = None

    @property
    def extension(self) -> str:
        return self.name.rsplit(".", 1)[1]


VARIANTS = {
    f"{prefix}-{edge}.{ext}": Variant(f"{prefix}-{edge}.{ext}", tier, mime, edge)
    for prefix, tier, edges in (("grid", "hot", (256, 512)), ("fit", "large", (1280, 1920, 2560)))
    for edge in edges
    for ext, mime in (("avif", "image/avif"), ("webp", "image/webp"), ("jpg", "image/jpeg"))
}
VARIANTS["native.jpg"] = Variant("native.jpg", "large", "image/jpeg")
VARIANTS.update({name: Variant(name, "large", "video/mp4") for name in VIDEO_LIMITS})


def recipe(config: DerivativeSettings) -> str:
    settings = (ENCODING_REVISION, config.avif_quality, config.webp_quality, config.jpeg_quality)
    return sha256(repr(settings).encode()).hexdigest()[:16]


def relative_path(library: str, asset_id: int, variant: Variant, version: str) -> Path:
    if not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError("Invalid derivative version")
    return Path(library) / str(asset_id // 1000) / f"{asset_id}-{version}-{variant.name}"


def derivative_path(
    settings: Settings, library: str, asset_id: int, variant: Variant, version: str
) -> Path:
    return settings.derivative_root(variant.tier) / relative_path(
        library, asset_id, variant, version
    )
