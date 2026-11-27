"""Generate photo-like landscapes for testing timeline layout with varied image sizes."""

from __future__ import annotations

import hashlib
import io
import random

from PIL import Image, ImageDraw, ImageOps

from photocull.db import connect, initialize, write
from photocull.media.cache import cache_path
from photocull.media.derivatives import VARIANTS, derivative_path, recipe
from photocull.media.walk import source_key
from photocull.worker.derivatives import encode_image, image_blurhash

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
            source = settings.libraries[library].root / name
            source.write_bytes(preview)
            info = source.stat()
            db.execute(
                """INSERT INTO assets(id,path,folder,kind,size,mtime_ns,content_hash,scan_id,
                                      day,width,height,source_key)
                   VALUES(?, ?, '', 'image', ?, ?, ?, 1, ?, ?, ?, ?)""",
                (
                    asset_id,
                    name,
                    len(preview),
                    info.st_mtime_ns,
                    digest,
                    day,
                    width,
                    height,
                    source_key(info),
                ),
            )
            db.execute(
                """INSERT INTO jobs(asset_id,stage,content_hash,status)
                   VALUES(?,'preview',?,'done')""",
                (asset_id, digest),
            )
            cache_path(state, "thumbs", asset_id, digest).write_bytes(jpeg(image, 320))
            cache_path(state, "previews", asset_id, digest).write_bytes(preview)
            if asset_id >= 107:
                seed_derivatives(settings, db, library, asset_id, digest, image)
            if asset_id == 118:
                db.execute(
                    """UPDATE assets SET make='Photocull', model='Test Camera',
                       lens='35 mm Prime', aperture=2.8, shutter_seconds=0.004,
                       focal_length_mm=35, iso=200, latitude=35.68, longitude=139.76 WHERE id=?""",
                    (asset_id,),
                )


def seed_derivatives(settings, db, library, asset_id, digest, image) -> None:
    version = digest + "-" + recipe(settings.derivatives)
    db.execute(
        """INSERT INTO derivative_placeholders(asset_id,version,blurhash) VALUES(?,?,?)""",
        (asset_id, version, image_blurhash(image)),
    )
    for name in ("grid-256.avif", "grid-512.avif", "fit-1280.avif", "fit-1920.avif"):
        variant = VARIANTS[name]
        target = derivative_path(settings, library, asset_id, variant, version)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as file:
            encode_image(image, variant, settings.derivatives, file)
        db.execute(
            """INSERT INTO derivative_files
               (asset_id,variant,version,tier,status,priority,requested_at,last_access,byte_size)
               VALUES (?,?,?,?,'ready',1,0,0,?)""",
            (asset_id, variant.name, version, variant.tier, target.stat().st_size),
        )
