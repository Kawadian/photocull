from __future__ import annotations

import fcntl
import logging
import os
import resource
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from sqlite3 import Connection
from threading import Event

from photocull.config import STAGES, Library, Settings, load_settings
from photocull.db import connect, initialize, write
from photocull.db.catalog import mark_catalog_updated
from photocull.db.jobs import (
    DISABLED,
    NO_EMBEDDING_PREVIEW,
    NO_PREVIEW,
    Job,
    claim_jobs,
    claim_scan,
    finish_scan,
    recover,
    skip_unavailable_previews,
)
from photocull.media.cache import cache_path
from photocull.media.walk import MediaJail, SourceChanged, source_key
from photocull.worker.bursts import refresh_bursts
from photocull.worker.insights import Insights
from photocull.worker.insights.story import Stories
from photocull.worker.insights.transfers import Transfers
from photocull.worker.insights.watch import Watch
from photocull.worker.ml import LibraryML
from photocull.worker.ml.model import Siglip
from photocull.worker.ml.process import InferenceProcess
from photocull.worker.rejections import (
    configure_scoring,
    save_scores,
)
from photocull.worker.scan import fallback_day, scan_batches

LOG = logging.getLogger(__name__)
CACHE_DIRS = ("thumbs", "previews", "details")
SEARCH_POLL_SECONDS = 0.05
HEARTBEAT_INTERVAL = 2


class StopWorker(Exception):
    pass


@dataclass
class LibraryWorker:
    db: Connection
    library: Library
    state: Path
    settings: Settings
    pulse: Callable[[], None]
    heartbeat: float = 0
    scan: tuple[int, Iterator[None]] | None = None
    ml: LibraryML | None = None
    insights: Insights | None = None
    stories: Stories | None = None
    watch: Watch | None = None
    transfers: Transfers | None = None

    def beat(self) -> None:
        if time.monotonic() - self.heartbeat < HEARTBEAT_INTERVAL:
            return
        with write(self.db):
            self.db.execute(
                """INSERT INTO worker_status (id, heartbeat, pid) VALUES (1, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET heartbeat = excluded.heartbeat,
                                                 pid = excluded.pid""",
                (time.time(), os.getpid()),
            )
        self.heartbeat = time.monotonic()

    def offline(self) -> None:
        self.db.execute("DELETE FROM worker_status WHERE id = 1 AND pid = ?", (os.getpid(),))

    def configure(self) -> None:
        self.pulse()
        for stage in STAGES:
            if stage in self.settings.worker.stages:
                status, reason = "pending", None
                condition = """status = 'skipped' AND (error = ? OR (
                    stage = 'score' AND error = ? AND asset_id IN (
                        SELECT asset_id FROM jobs WHERE stage = 'preview' AND status = 'done'
                    )
                ))"""
                params = (DISABLED, NO_PREVIEW)
            else:
                status, reason = "skipped", DISABLED
                condition = "status = 'pending'"
                params = ()
            # Startup configuration can affect a large existing queue.
            while True:
                self.pulse()
                with write(self.db):
                    count = self.db.execute(
                        f"""UPDATE jobs SET status = ?, error = ?
                            WHERE stage = ? AND asset_id IN (
                                SELECT asset_id FROM jobs
                                WHERE stage = ? AND {condition} LIMIT 256
                            )""",
                        (status, reason, stage, stage, *params),
                    ).rowcount
                    if count:
                        mark_catalog_updated(self.db)
                if not count:
                    break
        for variant in CACHE_DIRS:
            for temporary in (self.state / variant).glob(".[0-9]*-*-*"):
                temporary.unlink(missing_ok=True)
        configure_scoring(self.db, self.settings.scoring, self.pulse)
        if self.ml is not None:
            self.ml.configure()
        if self.insights is not None:
            for staged in (self.state / "video").glob(".video-*"):
                self.pulse()
                staged.unlink(missing_ok=True)
            self.insights.configure()
        if self.stories is not None:
            self.stories.configure(self.pulse)

    def step_scan(self, pulse: Callable[[], None] | None = None) -> bool:
        if self.scan is None:
            scan_id = claim_scan(self.db)
            if scan_id is None:
                return False
            try:
                jail = MediaJail(self.library, self.settings.state_dir)
            except (OSError, RuntimeError, ValueError) as error:
                finish_scan(self.db, scan_id, str(error))
                return True
            key = self.ml.key if self.ml is not None and self.settings.ml.enabled else None
            batches = scan_batches(
                self.db,
                scan_id,
                jail,
                self.settings.worker,
                pulse or self.pulse,
                key,
                settle_seconds=self.settings.insights.watch_debounce
                if self.settings.insights.watch
                else 0,
                defer=self.watch.notify if self.watch and self.settings.insights.watch else None,
            )
            self.scan = (scan_id, batches)
        scan_id, batches = self.scan
        try:
            next(batches)
        except StopIteration:
            finish_scan(self.db, scan_id)
            self.scan = None
        except (OSError, RuntimeError, ValueError) as error:
            LOG.warning("Scan %s failed: %s", scan_id, error)
            finish_scan(self.db, scan_id, str(error))
            self.scan = None
        return True

    def work(self, job: Job) -> tuple[dict, str | None]:
        if job.stage == "score":
            from photocull.worker.scoring import score_preview

            preview = cache_path(self.state, "previews", job.asset_id, job.content_hash)
            try:
                return asdict(score_preview(preview)), None
            except FileNotFoundError as error:
                raise RuntimeError(
                    "Cached preview is missing. Rerun preview and scoring."
                ) from error

        jail = MediaJail(self.library, self.settings.state_dir)
        with jail.open_media(job.path) as (fd, _):
            if source_key(os.fstat(fd)) != job.source_key:
                raise SourceChanged("Media changed since the scan. Scan the library again.")

            if job.stage == "exif":
                from photocull.media.exif import read_metadata

                result = read_metadata(f"/proc/self/fd/{fd}")
                values = asdict(result)
                values["day"] = (
                    result.captured_at[:10] if result.captured_at else fallback_day(job.mtime_ns)
                )
                error = result.metadata_error
            else:
                from photocull.media.decode import decode

                values = asdict(decode(fd, job.kind, self.state, job.asset_id, job.content_hash))
                error = None

            with jail.open_media(job.path) as (current, _):
                if source_key(os.fstat(current)) != job.source_key:
                    raise SourceChanged("Media changed during decoding. Scan the library again.")
        return values, error

    def finish(self, job: Job, future: Future) -> None:
        try:
            values, error = future.result()
        except Exception as exc:
            values = {}
            error = str(exc) or type(exc).__name__
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                error = exc.stderr.decode(errors="replace").strip() or error
            LOG.warning("%s for %s failed: %s", job.stage, job.path, error)
        with write(self.db):
            if job.stage == "score" and values:
                save_scores(self.db, job.asset_id, values, self.settings.scoring)
            elif job.stage == "exif" and values:
                self.db.execute(
                    """UPDATE assets SET captured_at = :captured_at, day = :day,
                        make = :make, model = :model, latitude = :latitude, longitude = :longitude,
                        metadata_error = :metadata_error WHERE id = :id""",
                    {**values, "id": job.asset_id},
                )
            elif values:
                self.db.execute(
                    """UPDATE assets SET width = :width, height = :height, duration = :duration
                       WHERE id = :id""",
                    {**values, "id": job.asset_id},
                )
            self.db.execute(
                """UPDATE jobs SET status = ?, error = NULLIF(?, ''), interruptions = 0
                   WHERE asset_id = ? AND stage = ?""",
                ("error" if error else "done", error, job.asset_id, job.stage),
            )
            if not error and job.stage == "preview" and "score" in self.settings.worker.stages:
                self.db.execute(
                    """UPDATE jobs SET status = 'pending', error = NULL
                       WHERE asset_id = ? AND stage = 'score' AND status = 'skipped'
                         AND error = ?""",
                    (job.asset_id, NO_PREVIEW),
                )
            if not error and job.stage == "preview" and self.settings.ml.enabled:
                self.db.execute(
                    """UPDATE jobs SET status = 'pending', error = NULL
                       WHERE asset_id = ? AND stage = 'embedding' AND status = 'skipped'
                         AND error = ?""",
                    (job.asset_id, NO_EMBEDDING_PREVIEW),
                )
            mark_catalog_updated(self.db)
        if not error and job.stage == "preview":
            for variant in CACHE_DIRS:
                current = cache_path(self.state, variant, job.asset_id, job.content_hash)
                for old in current.parent.glob(f"{job.asset_id}-*.jpg"):
                    if old != current:
                        old.unlink(missing_ok=True)

    def step(
        self,
        pool: ThreadPoolExecutor,
        heartbeat: Callable[[], None],
        service_search: Callable[[], bool] | None = None,
    ) -> bool:
        self.pulse()
        ml = self.ml if self.settings.ml.enabled else None
        poll_search = service_search or (ml.process_search if ml else None)
        searched = False

        def between_work() -> None:
            nonlocal searched
            self.pulse()
            if poll_search and poll_search():
                searched = True

        # Serve a short FIFO burst before letting background work make progress.
        for _ in range(4):
            if not poll_search or not poll_search():
                break
            searched = True
        if self.watch:
            self.watch.step(between_work)
        scanned = self.step_scan(between_work)
        between_work()
        config = self.settings.worker
        jobs = claim_jobs(self.db, min(config.batch_size, config.parallelism))
        pending = {pool.submit(self.work, job): job for job in jobs}
        while pending:
            heartbeat()
            done, _ = wait(
                pending,
                timeout=SEARCH_POLL_SECONDS if poll_search else 1,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                self.finish(pending.pop(future), future)
            between_work()
        embedded = ml.process_images() if ml else False
        between_work()
        tagged = ml.process_tags() if ml else False
        busy = bool(scanned or jobs or embedded or tagged)
        if self.insights and self.insights.clean_video():
            return True
        if not busy and self.transfers and self.transfers.step(between_work):
            return True
        if self.insights and self.insights.step(between_work, idle=not busy):
            return True
        if busy:
            return True
        if skip_unavailable_previews(self.db):
            return True
        between_work()
        if refresh_bursts(
            self.db,
            self.settings.burst,
            between_work,
            ml.key if ml else None,
            aesthetic_key=self.insights.identities["aesthetic"].key
            if self.insights and "aesthetic" in self.insights.identities
            else None,
        ):
            return True
        story = self.stories.step(between_work) if self.stories else False
        return (ml.group() if ml else False) or story or searched


def run(settings: Settings, stop: Event, *, model_factory=Siglip, face_factory=None) -> int:
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    if not settings.libraries:
        LOG.info("No libraries configured")
    try:
        with ExitStack() as stack:
            workers: list[LibraryWorker] = []
            inference = InferenceProcess(settings, model_factory)
            ready = False
            search_cursor = 0
            next_search_probe = 0.0

            def heartbeat() -> None:
                if ready:
                    for worker in workers:
                        worker.beat()

            def pulse() -> None:
                if stop.is_set():
                    raise StopWorker
                heartbeat()

            def service_search() -> bool:
                nonlocal search_cursor, next_search_probe
                if time.monotonic() < next_search_probe or inference.cooling_down:
                    return False
                for offset in range(len(workers)):
                    index = (search_cursor + offset) % len(workers)
                    if workers[index].ml.process_search():
                        search_cursor = (index + 1) % len(workers)
                        return True
                next_search_probe = time.monotonic() + SEARCH_POLL_SECONDS
                return False

            for name, library in sorted(settings.libraries.items()):
                state = settings.library_state(name)
                path = state / "index.db"
                initialize(path)
                lock = stack.enter_context((state / "worker.lock").open("a"))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(f"A worker already owns library {name}") from exc
                db = stack.enter_context(connect(path))
                db.execute("DELETE FROM worker_status")
                workers.append(
                    LibraryWorker(
                        db,
                        library,
                        state,
                        settings,
                        pulse,
                        ml=LibraryML(db, state, inference, pulse),
                    )
                )
            for worker in workers:
                worker.insights = Insights(worker, face_factory)
                worker.stories = Stories(worker)
                worker.transfers = Transfers(worker)
                worker.transfers.recover()
                worker.watch = Watch(worker)
                worker.watch.start()
                stack.callback(worker.watch.close)
                recover(worker.db)
                worker.configure()
            config = settings.worker
            pool = stack.enter_context(ThreadPoolExecutor(max_workers=config.parallelism))
            stack.callback(inference.close)
            for worker in workers:
                stack.callback(worker.offline)
            ready = True
            heartbeat()
            poll_search = service_search if settings.ml.enabled else None
            while not stop.is_set():
                busy = False
                for worker in workers:
                    busy = worker.step(pool, heartbeat, poll_search) or busy
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                    if rss_mb >= config.memory_mb:
                        LOG.warning("Worker reached %.0f MiB RSS and will restart", rss_mb)
                        return 75
                if not busy:
                    stop.wait(SEARCH_POLL_SECONDS if poll_search else 1)
    except StopWorker:
        pass
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = Event()

    def handle_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    raise SystemExit(run(load_settings(), stop))
