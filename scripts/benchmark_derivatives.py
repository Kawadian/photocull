"""Encode representative synthetic stills to estimate cache capacity."""

from __future__ import annotations

import json
from io import BytesIO
from statistics import median, quantiles
from time import perf_counter

from PIL import Image, ImageOps

from photocull.config import DerivativeSettings
from photocull.media.derivatives import VARIANTS
from photocull.worker.derivatives import encode_image


def benchmark():
    settings = DerivativeSettings()
    layouts = ((3200, 2000), (2000, 3200), (3840, 2048))
    elapsed = {}
    totals = {"hot": [], "large": []}
    for width, height in layouts:
        size = width, height
        with Image.linear_gradient("L").resize(size) as gradient:
            for contrast in (8, 25, 60):
                with (
                    Image.effect_noise(size, contrast) as noise,
                    Image.blend(gradient, noise, 0.65) as detail,
                    ImageOps.colorize(detail, "#154121", "#fdf4e6") as image,
                ):
                    used = {"hot": 0, "large": 0}
                    for name, variant in VARIANTS.items():
                        if not variant.edge:
                            continue
                        output = BytesIO()
                        started = perf_counter()
                        encode_image(image, variant, settings, output)
                        elapsed.setdefault(name, []).append((perf_counter() - started) * 1_000)
                        used[variant.tier] += output.tell()
                    for tier, count in used.items():
                        totals[tier].append(count)

    results = {}
    for tier, sizes in totals.items():
        results[tier] = {
            "median_kib_per_photo": round(median(sizes) / 1024, 1),
            "range_kib_per_photo": [round(size / 1024, 1) for size in (min(sizes), max(sizes))],
            "median_gib_for_16000": round(median(sizes) * 16_000 / 1024**3, 2),
        }
    results["p95_encode_ms"] = {
        name: round(quantiles(times, n=100, method="inclusive")[94], 1)
        for name, times in elapsed.items()
    }
    return results


if __name__ == "__main__":
    print(json.dumps(benchmark()))
