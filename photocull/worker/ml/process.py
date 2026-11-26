from __future__ import annotations

import logging
import os
import resource
import time
from importlib.util import find_spec
from multiprocessing import get_context
from pathlib import Path

from photocull.config import Settings
from photocull.worker.ml.model import ModelUnavailable, Siglip, set_native_threads

LOG = logging.getLogger(__name__)
MIB = 1024 * 1024
MIN_CHILD_MB = 1536
PARENT_HEADROOM_MB = 512
PACKAGES = ("numpy", "PIL", "onnxruntime", "tokenizers", "usearch")


def _child(channel, settings, cache, memory_bytes: int, factory) -> None:
    with channel:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            set_native_threads()
            from photocull.worker.ml.engine import Engine

            engine = Engine(settings, cache, factory)
            channel.send(("ok", None))
        except Exception as error:
            channel.send(("fatal", str(error) or type(error).__name__))
            return
        try:
            while True:
                operation, args = channel.recv()
                try:
                    channel.send(("ok", getattr(engine, operation)(*args)))
                except MemoryError:
                    channel.send(("fatal", "ML reached its memory allocation limit"))
                    break
                except ModelUnavailable as error:
                    channel.send(("model", str(error)))
                except Exception as error:
                    LOG.exception("ML operation %s failed", operation)
                    channel.send(("error", str(error) or type(error).__name__))
        except (EOFError, BrokenPipeError):
            pass
        finally:
            engine.drop_group()


class InferenceProcess:
    """One lazy, memory-limited CPU process shared by all libraries."""

    def __init__(self, settings: Settings, factory=Siglip):
        self.settings = settings
        self.factory = factory
        self.process = None
        self.channel = None
        self.error: str | None = None
        if settings.ml.enabled:
            missing = [name for name in PACKAGES if find_spec(name) is None]
            if missing:
                self.error = f"Install photocull[ml] on the worker (missing {', '.join(missing)})"
            elif self._budget() < MIN_CHILD_MB * MIB:
                self.error = self._memory_message()

    def _memory_message(self) -> str:
        return (
            f"The {self.settings.worker.memory_mb} MiB worker budget leaves less than "
            f"{MIN_CHILD_MB} MiB for ML. Raise PHOTOCULL_WORKER_MEMORY_MB to at least 3072."
        )

    def _budget(self) -> int:
        resident = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        headroom = max(PARENT_HEADROOM_MB, 256 * self.settings.worker.parallelism)
        return (self.settings.worker.memory_mb - headroom) * MIB - resident

    def _receive(self, pulse):
        # Downloads use an I/O timeout, and this also bounds lock waits and native calls.
        deadline = time.monotonic() + 300
        while True:
            pulse()
            if self.channel.poll(0.2):
                status, value = self.channel.recv()
                if status == "ok":
                    return value
                if status == "fatal":
                    self.error = value
                    self.close()
                if status in {"model", "fatal"}:
                    raise ModelUnavailable(value)
                raise ValueError(value)
            if not self.process.is_alive() or time.monotonic() >= deadline:
                raise EOFError

    def call(self, operation: str, args: tuple, pulse):
        if self.error:
            raise ModelUnavailable(self.error)
        try:
            if self.process is None:
                budget = self._budget()
                if budget < MIN_CHILD_MB * MIB:
                    self.error = self._memory_message()
                    raise ModelUnavailable(self.error)
                context = get_context("spawn")
                parent, child = context.Pipe()
                process = context.Process(
                    target=_child,
                    args=(child, self.settings.ml, self.settings.model_cache, budget, self.factory),
                    daemon=True,
                )
                try:
                    process.start()
                except BaseException:
                    parent.close()
                    raise
                finally:
                    child.close()
                self.process, self.channel = process, parent
                self._receive(pulse)
            self.channel.send((operation, args))
            return self._receive(pulse)
        except (EOFError, OSError) as error:
            self.error = (
                "The ML process exited or timed out. Check worker logs and the memory limit "
                "before restarting the worker."
            )
            self.close()
            raise ModelUnavailable(self.error) from error

    def close(self) -> None:
        if self.channel is not None:
            self.channel.close()
            self.channel = None
        if self.process is not None:
            self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()
            self.process.close()
            self.process = None
