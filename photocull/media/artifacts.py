from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager, suppress
from pathlib import Path

from photocull.media.walk import open_directory

TRANSFER_ID = re.compile(r"[a-f0-9]{32}")


def artifact_name(job: str, kind: str) -> str:
    if not TRANSFER_ID.fullmatch(job):
        raise ValueError("Invalid transfer ID")
    match kind:
        case "report":
            return f"{job}.html"
        case "zip":
            return f"{job}.zip"
        case "copy":
            return job
        case _:
            raise ValueError("Invalid transfer kind")


@contextmanager
def transfer_directory(state: Path, *, create: bool = False):
    # Resolve the configured state root and open the generated subdirectory without
    # following links. All file operations are relative to the pinned directory.
    with open_directory(state.resolve(strict=True)) as parent:
        if create:
            with suppress(FileExistsError):
                os.mkdir("transfers", mode=0o700, dir_fd=parent)
        fd = os.open("transfers", os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            yield fd
        finally:
            os.close(fd)


@contextmanager
def open_regular(parent: int, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Expected a regular file")
        yield fd, info
    finally:
        os.close(fd)
