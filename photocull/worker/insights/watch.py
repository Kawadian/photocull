from __future__ import annotations

import logging
import time
from contextlib import closing
from pathlib import Path
from threading import Lock

from photocull.db.jobs import request_scan
from photocull.media.walk import EXCLUDED, EXTENSIONS, MediaJail, source_key

LOG = logging.getLogger(__name__)


class Watch:
    """Treat notifications as hints and reconcile against the indexed source keys."""

    def __init__(self, worker):
        self.worker = worker
        self.config = worker.settings.insights
        self.lock = Lock()
        self.changed_at: float | None = None
        self.generation = 0
        self.next_poll = 0.0
        self.observer = None

    def start(self):
        if not self.config.watch or self.config.watch_mode == "poll":
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            LOG.info("Watchdog unavailable; using polling")
            return

        watch = self
        state_dir = self.worker.settings.state_dir.resolve()

        class Handler(FileSystemEventHandler):
            def __init__(self, root: Path):
                super().__init__()
                self.root = root

            def on_any_event(self, event):
                if event.event_type in {"opened", "closed_no_write"} or (
                    event.is_directory and event.event_type == "modified"
                ):
                    return
                for value in (event.src_path, event.dest_path):
                    if not value:
                        continue
                    path = Path(value)
                    if path.is_relative_to(state_dir) or not path.is_relative_to(self.root):
                        continue
                    if any(
                        part.casefold() in EXCLUDED for part in path.relative_to(self.root).parts
                    ):
                        continue
                    if event.is_directory or path.suffix[1:].casefold() in EXTENSIONS:
                        watch.notify()
                        break

        observer = Observer()
        try:
            # The periodic scan covers aliases and filesystems with incomplete events.
            for root in {root.resolve() for root in self.worker.library.roots}:
                observer.schedule(Handler(root), str(root), recursive=True)
            observer.start()
        except (OSError, RuntimeError) as error:
            observer.stop()
            if observer.is_alive():
                observer.join()
            LOG.warning("Filesystem notification failed; using polling: %s", error)
        else:
            self.observer = observer

    def close(self):
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()

    def notify(self):
        with self.lock:
            self.generation += 1
            self.changed_at = time.monotonic()

    def differs(self, pulse) -> bool:
        jail = MediaJail(self.worker.library, self.worker.settings.state_dir)
        indexed = dict(self.worker.db.execute("SELECT path, source_key FROM assets WHERE active=1"))
        with closing(jail.walk(pulse)) as entries:
            for entry in entries:
                if indexed.pop(entry.path, None) != source_key(entry.stat):
                    return True
        return bool(indexed)

    def step(self, pulse):
        if not self.config.watch or self.worker.scan is not None:
            return
        now = time.monotonic()
        with self.lock:
            generation, changed_at = self.generation, self.changed_at
        due = changed_at is not None and now - changed_at >= self.config.watch_debounce
        if not due and now < self.next_poll:
            return
        if self.worker.db.execute(
            "SELECT 1 FROM scan_requests WHERE status IN ('pending', 'running')"
        ).fetchone():
            return
        self.next_poll = now + self.config.watch_interval
        try:
            differs = self.differs(pulse)
        except (OSError, ValueError, RuntimeError) as error:
            LOG.warning("Watch polling failed: %s", error)
            with self.lock:
                if self.generation == generation:
                    self.changed_at = None  # The next scheduled poll retries reconciliation.
            return
        with self.lock:
            if self.generation != generation:
                return  # Changes during reconciliation need their own quiet interval.
            if not differs:
                self.changed_at = None
                return
            if not due:
                if changed_at is None:
                    self.changed_at = now
                return
            self.changed_at = None
        # The unique scan index coalesces any manual request made during the walk.
        request_scan(self.worker.db)
