"""Generate photo-like landscapes for testing timeline layout with varied image sizes."""

from __future__ import annotations

import hashlib
import io
import random

from PIL import Image, ImageDraw, ImageOps

from photocull.db import connect, initialize, write
from photocull.media.cache import cache_path

PALETTES = (
    ("#ccd8d9", "#fae3b3", "#657a78", "#2b4e42", "#627845"),
    ("#869ca9", "#ecd8bc", "#82786f", "#514d44", "#bc8b50"),
    ("#77939e", "#d5e0d9", "#507481", "#195263", "#356e6c"),
    ("#504e66", "#e8b08f", "#705767", "#38314a", "#694659"),
    ("#a4b7b8", "#eee5d4", "#6b818a", "#3f555f", "#8c8f79"),
    ("#7c9c9e", "#c8ddad", "#688060", "#334d4b", "#8d9e65"),
)
RATIOS = (1.53, 1.62, 0.72, 1.42, 1.19, 2.15, 1.35, 0.84)


def landscape(number: int, width: int, height: int) -> Image.Image:
    rng = random.Random(number)
    sky, horizon, far, near, ground = PALETTES[number % len(PALETTES)]
    image = ImageOps.colorize(Image.linear_gradient("L"), sky, horizon).resize((width, height))
    draw = ImageDraw.Draw(image)
    light = int(width * rng.uniform(0.25, 0.76))
    radius = int(height * rng.uniform(0.029, 0.072))
    draw.ellipse(
        (light - radius, height * 0.27 - radius, light + radius, height * 0.27 + radius),
        fill=horizon,
    )
    for color, base, depth in ((far, 0.52, 0.17), (near, 0.69, 0.17), (ground, 0.89, 0.12)):
        points = [
            (int(width * i / 15), int(height * (base + rng.uniform(-depth, depth))))
            for i in range(16)
        ]
        draw.polygon([*points, (width, height), (0, height)], fill=color)
    return image


def jpeg(image: Image.Image, limit: int) -> bytes:
    resized = image.copy()
    resized.thumbnail((limit, limit))
    output = io.BytesIO()
    resized.save(output, format="JPEG", quality=87, optimize=True)
    return output.getvalue()


def seed_gallery(settings, library: str, year: int) -> None:
    state = settings.library_state(library)
    path = state / "index.db"
    initialize(path)
    thumbs, previews = state / "thumbs", state / "previews"
    thumbs.mkdir()
    previews.mkdir()
    with connect(path) as db, write(db):
        db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
        for index in range(118):
            asset_id = index + 1
            month, day = ((10, 18) if index >= 6 else (7, 23)) if year == 2025 else (5, 4)
            day = f"{year}-{month:02}-{day:02}"
            number = (index * 17 + year) % 127
            height = 720
            width = int(height * RATIOS[number % len(RATIOS)])
            image = landscape(number, width, height)
            preview = jpeg(image, 1600)
            digest = hashlib.sha256(preview).hexdigest()
            name = f"Frame-{asset_id:03}.jpg"
            db.execute(
                """INSERT INTO assets(id,path,folder,kind,size,mtime_ns,content_hash,scan_id,
                                      day,width,height)
                   VALUES(?, ?, '', 'image', ?, 0, ?, 1, ?, ?, ?)""",
                (asset_id, name, len(preview), digest, day, width, height),
            )
            db.execute(
                """INSERT INTO jobs(asset_id,stage,content_hash,status)
                   VALUES(?,'preview',?,'done')""",
                (asset_id, digest),
            )
            cache_path(state, "thumbs", asset_id, digest).write_bytes(jpeg(image, 320))
            cache_path(state, "previews", asset_id, digest).write_bytes(preview)
