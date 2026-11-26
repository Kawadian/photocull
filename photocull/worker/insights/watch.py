from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Lock

from photocull.db.jobs import request_scan
from photocull.media.walk import EXCLUDED, EXTENSIONS, MediaJail, source_key

LOG = logging.getLogger(__name__)


class Watch:
    """Use filesystem events for latency and a jailed metadata walk for reconciliation."""

    def __init__(self, worker):
        self.worker = worker
        self.config = worker.settings.insights
        self.lock = Lock()
        self.changed_at = None
        self.next_poll = 0.0
        self.snapshot: dict[str, str] | None = None
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

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in {"opened", "closed_no_write"}:
                    return
                for value in (event.src_path, event.dest_path):
                    if not value:
                        continue
                    path = Path(value)
                    if path.is_relative_to(watch.worker.settings.state_dir.absolute()):
                        continue
                    if any(part.casefold() in EXCLUDED for part in path.parts):
                        continue
                    if event.is_directory or path.suffix[1:].lower() in EXTENSIONS:
                        watch.notify()
                        break

        observer = Observer()
        try:
            # Direct roots get notifications. Symlink targets and remote mounts are
            # always reconciled by the canonical walker as well.
            for root in set(self.worker.library.roots):
                observer.schedule(Handler(), str(root), recursive=True)
            observer.start()
            self.observer = observer
        except OSError as error:
            observer.stop()
            LOG.warning("Filesystem notification failed; using polling: %s", error)

    def close(self):
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()

    def notify(self):
        with self.lock:
            self.changed_at = time.monotonic()

    def step(self, pulse):
        if not self.config.watch:
            return
        now = time.monotonic()
        if now >= self.next_poll:
            self.next_poll = now + self.config.watch_interval
            try:
                jail = MediaJail(self.worker.library, self.worker.settings.state_dir)
                snapshot = {entry.path: source_key(entry.stat) for entry in jail.walk(pulse)}
                if snapshot != self.snapshot:
                    self.notify()
                self.snapshot = snapshot
            except (OSError, ValueError, RuntimeError) as error:
                LOG.warning("Watch polling failed: %s", error)
        with self.lock:
            ready = self.changed_at is not None and (
                time.monotonic() - self.changed_at >= self.config.watch_debounce
            )
            if ready and self.worker.scan is None:
                self.changed_at = None
                request_scan(self.worker.db)
