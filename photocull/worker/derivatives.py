from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

from photocull.config import Settings
from photocull.db import derivatives as queue
from photocull.db import write
from photocull.media.derivatives import VARIANTS, VIDEO_LIMITS, derivative_path, recipe
from photocull.media.walk import MediaJail, SourceChanged, source_key

LOG = logging.getLogger(__name__)
MIB = 1024 * 1024


def encode_image(pixels, variant, settings, file) -> None:
    from PIL import Image

    from photocull.media.color import SRGB

    ext = variant.extension
    format_name, options = {
        "jpg": ("JPEG", {"quality": settings.jpeg_quality, "progressive": True, "optimize": True}),
        "avif": ("AVIF", {"quality": settings.avif_quality, "speed": 8, "max_threads": 1}),
        "webp": ("WEBP", {"quality": settings.webp_quality, "method": 4}),
    }[ext]
    # Construct clean pixels explicitly. Pillow plugins can otherwise inherit source metadata.
    with pixels.copy() as image:
        image.info.clear()
        if variant.edge:
            image.thumbnail((variant.edge, variant.edge), Image.Resampling.LANCZOS)
        image.save(file, format=format_name, icc_profile=SRGB, **options)


def image_blurhash(image) -> str:
    import blurhash
    from PIL import Image

    with (
        image.resize((24, 24), Image.Resampling.BILINEAR) as resized,
        resized.convert("RGB") as sample,
    ):
        pixels = sample.tobytes()
    rgb = list(zip(pixels[::3], pixels[1::3], pixels[2::3], strict=True))
    return blurhash.encode([rgb[i : i + 24] for i in range(0, len(rgb), 24)], 4, 3)


@contextmanager
def staged_image(target: Path, pixels, variant, settings):
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, prefix=".derivative-", delete=False) as file:
        staged = Path(file.name)
        try:
            encode_image(pixels, variant, settings, file)
            file.close()
            yield staged
        finally:
            staged.unlink(missing_ok=True)


def _generate(settings: Settings, library: str, jobs: list[queue.DerivativeJob], pulse: Callable):
    from PIL import Image

    from photocull.media.cache import cache_path
    from photocull.media.decode import decode_source
    from photocull.worker.insights.video import proxy

    first = jobs[0]
    config = settings.derivatives
    jail = MediaJail(settings.libraries[library], settings.state_dir, validate=False)
    sizes = {}
    blurhash = None

    def verify(fd):
        if source_key(os.fstat(fd)) != first.source_key:
            raise SourceChanged("Media changed since scan")

    with jail.open_media(first.path) as (fd, _), ExitStack() as stack:
        verify(fd)
        video = first.variant in VIDEO_LIMITS
        if not video:
            digest = first.version.split("-", 1)[0]
            legacy = cache_path(settings.library_state(library), "previews", first.asset_id, digest)
            if first.kind == "video" and legacy.is_file():
                pixels = stack.enter_context(Image.open(legacy))
            else:
                pixels, _ = decode_source(fd, first.kind, color_managed=True)
                stack.enter_context(pixels)
            blurhash = image_blurhash(pixels)
        for job in jobs:
            pulse()
            variant = VARIANTS[job.variant]
            target = derivative_path(settings, library, job.asset_id, variant, job.version)
            budget = (config.hot_mib if variant.tier == "hot" else config.large_mib) * MIB
            output = (
                proxy(
                    f"/proc/self/fd/{fd}",
                    fd,
                    1,
                    target,
                    pulse,
                    limits=VIDEO_LIMITS[job.variant],
                    max_bytes=budget,
                )
                if video
                else staged_image(target, pixels, variant, config)
            )
            try:
                with output as staged:
                    with jail.open_media(first.path) as (current, _):
                        verify(current)
                    size = staged.stat().st_size
                    if not 0 < size <= budget:
                        raise ValueError("File exceeds the derivative cache limit")
                    staged.chmod(0o644)
                    staged.replace(target)
                sizes[job.variant] = size
            except (OSError, RuntimeError, ValueError) as error:
                LOG.warning(
                    "Derivative %s/%s %s failed: %s", library, job.asset_id, job.variant, error
                )
    return sizes, blurhash


class Derivatives:
    def __init__(self, settings: Settings, workers, stack: ExitStack, stop):
        self.settings = settings
        self.catalogs = {worker.state.name: worker.db for worker in workers}
        self.enabled = settings.derivatives.enabled and bool(self.catalogs)
        self.recipe = recipe(settings.derivatives)
        self.pending: tuple[str, list[queue.DerivativeJob], Future] | None = None
        self.next_maintenance = 0.0
        self.next_warmup = 0.0
        self.cursor = 0
        self.warm_cursor = 0
        if self.enabled:
            self.pool = stack.enter_context(
                ThreadPoolExecutor(max_workers=1, initializer=os.nice, initargs=(8,))
            )
            for name, db in self.catalogs.items():
                for entry in queue.recover_files(db):
                    if variant := VARIANTS.get(entry["variant"]):
                        with suppress(OSError, ValueError):
                            derivative_path(
                                settings, name, entry["asset_id"], variant, entry["version"]
                            ).unlink(missing_ok=True)
            for tier in ("hot", "large"):
                root = settings.derivative_root(tier)
                for name in self.catalogs:
                    for prefix in (".derivative-", ".video-"):
                        for staged in (root / name).glob(f"*/{prefix}*"):
                            staged.unlink(missing_ok=True)
        self.stop = stop

    def _pulse(self) -> None:
        if self.stop.is_set():
            raise InterruptedError("Worker stopping")

    def _maintain(self) -> None:
        for db in self.catalogs.values():
            queue.discard_stale_requests(db, self.recipe)
        self._trim()

    def _trim(self) -> None:
        config = self.settings.derivatives
        for tier, budget in (("hot", config.hot_mib * MIB), ("large", config.large_mib * MIB)):
            usage = self._usage(tier)
            while usage > budget:
                candidates = []
                for name, db in self.catalogs.items():
                    row = db.execute(
                        """SELECT asset_id, variant, version, byte_size, last_access
                           FROM derivative_files WHERE tier = ? AND status = 'ready'
                           ORDER BY last_access LIMIT 1""",
                        (tier,),
                    ).fetchone()
                    if row:
                        candidates.append((row["last_access"], name, row))
                if not candidates:
                    break
                _, name, oldest = min(candidates)
                variant = VARIANTS.get(oldest["variant"])
                key = (oldest["asset_id"], oldest["variant"], oldest["version"])
                if variant:
                    try:
                        derivative_path(self.settings, name, key[0], variant, key[2]).unlink(
                            missing_ok=True
                        )
                    except OSError as error:
                        LOG.warning("Could not evict derivative %s/%s: %s", name, key[0], error)
                        break
                with write(self.catalogs[name]):
                    self.catalogs[name].execute(
                        """DELETE FROM derivative_files
                           WHERE asset_id = ? AND variant = ? AND version = ?
                             AND status = 'ready'""",
                        key,
                    )
                usage -= oldest["byte_size"]

    def _usage(self, tier: str) -> int:
        return sum(
            db.execute("SELECT byte_size FROM derivative_usage WHERE tier = ?", (tier,)).fetchone()[
                0
            ]
            for db in self.catalogs.values()
        )

    def _prewarm(self, names: tuple[str, ...], now: float) -> bool:
        limit = self.settings.derivatives.prewarm
        if (
            not limit
            or now < self.next_warmup
            or self._usage("hot") > self.settings.derivatives.hot_mib * MIB * 0.8
        ):
            return False
        for offset in range(len(names)):
            index = (self.warm_cursor + offset) % len(names)
            name = names[index]
            db = self.catalogs[name]
            row = db.execute(
                """WITH recent_access AS (
                       SELECT asset_id FROM derivative_files
                       WHERE tier = 'hot' AND status = 'ready' AND hits > 0 AND last_access >= ?
                       ORDER BY last_access DESC LIMIT 160
                   ), viewed_day AS (
                       SELECT a.day FROM recent_access AS d JOIN assets AS a ON a.id = d.asset_id
                       GROUP BY a.day ORDER BY count(*) DESC, a.day DESC LIMIT 1
                   ), newest AS (
                       SELECT a.id, a.content_hash FROM assets AS a
                       JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
                       WHERE a.active = 1 AND p.status = 'done'
                       ORDER BY a.day DESC, a.timeline_time DESC, a.id DESC LIMIT ?
                   ), viewed AS (
                       SELECT a.id, a.content_hash FROM assets AS a
                       JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
                       WHERE a.active = 1 AND p.status = 'done'
                         AND a.day = (SELECT day FROM viewed_day)
                       ORDER BY a.timeline_time DESC, a.id DESC LIMIT ?
                   ), candidates AS (
                       SELECT * FROM newest UNION ALL SELECT * FROM viewed
                   )
                   SELECT c.id, c.content_hash || '-' || ? AS version FROM candidates AS c
                   WHERE length(c.content_hash) = 64 AND NOT EXISTS (
                       SELECT 1 FROM derivative_files AS d
                       WHERE d.asset_id = c.id AND d.variant = 'grid-256.avif'
                         AND d.version = c.content_hash || '-' || ?
                   ) LIMIT 1""",
                (now - 604800, limit, limit, self.recipe, self.recipe),
            ).fetchone()
            if row:
                self.warm_cursor = (index + 1) % len(names)
                return (
                    queue.request_file(
                        db, row["id"], VARIANTS["grid-256.avif"], row["version"], now, priority=0
                    )
                    == "pending"
                )
        self.next_warmup = now + 15
        return False

    def step(self, *, available: bool, idle: bool) -> bool:
        if not self.enabled:
            return False
        now = time.time()
        if self.pending:
            name, jobs, future = self.pending
            if not future.done():
                return False
            self.pending = None
            try:
                sizes, blurhash = future.result()
            except Exception as error:
                LOG.warning(
                    "Derivative generation failed for %s/%s: %s", name, jobs[0].asset_id, error
                )
                sizes, blurhash = {}, None
            queue.finish_files(self.catalogs[name], jobs, sizes, blurhash, now)
            self._trim()
        if not available:
            return False
        if now >= self.next_maintenance:
            self._maintain()
            self.next_maintenance = now + 30
        names = tuple(self.catalogs)
        for priority in (1, 0) if idle else (1,):
            for offset in range(len(names)):
                index = (self.cursor + offset) % len(names)
                name = names[index]
                if jobs := queue.claim_files(self.catalogs[name], self.recipe, priority):
                    self.cursor = (index + 1) % len(names)
                    future = self.pool.submit(_generate, self.settings, name, jobs, self._pulse)
                    self.pending = (name, jobs, future)
                    return True
        return idle and self._prewarm(names, now)
