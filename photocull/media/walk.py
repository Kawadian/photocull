from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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


class OutsideLibrary(ValueError):
    pass


class SourceChanged(OSError):
    pass


def excluded(name: str) -> bool:
    name = name.casefold()
    return name in EXCLUDED or name.startswith(".photocull-stage-")


@dataclass(frozen=True)
class MediaEntry:
    path: str
    kind: str
    stat: os.stat_result


@contextmanager
def open_directory(path: Path) -> Iterator[int]:
    """Pin an absolute, canonical directory without following replacement symlinks."""
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class MediaJail:
    def __init__(self, library: Library, state_dir: Path):
        self.roots = tuple(root.resolve(strict=True) for root in library.roots)
        self.root = self.roots[0]
        self.state_dir = state_dir.resolve()
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

        target = self.root.joinpath(relative).resolve(strict=True)
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

    def walk(self, pulse: Callable[[], None]) -> Iterator[MediaEntry]:
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
                    entries = sorted(
                        (
                            (entry.name, entry.is_dir(follow_symlinks=False), entry.is_symlink())
                            for entry in listing
                        ),
                        key=lambda entry: (entry[0].casefold(), entry[0]),
                    )
            except (OutsideLibrary, FileNotFoundError, NotADirectoryError):
                if not path:
                    raise
                continue

            descendants = ancestors | {identity}
            folders = []
            for name, is_dir, is_link in entries:
                pulse()
                # SQLite and the browser use UTF-8. Skip undecodable byte filenames.
                if any(0xD800 <= ord(char) <= 0xDFFF for char in name):
                    continue
                child = f"{path}/{name}" if path else name
                if is_dir:
                    if not excluded(name):
                        folders.append((child, descendants))
                    continue

                kind = EXTENSIONS.get(Path(name).suffix[1:].casefold())
                if not kind and not is_link:
                    continue
                try:
                    resolved = self.locate(child)
                    with open_directory(resolved.parent) as parent:
                        info = os.stat(resolved.name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and not excluded(name):
                        folders.append((child, descendants))
                    elif kind and stat.S_ISREG(info.st_mode):
                        yield MediaEntry(child, kind, info)
                except (OutsideLibrary, FileNotFoundError, NotADirectoryError, RuntimeError):
                    # Broken links, symlink loops and concurrent removals are ordinary skips.
                    continue
            pending.extend(reversed(folders))


def hash_file(jail: MediaJail, entry: MediaEntry, pulse: Callable[[], None]) -> str:
    digest = hashlib.sha256()
    with jail.open_media(entry.path) as (fd, _):
        before = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as source:
            while data := source.read(1024 * 1024):
                pulse()
                digest.update(data)
        key = source_key(before)
        if key != source_key(entry.stat) or source_key(os.fstat(fd)) != key:
            raise SourceChanged(f"Media changed during scan: {entry.path}")
        with jail.open_media(entry.path) as (current, _):
            if source_key(os.fstat(current)) != key:
                raise SourceChanged(f"Media was replaced during scan: {entry.path}")
    return digest.hexdigest()


def source_key(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}:{info.st_mtime_ns}:{info.st_size}"
