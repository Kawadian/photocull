"""Apply Linux per-process limits before executing an archive tool."""

from __future__ import annotations

import ctypes
import os
import resource
import shutil
import signal
import sys


def main():
    parent, memory_mb, threads = map(int, sys.argv[1:4])
    command = sys.argv[4:]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise SystemExit("Could not attach archive subprocess to its worker")
    cpus = sorted(os.sched_getaffinity(0))[:threads]
    os.sched_setaffinity(0, cpus)
    os.nice(10)
    limit = memory_mb * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    if ionice := shutil.which("ionice"):
        command = [ionice, "-c", "3", "-t", *command]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
