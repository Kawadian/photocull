from __future__ import annotations

import errno
import os
import stat
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from photocull.archive import ArchiveError, permitted
from photocull.media.walk import DIRECTORY_FLAGS, open_directory
from photocull.worker.archive_fs import RENAME_EXCHANGE, rename


def mount_identity(fd):
    info = os.fstat(fd)
    with Path(f"/proc/self/fdinfo/{fd}").open() as fdinfo:
        for line in fdinfo:
            if line.startswith("mnt_id:"):
                return {
                    "device": info.st_dev,
                    "mount_id": int(line.split()[1]),
                    "inode": info.st_ino,
                }
    raise ArchiveError("Linux could not identify the mount for the open directory.")


def nearest_mount(path, root):
    while True:
        try:
            with open_directory(path) as fd:
                return mount_identity(fd)
        except FileNotFoundError:
            if path == root:
                raise
            path = path.parent


def same_mount(first, second):
    return all(first[field] == second[field] for field in ("device", "mount_id"))


def writable(directory):
    return not (os.fstatvfs(directory).f_flag & os.ST_RDONLY) and os.access(
        ".",
        os.W_OK | os.X_OK,
        dir_fd=directory,
        effective_ids=True,
    )


def run_probe(path: Path):
    result = {
        "path": str(path),
        "writable": False,
        "passed": False,
        "empty_bytes": None,
        "identity": None,
        "checks": [],
        "error": None,
    }
    try:
        with open_directory(path) as parent:
            result["identity"] = mount_identity(parent)
            stats = os.fstatvfs(parent)
            result["empty_bytes"] = stats.f_bavail * stats.f_frsize
            result["writable"] = writable(parent)
            if not result["writable"]:
                raise ArchiveError("Directory is read-only or access is denied")
            result["checks"].append("permissions")
            if result["empty_bytes"] < 8192:
                raise ArchiveError("Fewer than 8192 bytes available for the probe")
            result["checks"].append("free space")
            name = f".photocull-stage-selftest-{uuid4().hex}"
            os.mkdir(name, mode=0o700, dir_fd=parent)
            try:
                probe = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
                try:
                    check_renames(probe, result)
                finally:
                    try:
                        for child in ("a", "b", "published"):
                            with suppress(FileNotFoundError):
                                os.unlink(child, dir_fd=probe)
                        os.fsync(probe)
                    finally:
                        os.close(probe)
            finally:
                os.rmdir(name, dir_fd=parent)
                os.fsync(parent)
            result["checks"].append("parent fsync")
            result["passed"] = True
    except (OSError, ValueError) as error:
        result["error"] = str(error)
    return result


def check_renames(directory, result):
    for name, value in (("a", b"photocull-a"), ("b", b"photocull-b")):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(name, flags, 0o600, dir_fd=directory), "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    result["checks"].append("file fsync")
    try:
        rename(directory, "a", directory, "b")
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise
    else:
        raise ArchiveError("No-clobber rename accepted an existing name")
    rename(directory, "a", directory, "published")
    result["checks"].append("no-clobber rename")
    rename(directory, "published", directory, "b", RENAME_EXCHANGE)
    for name, expected in (("b", b"photocull-a"), ("published", b"photocull-b")):
        with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory), "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode) or file.read(64) != expected:
                raise ArchiveError("Atomic exchange did not preserve the two probe files")
    result["checks"].append("atomic exchange")
    os.fsync(directory)
    result["checks"].append("directory fsync")


def describe(settings, library, path):
    roots = settings.archive.write_roots
    root = path if path in roots else permitted(path, roots)
    result = run_probe(path)
    trash_path = settings.archive.trash_roots.get(library, root / ".photocull-trash")
    result["trash"] = str(trash_path)
    result["trash_same_mount"] = None
    if result["identity"]:
        try:
            trash_mount = nearest_mount(trash_path, permitted(trash_path, roots))
            result["trash_same_mount"] = same_mount(result["identity"], trash_mount)
        except (OSError, ValueError) as error:
            result["trash_error"] = str(error)
    return result
