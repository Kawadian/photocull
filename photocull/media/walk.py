from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from photocull.config import Library

EXCLUDED = {"edit", ".git", "__pycache__", "node_modules", ".photocull", ".photocull-trash"}
EXTENSIONS = {
    **dict.fromkeys(("jpg", "jpeg", "png", "webp", "tif", "tiff"), "image"),
    **dict.fromkeys(("heic", "heif", "hif", "avif"), "heif"),
    **dict.fromkeys(("dng", "arw", "cr2", "cr3", "nef", "raf", "orf", "rw2", "pef"), "raw"),
    **dict.fromkeys(("mp4", "mov", "m4v", "mkv"), "video"),
}
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
PROCESS_ERRORS = {errno.EBADF, errno.EMFILE, errno.ENFILE, errno.ENOMEM}


class OutsideLibrary(ValueError):
    pass


class SourceChanged(OSError):
    pass


@dataclass(frozen=True)
class ScanIssue:
    path: str
    kind: str
    operation: str
    code: str
    errno: int | None
    source_key: str | None = None

    @classmethod
    def from_error(
        cls, path: str, kind: str, operation: str, error: OSError, *, key: str | None = None
    ) -> ScanIssue:
        if error.errno in PROCESS_ERRORS:
            raise error
        if isinstance(error, SourceChanged):
            code = "changed"
        elif isinstance(error, PermissionError):
            code = "permission_denied"
        elif isinstance(error, (FileNotFoundError, NotADirectoryError)):
            code = "missing"
        else:
            code = "io_error"
        return cls(path or ".", kind, operation, code, error.errno, key)


def excluded(name: str) -> bool:
    name = name.casefold()
    return name in EXCLUDED or name.startswith(".photocull-stage-")


@dataclass(frozen=True)
class MediaEntry:
    path: str
    kind: str
    stat: os.stat_result


@contextmanager
def open_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Pin an absolute, canonical directory without following replacement symlinks."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute directory path without parent traversal")
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o755, dir_fd=fd)
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class MediaJail:
    def __init__(self, library: Library, state_dir: Path, *, validate: bool = True):
        self.root = library.root.resolve(strict=validate)
        roots = [self.root]
        for candidate in library.allowed_roots:
            try:
                roots.append(candidate.resolve(strict=validate))
            except OSError as error:
                # Check an unavailable alias at its entry without blocking the primary root.
                if validate or error.errno in PROCESS_ERRORS:
                    raise
        self.roots = tuple(roots)
        self.state_dir = state_dir.resolve()
        if validate:
            for root in self.roots:
                with open_directory(root):
                    pass

    def locate(self, path: str, *, directory: bool = False) -> Path:
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise OutsideLibrary(f"Invalid library path: {path}")
        lexical_dirs = relative.parts if directory else relative.parts[:-1]
        if any(excluded(part) for part in lexical_dirs):
            raise OutsideLibrary(f"Excluded library path: {path}")

        try:
            target = self.root.joinpath(relative).resolve(strict=True)
        except RuntimeError as error:
            raise OutsideLibrary(f"Symlink cycle: {path}") from error
        matching = [target.relative_to(root) for root in self.roots if target.is_relative_to(root)]
        if not matching or target.is_relative_to(self.state_dir):
            raise OutsideLibrary(f"Path is outside permitted media directories: {path}")
        for location in matching:
            target_dirs = location.parts if directory else location.parts[:-1]
            if any(excluded(part) for part in target_dirs):
                raise OutsideLibrary(f"Excluded target directory: {path}")
        return target

    @contextmanager
    def open_media(self, path: str) -> Iterator[tuple[int, Path]]:
        target = self.locate(path)
        with open_directory(target.parent) as parent:
            fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OutsideLibrary(f"Expected a regular media file: {path}")
                yield fd, target
            finally:
                os.close(fd)

    def walk(
        self, pulse: Callable[[], None], on_error: Callable[[ScanIssue], None] | None = None
    ) -> Iterator[MediaEntry]:
        pending = [("", frozenset())]
        while pending:
            path, ancestors = pending.pop()
            pulse()
            try:
                target = self.locate(path, directory=True)
                with open_directory(target) as fd, os.scandir(fd) as listing:
                    info = os.fstat(fd)
                    identity = (info.st_dev, info.st_ino)
                    if identity in ancestors:
                        continue
                    entries = sorted(listing, key=lambda entry: (entry.name.casefold(), entry.name))
                    descendants = ancestors | {identity}
                    folders = []
                    for entry in entries:
                        pulse()
                        name = entry.name
                        # SQLite and the browser use UTF-8. Skip undecodable byte filenames.
                        if any(0xD800 <= ord(char) <= 0xDFFF for char in name):
                            continue
                        child = f"{path}/{name}" if path else name
                        kind = EXTENSIONS.get(Path(name).suffix[1:].casefold())
                        is_link = False
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if not excluded(name):
                                    folders.append((child, descendants))
                                continue
                            is_link = entry.is_symlink()
                            if is_link:
                                resolved = self.locate(child)
                                with open_directory(resolved.parent) as parent:
                                    info = os.stat(
                                        resolved.name, dir_fd=parent, follow_symlinks=False
                                    )
                            elif kind and not (target / name).is_relative_to(self.state_dir):
                                info = entry.stat(follow_symlinks=False)
                            else:
                                continue
                        except OutsideLibrary:
                            # Skip links outside the jail and symlink cycles.
                            continue
                        except OSError as error:
                            if (
                                isinstance(error, (FileNotFoundError, NotADirectoryError))
                                and is_link
                            ):
                                continue  # Broken aliases are intentionally excluded.
                            issue = ScanIssue.from_error(
                                child, "file" if kind else "directory", "stat", error
                            )
                            if on_error:
                                on_error(issue)
                            continue
                        if stat.S_ISDIR(info.st_mode) and not excluded(name):
                            folders.append((child, descendants))
                        elif kind and stat.S_ISREG(info.st_mode):
                            yield MediaEntry(child, kind, info)
            except OutsideLibrary:
                if not path:
                    raise
                continue
            except OSError as error:
                issue = ScanIssue.from_error(path, "directory", "list", error)
                if on_error:
                    on_error(issue)
                if not path:
                    raise
                continue
            pending.extend(reversed(folders))


def hash_file(
    jail: MediaJail,
    entry: MediaEntry,
    pulse: Callable[[], None],
    *,
    buffer: memoryview | None = None,
) -> str:
    if buffer is None:
        buffer = memoryview(bytearray(4 * 1024 * 1024))
    digest = hashlib.sha256()
    with jail.open_media(entry.path) as (fd, _):
        before = os.fstat(fd)
        key = source_key(before)
        if key != source_key(entry.stat):
            raise SourceChanged(f"Media changed before hashing: {entry.path}")
        if hasattr(os, "posix_fadvise"):
            with suppress(OSError):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        with os.fdopen(fd, "rb", buffering=0, closefd=False) as source:
            while count := source.readinto(buffer):
                pulse()
                digest.update(buffer[:count])
        if source_key(os.fstat(fd)) != key:
            raise SourceChanged(f"Media changed during scan: {entry.path}")
        with jail.open_media(entry.path) as (current, _):
            if source_key(os.fstat(current)) != key:
                raise SourceChanged(f"Media was replaced during scan: {entry.path}")
    return digest.hexdigest()


def source_key(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}:{info.st_mtime_ns}:{info.st_size}"
