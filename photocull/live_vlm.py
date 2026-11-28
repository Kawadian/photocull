from __future__ import annotations

import logging
from threading import Lock

from photocull.config import ProviderSelection, VLMSettings
from photocull.settings_store import SettingsError, SettingsStore

LOG = logging.getLogger(__name__)

LIVE_VLM_FIELDS = frozenset(
    {
        "vlm.tags",
        "vlm.flags",
        "vlm.on_failure",
        "vlm.openai.timeout_seconds",
        "vlm.openai.concurrency",
        "vlm.openai.requests_per_minute",
        "vlm.openai.max_requests_per_run",
        "vlm.openai.max_requests_per_day",
    }
)
PROVIDER_FIELDS = {"vlm.tags", "vlm.flags"}


class LiveVLM:
    def __init__(self, startup: VLMSettings, store: SettingsStore | None):
        self.startup = startup
        self.store = store
        self.revision: tuple[int, ...] | None = None
        self.current = startup
        self.lock = Lock()
        self.read_error = False

    def same_endpoint(self, candidate: VLMSettings) -> bool:
        before, after = self.startup.openai, candidate.openai
        return before.base_url == after.base_url and before.model == after.model

    def read(self) -> VLMSettings:
        if self.store is None:
            return self.startup
        with self.lock:
            try:
                try:
                    sig = self.store.active.lstat()
                    fingerprint = sig.st_dev, sig.st_ino, sig.st_mtime_ns, sig.st_size
                except FileNotFoundError:
                    fingerprint = ()
                if fingerprint != self.revision:
                    candidate = self.store.read().settings.vlm
                    if not self.same_endpoint(candidate):
                        # In particular, never start sending photos to the old endpoint
                        # when the admin enabled remote analysis for a newly saved one.
                        candidate = candidate.model_copy(
                            update={
                                kind: getattr(self.current, kind)
                                if getattr(candidate, kind) == "openai"
                                else "local"
                                for kind in ("tags", "flags")
                            }
                        )
                    self.current = candidate.model_copy(
                        update={
                            "openai": candidate.openai.model_copy(
                                update={
                                    "base_url": self.startup.openai.base_url,
                                    "model": self.startup.openai.model,
                                }
                            )
                        }
                    )
                    self.revision = fingerprint
                self.read_error = False
            except (OSError, SettingsError, RuntimeError):
                if not self.read_error:
                    LOG.warning(
                        "Unable to reload VLM settings; keeping the last valid configuration"
                    )
                    self.read_error = True
            return self.current

    def selection(self) -> ProviderSelection:
        return self.read().selection
