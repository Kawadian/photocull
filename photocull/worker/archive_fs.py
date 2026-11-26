from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import stat
from contextlib import contextmanager, suppress
from pathlib import Path

from photocull.archive import ArchiveError, child_stat, descriptor, permitted
from photocull.media.walk import DIRECTORY_FLAGS, open_directory, source_key

RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = libc.renameat2
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
renameat2.restype = ctypes.c_int


def rename(source_fd, source, target_fd, target, flags=RENAME_NOREPLACE):
    if renameat2(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flags):
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), target)


@contextmanager
def directories(path: Path, root: Path, *, mode=0o755):
    with open_directory(root) as root_fd:
        current = os.dup(root_fd)
        try:
            for part in path.relative_to(root).parts:
                try:
                    child = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=mode, dir_fd=current)
                        os.fsync(current)
                    except FileExistsError:
                        pass
                    child = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
                os.close(current)
                current = child
            yield current
        finally:
            os.close(current)


@contextmanager
def read_file(path):
    with open_directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ArchiveError(f"Expected a regular file: {path}")
            yield handle


def fingerprint(path, pulse, expected=None):
    with read_file(path) as handle:
        info = os.fstat(handle.fileno())
        if expected and expected.get("key") and source_key(info) != expected["key"]:
            raise ArchiveError(f"Changed since preview: {path}")
        digest = hashlib.sha256()
        while chunk := handle.read(1024 * 1024):
            pulse()
            digest.update(chunk)
        checked = descriptor(path, info, digest.hexdigest())
        current = child_stat(path)
        if (
            not current
            or checked["key"] != source_key(current)
            or checked["key"] != source_key(os.fstat(handle.fileno()))
            or (expected and expected["sha256"] and not matches(checked, expected))
        ):
            raise ArchiveError(f"File changed or hash does not match: {path}")
        checked["mode"] = stat.S_IMODE(info.st_mode) & 0o777
        return checked


def matches(actual, expected):
    return all(actual[key] == expected[key] for key in ("sha256", "size", "mtime_ns"))


def identity(key):
    return key.split(":", 2)[:2]


def require_identity(current, expected, path):
    if identity(current["key"]) != identity(expected["key"]):
        raise ArchiveError(f"File identity changed: {path}")


def verified(path, expected, pulse, *, same_inode=False):
    try:
        actual = fingerprint(path, pulse, expected | {"key": None})
        if same_inode:
            require_identity(actual, expected, path)
        return True
    except FileNotFoundError:
        return False


def copy_file(source, target, expected, pulse):
    with read_file(source) as origin, open_directory(target.parent) as parent:
        fd = os.open(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as output:
            while chunk := origin.read(1024 * 1024):
                pulse()
                output.write(chunk)
            output.flush()
            os.utime(fd, ns=(expected["mtime_ns"], expected["mtime_ns"]))
            os.fchmod(fd, expected["mode"])
            os.fsync(fd)
        os.fsync(parent)
    fingerprint(target, pulse, expected | {"key": None})


def sync_result(path, original):
    with read_file(path) as handle:
        fd = handle.fileno()
        os.utime(fd, ns=(original["mtime_ns"], original["mtime_ns"]))
        os.fchmod(fd, original["mode"])
        os.fsync(fd)
    if child_stat(path).st_mtime_ns != original["mtime_ns"]:
        raise ArchiveError(f"Destination filesystem did not preserve mtime: {path}")


def publish(staged: Path, target: Path, expected: dict, prior: dict | None, pulse) -> None:
    # The only same-name overwrite exchanges with the verified staging file. A displaced
    # unexpected inode is exchanged back before reporting the conflict.
    info = child_stat(staged)
    target_info = child_stat(target)
    if target_info and info is None and verified(target, expected, pulse, same_inode=True):
        return
    exchanged = (
        prior
        and target_info
        and info
        and identity(source_key(target_info)) == identity(expected["key"])
    )
    with open_directory(staged.parent) as sfd, open_directory(target.parent) as tfd:
        if exchanged:
            verified(target, expected, pulse, same_inode=True)
        else:
            if not verified(staged, expected, pulse, same_inode=True):
                raise ArchiveError(f"Verified staged output is missing: {staged}")
            if prior and not verified(target, prior, pulse, same_inode=True):
                raise ArchiveError(f"Original is missing: {target}")
            flags = RENAME_EXCHANGE if prior else RENAME_NOREPLACE
            rename(sfd, staged.name, tfd, target.name, flags)
            os.fsync(sfd)
            os.fsync(tfd)
        if prior:
            try:
                if not verified(staged, prior, pulse, same_inode=True):
                    raise ArchiveError(f"Displaced original is missing: {staged}")
            except (ArchiveError, OSError):
                rename(sfd, staged.name, tfd, target.name, RENAME_EXCHANGE)
                os.fsync(sfd)
                os.fsync(tfd)
                raise


def backup(source: Path, target: Path, expected: dict, root: Path, pulse, *, move=False):
    if verified(target, expected, pulse):
        return
    if not verified(source, expected, pulse, same_inode=True):
        raise ArchiveError(f"Original is missing: {source}")
    with directories(target.parent, root, mode=0o700) as tfd, open_directory(source.parent) as sfd:
        directory = os.fstat(tfd)
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise ArchiveError(f"Trash ticket directory is not private: {target.parent}")
        try:
            if move:
                rename(sfd, source.name, tfd, target.name)
                os.fsync(sfd)
            else:
                os.link(
                    source.name, target.name, src_dir_fd=sfd, dst_dir_fd=tfd, follow_symlinks=False
                )
            os.fsync(tfd)
        except OSError as error:
            if error.errno not in {errno.EXDEV, errno.EOPNOTSUPP, errno.EMLINK, errno.EPERM}:
                raise
        else:
            try:
                if not verified(target, expected, pulse, same_inode=True):
                    raise ArchiveError(f"Trash move did not produce a file: {target}")
            except (OSError, ArchiveError):
                if move:
                    rename(tfd, target.name, sfd, source.name)
                    os.fsync(sfd)
                else:
                    os.unlink(target.name, dir_fd=tfd)
                os.fsync(tfd)
                raise
            return
        temporary = target.with_name(f".{target.name}.partial")
        with suppress(FileNotFoundError):
            os.unlink(temporary.name, dir_fd=tfd)
        copy_file(source, temporary, expected, pulse)
        rename(tfd, temporary.name, tfd, target.name)
        os.fsync(tfd)
        if not verified(target, expected, pulse):
            raise ArchiveError(f"Trash copy was not published: {target}")


def remove(path: Path, quarantine: Path, expected: dict, pulse) -> None:
    # Quarantine first so a concurrent change to the filename cannot be unlinked blindly.
    if child_stat(quarantine) is None:
        if not verified(path, expected, pulse, same_inode=True):
            return
        with open_directory(path.parent) as pfd, open_directory(quarantine.parent) as qfd:
            rename(pfd, path.name, qfd, quarantine.name)
            os.fsync(pfd)
            os.fsync(qfd)
    with open_directory(path.parent) as pfd, open_directory(quarantine.parent) as qfd:
        try:
            if not verified(quarantine, expected, pulse, same_inode=True):
                raise ArchiveError(f"Quarantined original is missing: {quarantine}")
        except (ArchiveError, OSError):
            rename(qfd, quarantine.name, pfd, path.name)
            os.fsync(qfd)
            os.fsync(pfd)
            raise
        os.unlink(quarantine.name, dir_fd=qfd)
        os.fsync(qfd)


def clean_stage(path: Path, root: Path):
    permitted(path, (root,))
    try:
        with open_directory(path.parent) as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise ArchiveError(f"Unexpected staging directory: {path}")
            shutil.rmtree(path.name, dir_fd=parent)
            os.fsync(parent)
    except FileNotFoundError:
        pass


def remove_empty(name, parent):
    try:
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)
    except OSError as error:
        if error.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST}:
            raise
