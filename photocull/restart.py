"""Restart handoff shared by the API and the standalone systemd host helper."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

SERVICES = ("worker", "egress-proxy", "api")
MAX_AGE = 600
MAX_START_AGE = 90
COMMAND_TIMEOUT = 150
MAX_BYTES = 4096
ROOT_UID = 0
REQUEST = "request.json"
RESULT = "result.json"
EGRESS = "egress.json"
OPEN_DIRECTORY = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
ID = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"(?:base|\d{8}T\d{12}Z-[0-9a-f]{12})")


class RestartError(ValueError):
    pass


def services(values: object) -> tuple[str, ...]:
    if (
        not isinstance(values, (tuple, list))
        or not 1 <= len(values) <= len(SERVICES)
        or any(not isinstance(name, str) or name not in SERVICES for name in values)
        or len(set(values)) != len(values)
    ):
        raise RestartError("Invalid restart services")
    return tuple(name for name in SERVICES if name in values)


def service_env(value: str) -> tuple[str, ...]:
    return services([item.strip() for item in value.split(",")]) if value else ()


def matches(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class RestartRequest:
    id: str
    revision: str
    requested_at: int
    services: tuple[str, ...]
    egress: str | None

    @classmethod
    def from_json(cls, value: object) -> RestartRequest:
        if not isinstance(value, dict) or value.keys() != {
            "id",
            "revision",
            "requested_at",
            "services",
            "egress",
        }:
            raise RestartError("Invalid restart request")
        names = services(value["services"])
        if (
            not matches(ID, value["id"])
            or not matches(REVISION, value["revision"])
            or type(value["requested_at"]) is not int
            or not 0 < value["requested_at"] <= 253402300799
            or (
                not matches(SHA, value["egress"])
                if "egress-proxy" in names
                else value["egress"] is not None
            )
        ):
            raise RestartError("Invalid restart request")
        return cls(value["id"], value["revision"], value["requested_at"], names, value["egress"])


def read_record(fd: int, name: str, owner: int) -> dict | None:
    try:
        source = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(source)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or info.st_size > MAX_BYTES
        ):
            raise RestartError("Untrusted restart record")
        # The host must inspect the type before opening an API-controlled inode for I/O.
        with open(f"/proc/self/fd/{source}", "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
        try:
            value = json.loads(data)
        except (ValueError, RecursionError):
            raise RestartError("Invalid restart record") from None
    finally:
        os.close(source)
    if len(data) > MAX_BYTES or not isinstance(value, dict):
        raise RestartError("Invalid restart record")
    return value


def read_result(fd: int) -> dict | None:
    result = read_record(fd, RESULT, ROOT_UID)
    if result is None:
        return None
    try:
        request = RestartRequest.from_json(result["request"])
        status = result["status"]
        restarted = result["restarted"]
        if (
            status not in ("running", "succeeded", "failed")
            or not isinstance(restarted, list)
            or restarted != list(request.services[: len(restarted)])
            or (status == "succeeded" and len(restarted) != len(request.services))
        ):
            raise RestartError("Invalid restart result")
        validated = {"request": asdict(request), "status": status, "restarted": restarted}
        if status != "running":
            finished = result["finished_at"]
            if type(finished) is not int or not 0 < finished <= 253402300799:
                raise RestartError("Invalid restart result")
            validated["finished_at"] = finished
        return validated
    except KeyError:
        raise RestartError("Invalid restart result") from None


def write_record(fd: int, name: str, value: dict, mode: int) -> None:
    temporary = f".restart-{uuid4().hex}"
    data = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    temp_fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd
    )
    try:
        with os.fdopen(temp_fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=fd)


def root_owned(fd: int) -> bool:
    info = os.fstat(fd)
    return info.st_uid == ROOT_UID and not info.st_mode & 0o022


@contextmanager
def open_bridge(directory: Path) -> Iterator[tuple[int, int]]:
    if not directory.is_absolute() or ".." in directory.parts:
        raise RestartError("Expected an absolute restart directory")
    fd = os.open("/", OPEN_DIRECTORY)
    try:
        for part in directory.parts[1:]:
            child = os.open(part, OPEN_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        if not root_owned(fd):
            raise RestartError("Untrusted restart directory")
        incoming = os.open("requests", OPEN_DIRECTORY, dir_fd=fd)
        try:
            outgoing = os.open("results", OPEN_DIRECTORY, dir_fd=fd)
            try:
                if not root_owned(outgoing) or os.fstat(incoming).st_mode & 0o022:
                    raise RestartError("Untrusted restart directory")
                yield incoming, outgoing
            finally:
                os.close(outgoing)
        finally:
            os.close(incoming)
    finally:
        os.close(fd)


class RestartBridge:
    def __init__(self, directory: Path, allowed: str):
        self.directory = directory
        try:
            self.allowed = service_env(allowed)
        except RestartError:
            self.allowed = ()

    def inspect(self) -> tuple[tuple[str, ...], dict | None, str | None, bool]:
        if not self.allowed:
            return (), None, None, False
        try:
            with open_bridge(self.directory) as (incoming, outgoing):
                result = read_result(outgoing)
                egress = read_record(outgoing, EGRESS, ROOT_UID)
                pending = read_record(incoming, REQUEST, os.geteuid())
                if pending is not None:
                    request = RestartRequest.from_json(pending)
                    if not result or request.id != result["request"]["id"]:
                        result = {"request": asdict(request), "status": "queued", "restarted": []}
                    age = time.time() - request.requested_at
                    if (result["status"] == "queued" and age > MAX_START_AGE) or (
                        result["status"] == "running" and age > MAX_AGE
                    ):
                        result = {**result, "status": "expired"}
                return (
                    self.allowed,
                    result,
                    egress["digest"] if egress and matches(SHA, egress.get("digest")) else None,
                    pending is not None and result["status"] != "expired",
                )
        except (FileNotFoundError, PermissionError):
            return (), None, None, False

    def submit(self, revision: str, pending: tuple[str, ...], egress: str | None) -> dict:
        if not pending or not set(pending) <= set(self.allowed):
            raise RestartError("Restart targets not enabled")
        request = RestartRequest(uuid4().hex, revision, int(time.time()), services(pending), egress)
        with open_bridge(self.directory) as (incoming, _):
            if read_record(incoming, REQUEST, os.geteuid()) is not None:
                raise RestartError("Restart already queued")
            write_record(incoming, REQUEST, asdict(request), 0o600)
        return {"request": asdict(request), "status": "queued", "restarted": []}


def validate_wrapper(wrapper: Path) -> None:
    if not wrapper.is_absolute() or ".." in wrapper.parts:
        raise RestartError("Expected a trusted absolute wrapper path")
    for path in (wrapper, *wrapper.parents):
        info = path.lstat()
        if info.st_uid != ROOT_UID or info.st_mode & 0o022:
            raise RestartError("Untrusted wrapper path")
        if path == wrapper:
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                raise RestartError("Expected an executable wrapper")
        elif not stat.S_ISDIR(info.st_mode):
            raise RestartError("Untrusted wrapper path")


def run_host(directory: Path, wrapper: Path, allowed: tuple[str, ...]) -> None:
    if not allowed:
        raise RestartError("Host restart configuration is incomplete")
    validate_wrapper(wrapper)
    with open_bridge(directory) as (incoming, outgoing):
        # The lock is in the host-owned directory and survives an API restart.
        lock_fd = os.open(".lock", os.O_NOFOLLOW | os.O_RDWR | os.O_CREAT, 0o600, dir_fd=outgoing)
        with os.fdopen(lock_fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                raw = read_record(incoming, REQUEST, os.fstat(incoming).st_uid)
                if raw is None:
                    return
                request = RestartRequest.from_json(raw)
            except (RestartError, OSError):
                # Drop a malformed or substituted request without invoking the wrapper.
                os.unlink(REQUEST, dir_fd=incoming)
                return
            previous = read_result(outgoing)
            if previous and previous["request"]["id"] == request.id:
                if previous["status"] == "running":
                    write_record(
                        outgoing,
                        RESULT,
                        {
                            **previous,
                            "status": "failed",
                            "finished_at": int(time.time()),
                        },
                        0o644,
                    )
            else:
                result = {"request": asdict(request), "status": "running", "restarted": []}
                write_record(outgoing, RESULT, result, 0o644)
                try:
                    age = time.time() - request.requested_at
                    if not -5 <= age <= MAX_START_AGE or not set(request.services) <= set(allowed):
                        raise RestartError("Restart request is stale or disabled")
                    for service in request.services:
                        subprocess.run(
                            [str(wrapper), "restart", service],
                            check=True,
                            timeout=COMMAND_TIMEOUT,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        result["restarted"].append(service)
                        if service == "egress-proxy":
                            write_record(outgoing, EGRESS, {"digest": request.egress}, 0o644)
                        write_record(outgoing, RESULT, result, 0o644)
                    result["status"] = "succeeded"
                except (OSError, RestartError, subprocess.SubprocessError):
                    result["status"] = "failed"
                result["finished_at"] = int(time.time())
                write_record(outgoing, RESULT, result, 0o644)
            os.unlink(REQUEST, dir_fd=incoming)
            os.fsync(incoming)


def main() -> int:
    try:
        run_host(
            Path(os.environ["PHOTOCULL_RESTART_DIRECTORY"]),
            Path(os.environ["PHOTOCULL_RESTART_WRAPPER"]),
            service_env(os.environ["PHOTOCULL_RESTART_SERVICES"]),
        )
    except (KeyError, OSError, RestartError):
        # No deployment commands, private paths or wrapper output in the journal.
        print("Photocull restart handoff is unavailable.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
