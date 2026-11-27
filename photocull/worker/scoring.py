from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class Scores:
    sharpness: float
    exposure: float
    clipped_highlights: float
    clipped_shadows: float
    dhash: str
    detail: float
    bright_p99: float
    dark_p01: float


def score_preview(path: Path) -> Scores:
    with Image.open(path) as preview, preview.convert("L") as gray:
        fingerprint = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS))
        bits = np.packbits((fingerprint[:, 1:] > fingerprint[:, :-1]).ravel())
        gray.thumbnail((512, 512), Image.Resampling.LANCZOS)
        luminance = np.asarray(gray)
        pixels = luminance.astype(np.float32) / 255.0

    bright_p99, dark_p01 = np.percentile(pixels, (99, 1))
    sharpness = detail = 0.0
    if min(pixels.shape) >= 3:
        center = pixels[1:-1, 1:-1]
        laplacian = (
            pixels[:-2, 1:-1] + pixels[2:, 1:-1] + pixels[1:-1, :-2] + pixels[1:-1, 2:] - 4 * center
        )
        variance = float(laplacian.var())
        sharpness = variance / (variance + 0.01)
        valid = (luminance > 5) & (luminance < 250)
        # Ignore stencils touching a clipped pixel so clipping boundaries do not count as detail.
        mask = valid[1:-1, 1:-1] & valid[:-2, 1:-1] & valid[2:, 1:-1]
        mask &= valid[1:-1, :-2] & valid[1:-1, 2:]
        if mask.any():
            variance = float(laplacian[mask].var())
            detail = variance / (variance + 0.01)

    return Scores(
        sharpness=sharpness,
        exposure=float(luminance.mean() / 255.0),
        clipped_highlights=float(np.mean(luminance >= 250)),
        clipped_shadows=float(np.mean(luminance <= 5)),
        dhash=bits.tobytes().hex(),
        detail=detail,
        bright_p99=float(bright_p99),
        dark_p01=float(dark_p01),
    )
