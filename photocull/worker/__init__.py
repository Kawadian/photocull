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

from photocull.config import Library, Settings, load_settings
from photocull.db import connect, initialize, write
from photocull.db.jobs import DISABLED, STAGES, Job, claim_jobs, claim_scan, finish_scan, recover
from photocull.media.cache import cache_path
from photocull.media.walk import MediaJail, SourceChanged, source_key
from photocull.worker.scan import fallback_day, mark_catalog_updated, scan_batches

LOG = logging.getLogger(__name__)
CACHE_DIRS = ("thumbs", "previews", "details")


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

    def beat(self) -> None:
        if time.monotonic() - self.heartbeat < 5:
            return
        with write(self.db):
            self.db.execute(
                """INSERT INTO worker_status (id, heartbeat, pid) VALUES (1, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET heartbeat = excluded.heartbeat,
                                                 pid = excluded.pid""",
                (time.time(), os.getpid()),
            )
        self.heartbeat = time.monotonic()

    def configure(self) -> None:
        self.pulse()
        for stage in STAGES:
            if stage in self.settings.worker.stages:
                status, reason = "pending", None
                condition = "status = 'skipped' AND error = ?"
                params = (DISABLED,)
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
                if not count:
                    break
        for variant in CACHE_DIRS:
            for temporary in (self.state / variant).glob(".[0-9]*-*-*"):
                temporary.unlink(missing_ok=True)

    def step_scan(self) -> bool:
        if self.scan is None:
            scan_id = claim_scan(self.db)
            if scan_id is None:
                return False
            try:
                jail = MediaJail(self.library, self.settings.state_dir)
            except (OSError, RuntimeError, ValueError) as error:
                finish_scan(self.db, scan_id, str(error))
                return True
            batches = scan_batches(self.db, scan_id, jail, self.settings.worker, self.pulse)
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
            if values:
                setters = ", ".join(f"{name} = ?" for name in values)
                self.db.execute(
                    f"UPDATE assets SET {setters} WHERE id = ?", (*values.values(), job.asset_id)
                )
            self.db.execute(
                "UPDATE jobs SET status = ?, error = ? WHERE asset_id = ? AND stage = ?",
                ("error" if error else "done", error, job.asset_id, job.stage),
            )
            mark_catalog_updated(self.db)
        if not error and job.stage == "preview":
            for variant in CACHE_DIRS:
                current = cache_path(self.state, variant, job.asset_id, job.content_hash)
                for old in current.parent.glob(f"{job.asset_id}-*.jpg"):
                    if old != current:
                        old.unlink(missing_ok=True)

    def step(self, pool: ThreadPoolExecutor, heartbeat: Callable[[], None]) -> bool:
        self.pulse()
        scanned = self.step_scan()
        self.pulse()
        config = self.settings.worker
        jobs = claim_jobs(self.db, min(config.batch_size, config.parallelism))
        pending = {pool.submit(self.work, job): job for job in jobs}
        while pending:
            heartbeat()
            done, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                self.finish(pending.pop(future), future)
        return scanned or bool(jobs)


def run(settings: Settings, stop: Event) -> int:
    if not settings.libraries:
        LOG.info("No libraries configured")
    try:
        with ExitStack() as stack:
            workers: list[LibraryWorker] = []

            def heartbeat() -> None:
                for worker in workers:
                    worker.beat()

            def pulse() -> None:
                if stop.is_set():
                    raise StopWorker
                heartbeat()

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
                workers.append(LibraryWorker(db, library, state, settings, pulse))
            for worker in workers:
                recover(worker.db)
                worker.configure()
            config = settings.worker
            pool = stack.enter_context(ThreadPoolExecutor(max_workers=config.parallelism))
            while not stop.is_set():
                busy = False
                for worker in workers:
                    busy = worker.step(pool, heartbeat) or busy
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                    if rss_mb >= config.memory_mb:
                        LOG.warning("Worker reached %.0f MiB RSS and will restart", rss_mb)
                        return 75
                if not busy:
                    stop.wait(1)
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
