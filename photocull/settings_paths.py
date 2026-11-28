from __future__ import annotations

import heapq
import os
import re
import stat
from collections.abc import Collection
from contextlib import contextmanager
from pathlib import Path

from photocull.config import Settings
from photocull.settings_store import SettingsError, Snapshot

PATH_FIELDS = {
    "libraries": "libraries",
    "state_dir": "write-directory",
    "static_dir": "directory",
    "token_file": "file",
    "model_dir": "write-directory",
    "derivatives.hot_dir": "write-directory",
    "derivatives.large_dir": "write-directory",
    "insights.story_geonames": "file",
    "insights.export_roots": "write-map",
    "archive.export_roots": "write-map",
    "archive.trash_roots": "directory-map",
    "archive.write_roots": "directories",
}
CACHES = {"model_dir", "derivatives.hot_dir", "derivatives.large_dir"}


def media_roots(settings: Settings) -> set[Path]:
    return {root.resolve() for library in settings.libraries.values() for root in library.roots}


def _overlaps(path: Path, areas: set[Path]) -> bool:
    return any(path.is_relative_to(area) or area.is_relative_to(path) for area in areas)


class PathBrowser:
    def __init__(self, running: Settings, baseline: Settings, configured: str, protected: Path):
        self.explicit = self._roots(Path(value) for value in configured.split(os.pathsep) if value)
        self.protected = {
            protected,
            *(cfg.token_file.resolve() for cfg in (running, baseline) if cfg.token_file),
        }
        self.media = media_roots(running) | media_roots(baseline)
        self.roots = self._roots(
            {
                *self.explicit,
                *self.media,
                *(
                    path.resolve()
                    for cfg in (running, baseline)
                    for path in (
                        cfg.model_cache,
                        cfg.derivative_root("hot"),
                        cfg.derivative_root("large"),
                    )
                    if not _overlaps(path.resolve(), self.media)
                ),
            }
        )

    @staticmethod
    def _roots(paths) -> tuple[Path, ...]:
        return tuple(
            sorted(
                {
                    canonical
                    for path in paths
                    if path.is_absolute()
                    and (canonical := path.resolve()) != Path("/")
                    and canonical.is_dir()
                }
            )
        )

    def available(self, field: str) -> tuple[Path, ...]:
        if field not in PATH_FIELDS:
            raise SettingsError("選択できない項目です。")
        if field == "token_file":
            # Authentication must never read a file from a library mount.
            return tuple(root for root in self.explicit if not _overlaps(root, self.media))
        return self.roots

    def _lexical(self, raw: str) -> Path:
        path = Path(raw)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or raw.startswith("//")
            or re.search(r"%[0-9a-f]{2}|[\\\x00-\x1f\x7f]", raw, re.I)
        ):
            raise SettingsError("許可ルート内の絶対パスを指定してください。")
        return path

    @contextmanager
    def _open(self, field: str, raw: str, *, directory: bool = False):
        path = self._lexical(raw)
        try:
            for root in self.available(field):
                if not path.is_relative_to(root):
                    continue
                target = path.resolve(strict=True)
                if not target.is_relative_to(root):
                    continue
                if any(target.is_relative_to(area) for area in self.protected) and (
                    field != "token_file" or target not in self.protected or target.is_dir()
                ):
                    continue
                # Open each canonical component via its parent. A concurrent
                # rename cannot swap a checked directory for an escaping symlink.
                flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
                fd = os.open("/", flags)
                try:
                    parts = target.parts[1:]
                    for index, name in enumerate(parts):
                        next_flags = flags
                        if index == len(parts) - 1 and not directory:
                            next_flags = os.O_PATH | os.O_NOFOLLOW
                        child = os.open(name, next_flags, dir_fd=fd)
                        os.close(fd)
                        fd = child
                    if not stat.S_ISLNK(os.fstat(fd).st_mode):
                        yield root, target, fd
                        return
                finally:
                    os.close(fd)
        except (OSError, ValueError, RuntimeError):
            pass
        raise SettingsError("許可範囲内に存在するアクセス可能なパスを指定してください。")

    def inspect(self, field: str, raw: str) -> dict:
        mode = PATH_FIELDS.get(field)
        if not mode:
            raise SettingsError("選択できない項目です。")
        with self._open(field, raw, directory=mode != "file") as (_, target, fd):
            kind = os.fstat(fd).st_mode
            if mode == "file" and not stat.S_ISREG(kind):
                raise SettingsError("通常のファイルを指定してください。")
            proc = f"/proc/self/fd/{fd}"
            required = os.R_OK | (os.X_OK if mode != "file" else 0)
            if not os.access(proc, required, effective_ids=True):
                raise SettingsError("指定されたパスに読み取り権限がありません。")
            if mode.startswith("write") and (
                not os.access(proc, os.W_OK, effective_ids=True)
                or os.fstatvfs(fd).f_flag & os.ST_RDONLY
            ):
                raise SettingsError("指定されたディレクトリに書き込み権限がありません。")
            info = {"path": str(target)}
            if field in CACHES:
                space = os.fstatvfs(fd)
                info["free_bytes"] = space.f_bavail * space.f_frsize
            return info

    def list(self, field: str, raw: str, after: str) -> dict:
        roots = self.available(field)
        if not raw:
            return {"roots": [str(root) for root in roots], "entries": [], "crumbs": []}
        with self._open(field, raw, directory=True) as (root, target, fd):
            # Sort a bounded page without retaining an entire large library.
            with os.scandir(fd) as entries:
                names = heapq.nsmallest(
                    201, (entry.name for entry in entries if entry.name > after)
                )
            children = []
            for name in names[:200]:
                child = target / name
                try:
                    with self._open(field, str(child)) as (_, _, child_fd):
                        kind = os.fstat(child_fd).st_mode
                        if stat.S_ISDIR(kind) or (
                            PATH_FIELDS[field] == "file" and stat.S_ISREG(kind)
                        ):
                            children.append(
                                {"name": name, "path": str(child), "directory": stat.S_ISDIR(kind)}
                            )
                except SettingsError:
                    continue
            crumbs = [{"name": str(root), "path": str(root)}]
            cursor = root
            for part in target.relative_to(root).parts:
                cursor /= part
                crumbs.append({"name": part, "path": str(cursor)})
            return {
                "entries": children,
                "crumbs": crumbs,
                "roots": [str(r) for r in roots],
                "next": names[199] if len(names) > 200 else None,
                "path": str(target),
            }

    def validate(
        self, before: Snapshot, after: Snapshot, submitted: Collection[str] = ()
    ) -> dict[str, dict[str, str]]:
        old, new = before.values(), after.values()
        resolved: dict[str, dict[str, str]] = {}
        media = self.media | media_roots(after.settings)
        if (
            after.settings.token_file
            and any(after.settings.token_file.resolve().is_relative_to(area) for area in media)
            and after.settings.token_file != before.settings.token_file
        ):
            raise SettingsError("管理者トークンのファイルはライブラリの外に指定してください。")

        def paths(value, mode: str) -> set:
            if mode == "libraries":
                return {
                    str(path)
                    for lib in value.values()
                    for path in [lib["root"], *lib["allowed_roots"]]
                }
            if mode.endswith("-map"):
                return set(value.values())
            if mode == "directories":
                return set(value)
            return {value} if value else set()

        for field, mode in PATH_FIELDS.items():
            if old[field] == new[field] and field not in submitted:
                continue
            old_paths = paths(old[field], mode)
            for path in paths(after.overrides.get(field, new[field]), mode) - old_paths:
                try:
                    checked = self.inspect(field, path)
                    canonical = Path(checked["path"])
                    resolved.setdefault(field, {})[path] = str(canonical)
                    if (mode.startswith("write") or field == "static_dir") and _overlaps(
                        canonical, media
                    ):
                        raise SettingsError("書き込み先はライブラリの外に指定してください。")
                except SettingsError as exc:
                    raise SettingsError(f"{field} · {exc}") from None

        # Implicit cache paths also change when state_dir changes.
        for old_root, new_root in (
            (before.settings.state_dir, after.settings.state_dir),
            (before.settings.model_cache, after.settings.model_cache),
            *(
                (before.settings.derivative_root(tier), after.settings.derivative_root(tier))
                for tier in ("hot", "large")
            ),
        ):
            if new_root != old_root and _overlaps(new_root.resolve(), media):
                raise SettingsError(
                    "データやキャッシュの保存先はライブラリの外に指定してください。"
                )
        return resolved
