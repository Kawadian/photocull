from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager, suppress
from fnmatch import fnmatchcase
from pathlib import Path
from tempfile import NamedTemporaryFile

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
def open_regular(parent: int | None, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Expected a regular file")
        yield fd, info
    finally:
        os.close(fd)


@contextmanager
def open_artifact(path: Path):
    with open_directory(path.parent.absolute()) as parent, open_regular(parent, path.name) as file:
        yield file


@contextmanager
def stage_artifact(target: Path, prefix: str):
    # Keep the parent pinned through creation, encoding and atomic publication.
    with (
        open_directory(target.parent.absolute(), create=True) as parent,
        NamedTemporaryFile(dir=f"/proc/self/fd/{parent}", prefix=prefix, delete=False) as file,
    ):
        try:
            yield file
        finally:
            with suppress(FileNotFoundError):
                os.unlink(Path(file.name).name, dir_fd=parent)


def unlink_artifact(path: Path) -> None:
    with (
        suppress(FileNotFoundError, NotADirectoryError),
        open_directory(path.parent.absolute()) as parent,
    ):
        os.unlink(path.name, dir_fd=parent)


def prune_artifacts(directory: Path, pattern: str, *, keep: str | None = None) -> None:
    with (
        suppress(FileNotFoundError, NotADirectoryError),
        open_directory(directory.absolute()) as parent,
        os.scandir(parent) as entries,
    ):
        for entry in entries:
            if (
                entry.name != keep
                and fnmatchcase(entry.name, pattern)
                and not entry.is_dir(follow_symlinks=False)
            ):
                with suppress(FileNotFoundError):
                    os.unlink(entry.name, dir_fd=parent)
