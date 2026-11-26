from __future__ import annotations

import ctypes
import logging
import os
import resource
import signal
import time
from contextlib import suppress
from importlib.util import find_spec
from multiprocessing import get_context
from pathlib import Path

from photocull.config import Settings
from photocull.worker.ml.model import ModelUnavailable, Siglip, set_native_threads

LOG = logging.getLogger(__name__)
MIB = 1024 * 1024
MIN_CHILD_MB = 1536
PARENT_HEADROOM_MB = 512
PACKAGES = ("numpy", "PIL", "onnxruntime", "tokenizers", "usearch", "numkong")
MAX_INFERENCE_BATCH = 4


class InferenceInterrupted(RuntimeError):
    pass


def _child(channel, settings, cache, memory_bytes: int, factory, parent_pid: int) -> None:
    with channel:
        try:
            # Linux PR_SET_PDEATHSIG prevents orphan inference and SQLite writers.
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
                return
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            memory_bytes = (
                memory_bytes if hard == resource.RLIM_INFINITY else min(memory_bytes, hard)
            )
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            set_native_threads()
            from photocull.worker.ml.engine import Engine

            engine = Engine(settings, cache, factory)
            channel.send(("ok", None))
            while True:
                operation, args = channel.recv()
                try:
                    channel.send(("ok", getattr(engine, operation)(*args)))
                except ModelUnavailable as error:
                    channel.send(("model", str(error)))
                except MemoryError:
                    raise
                except Exception as error:
                    LOG.exception("ML operation %s failed", operation)
                    channel.send(("error", str(error) or type(error).__name__))
        except (EOFError, BrokenPipeError):
            pass
        except MemoryError:
            with suppress(EOFError, OSError):
                channel.send(("interrupted", "ML reached its memory allocation limit."))
        except Exception:
            LOG.exception("ML subprocess failed to initialize")


class InferenceProcess:
    """One lazy, memory-limited CPU process shared by all libraries."""

    def __init__(self, settings: Settings, factory=Siglip):
        self.settings = settings
        self.factory = factory
        self.process = None
        self.channel = None
        self.error: str | None = None
        self.retry_at = 0.0
        self.failures = 0
        self.image_batch_size = min(MAX_INFERENCE_BATCH, settings.ml.batch_size)
        if settings.ml.enabled:
            missing = [name for name in PACKAGES if find_spec(name) is None]
            if missing:
                self.error = f"Install photocull[ml] on the worker (missing {', '.join(missing)})"
            elif self._budget() < MIN_CHILD_MB * MIB:
                self.error = self._memory_message()

    @property
    def cooling_down(self) -> bool:
        return time.monotonic() < self.retry_at

    def _memory_message(self) -> str:
        return (
            f"The {self.settings.worker.memory_mb} MiB worker budget leaves less than "
            f"{MIN_CHILD_MB} MiB for ML. Raise PHOTOCULL_WORKER_MEMORY_MB to at least 3072."
        )

    def _budget(self) -> int:
        resident = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        headroom = max(PARENT_HEADROOM_MB, 256 * self.settings.worker.parallelism)
        return (self.settings.worker.memory_mb - headroom) * MIB - resident

    def _receive(self, pulse, deadline: float):
        while True:
            pulse()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InferenceInterrupted("The ML operation timed out. Try again shortly.")
            if self.channel.poll(min(0.05, remaining)):
                status, value = self.channel.recv()
                if status == "ok":
                    return value
                if status == "interrupted":
                    raise InferenceInterrupted(value)
                if status == "model":
                    raise ModelUnavailable(value)
                raise ValueError(value)
            if not self.process.is_alive():
                raise EOFError

    def _start(self, pulse, deadline: float) -> None:
        budget = self._budget()
        if budget < MIN_CHILD_MB * MIB:
            raise ModelUnavailable(self._memory_message())
        context = get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_child,
            args=(
                child,
                self.settings.ml,
                self.settings.model_cache,
                budget,
                self.factory,
                os.getpid(),
            ),
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            process.close()
            raise
        finally:
            child.close()
        self.process, self.channel = process, parent
        self._receive(pulse, deadline)

    def call(self, operation: str, args: tuple, pulse, *, timeout: float = 90):
        if self.error:
            raise ModelUnavailable(self.error)
        deadline = time.monotonic() + timeout
        try:
            if self.process is None:
                self._start(pulse, deadline)
            self.channel.send((operation, args))
            result = self._receive(pulse, deadline)
        except (EOFError, OSError, InferenceInterrupted) as error:
            self._close(interrupted=True)
            self.failures = min(self.failures + 1, 5)
            self.retry_at = time.monotonic() + 0.2 * (2 ** (self.failures - 1))
            if operation == "images":
                self.image_batch_size = max(1, self.image_batch_size // 2)
            if isinstance(error, InferenceInterrupted):
                raise
            raise InferenceInterrupted(
                "The ML process exited unexpectedly. Check the worker logs and memory limit."
            ) from error
        self.failures = 0
        return result

    def close(self) -> None:
        self._close(interrupted=False)

    def _close(self, *, interrupted: bool) -> None:
        if self.channel is not None:
            self.channel.close()
            self.channel = None
        if self.process is not None:
            self.process.join(timeout=0 if interrupted else 0.25)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=0.25)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()
            self.process.close()
            self.process = None
