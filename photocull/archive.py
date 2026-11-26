from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from photocull.config import Settings
from photocull.db.catalog import AUTO_REJECT_REASON
from photocull.media.walk import EXTENSIONS, MediaJail, excluded, open_directory, source_key

MAX_ITEMS = 500
ID = Annotated[int, Field(gt=0, le=2**63 - 1)]
TrashID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}-[1-9][0-9]{0,2}$")]
ShortText = Annotated[str, Field(max_length=200)]
EXTENSION = {
    "dng": ".dng",
    "jpeg": ".jpg",
    "heic": ".heic",
    "avif": ".avif",
}
VIDEO = {"hevc", "av1"}
RENAME_TOKENS = {"stem", "date", "year", "camera", "id", "n"}


class ArchiveError(ValueError):
    pass


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ids: tuple[ID, ...] = Field(default=(), max_length=MAX_ITEMS)
    after_id: ID | None = None
    start: date | None = None
    end: date | None = None
    older_days: int | None = Field(default=None, ge=1, le=73000)
    folder: str = Field(default="", max_length=4096)
    camera: ShortText = ""
    extensions: tuple[Annotated[str, Field(pattern=r"^[a-zA-Z0-9]{1,12}$")], ...] = Field(
        default=(), max_length=32
    )
    selection: Literal["all", "rejected", "suggested", "dead", "burst_rest"] = "all"
    burst_id: ID | None = None
    min_rating: int = Field(default=0, ge=0, le=5)
    max_rating: int = Field(default=5, ge=0, le=5)
    min_width: int = Field(default=0, ge=0, le=32768)
    min_height: int = Field(default=0, ge=0, le=32768)
    min_bytes: int = Field(default=0, ge=0, le=2**53)
    min_kbps: int = Field(default=0, ge=0, le=10_000_000)
    codecs: tuple[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,24}$")], ...] = Field(
        default=(), max_length=16
    )

    @model_validator(mode="after")
    def validate_range(self):
        p = PurePosixPath(self.folder)
        if p.is_absolute() or ".." in p.parts or "\0" in self.folder:
            raise ValueError("Folder must be relative to the library")
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start date is after end date")
        if self.min_rating > self.max_rating:
            raise ValueError("Minimum rating is above maximum rating")
        return self


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal[
        "copy",
        "dng",
        "jpeg",
        "heic",
        "avif",
        "hevc",
        "av1",
        "trash",
        "restore",
        "purge",
        "rename",
        "undo",
    ]
    mode: Literal["export", "overwrite", "remove"] = "export"
    filters: Filters = Field(default_factory=Filters)
    trash_ids: tuple[TrashID, ...] = Field(default=(), max_length=MAX_ITEMS)
    collision: Literal["fail", "rename"] = "fail"
    reason: ShortText = ""
    quality: int | None = Field(default=None, ge=0, le=100)
    preset: Literal["fast", "balanced", "slow"] = "balanced"
    template: ShortText = "{date}_{stem}"

    @property
    def destructive(self) -> bool:
        return self.mode != "export" or self.operation in {"trash", "restore", "purge", "undo"}

    @property
    def exports(self) -> bool:
        return self.mode in {"export", "remove"} and self.operation not in {
            "trash",
            "restore",
            "purge",
            "undo",
        }

    @model_validator(mode="after")
    def validate_options(self):
        if (self.operation in {"restore", "purge", "undo"}) != bool(self.trash_ids):
            raise ValueError("Restore, purge and undo require explicit trash IDs")
        if (
            self.operation in {"copy", "restore", "purge", "trash", "undo"}
            and self.mode != "export"
        ):
            raise ValueError("This operation uses its own destination")
        if self.operation == "rename" and self.mode == "remove":
            raise ValueError("Choose export or overwrite for rename")
        if self.quality is not None:
            maximum = 51 if self.operation == "hevc" else 63 if self.operation == "av1" else 100
            if (
                self.operation not in VIDEO | {"jpeg", "heic", "avif"}
                or not (0 if self.operation in VIDEO else 1) <= self.quality <= maximum
            ):
                raise ValueError(f"Quality out of range for {self.operation}")
        return self


def packed(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def permitted(path: Path, roots: tuple[Path, ...]) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ArchiveError(f"Invalid absolute path: {path}")
    for root in sorted(roots, key=lambda p: len(p.parts), reverse=True):
        if path != root and path.is_relative_to(root):
            return root
    raise ArchiveError(f"Path is outside archive write roots: {path}")


def child_stat(path: Path) -> os.stat_result | None:
    try:
        with open_directory(path.parent) as parent:
            return os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def descriptor(path: Path, info: os.stat_result, digest: str | None = None) -> dict:
    if not stat.S_ISREG(info.st_mode):
        raise ArchiveError(f"Expected a regular file: {path}")
    return {
        "path": str(path),
        "key": source_key(info),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": digest,
    }


def source_write_roots(settings: Settings, library: str) -> tuple[Path, ...]:
    base = settings.libraries[library].root.resolve()
    return tuple(
        root
        for root in settings.archive.write_roots
        if base.is_relative_to(root) or root.is_relative_to(base)
    )


def capabilities(db, settings, library):
    config = settings.archive
    current = {
        Path(row["path"]): bool(row["writable"])
        for row in db.execute(
            "SELECT path,writable FROM archive_permissions WHERE checked_at > unixepoch()-120"
        )
    }
    source_roots = source_write_roots(settings, library)
    export = config.export_roots.get(library)
    trash = config.trash_roots.get(library)
    reason = None
    if not source_roots:
        reason = "No PHOTOCULL_WRITE_ROOTS directory covers this library."
    elif not any(current.get(root, True) for root in source_roots):
        reason = "The worker sees this library's archive write roots as read-only or unavailable."
    elif trash and not any(
        current.get(area, True) for area in config.write_roots if trash.is_relative_to(area)
    ):
        reason = "The configured trash parent is read-only or unavailable to the worker."
    return {
        "destructive": reason is None,
        "destructive_reason": reason,
        "export": export is not None and current.get(export, True),
        "export_reason": (
            "Configure an archive export directory for this library."
            if not export
            else "The worker sees the archive export directory as read-only or missing."
            if not current.get(export, True)
            else None
        ),
        "retention_days": config.retention_days,
    }


def validate_capability(settings: Settings, library: str, request: ArchiveRequest) -> None:
    policy = settings.archive
    if request.destructive and not policy.write_roots:
        raise ArchiveError("Destructive actions disabled. Configure PHOTOCULL_WRITE_ROOTS.")
    if request.exports and library not in policy.export_roots:
        raise ArchiveError("Configure an archive export directory for this library.")


def sidecar_listing(folder: Path) -> tuple[dict, dict]:
    xmp: dict[str, list[str]] = {}
    pairs: dict[str, int] = {}
    with open_directory(folder) as fd, os.scandir(fd) as siblings:
        for entry in siblings:
            path = Path(entry.name)
            if path.suffix.casefold() == ".xmp":
                xmp.setdefault(entry.name.casefold(), []).append(entry.name)
            elif path.suffix[1:].casefold() in EXTENSIONS:
                stem = path.stem.casefold()
                pairs[stem] = pairs.get(stem, 0) + 1
    return xmp, pairs


def sidecar_state(source: Path, listing=sidecar_listing) -> tuple[list[dict], bool]:
    xmp, pairs = listing(source.parent)
    files = []
    stem, qualified = f"{source.stem}.xmp".casefold(), f"{source.name}.xmp".casefold()
    with open_directory(source.parent) as fd:
        for key in (stem, qualified):
            names = xmp.get(key, [])
            if len(names) > 1:
                raise ArchiveError("Ambiguous XMP filenames differing only in case")
            for name in names:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                file = descriptor(source.with_name(name), info)
                files.append(file | {"qualified": key == qualified})
    return files, bool(files and pairs.get(source.stem.casefold(), 0) > 1)


def related_path(destination: Path, file: dict) -> Path:
    return destination.with_name(
        f"{destination.name if file.get('qualified') else destination.stem}.xmp"
    )


def asset_query(request: ArchiveRequest, *, asset_id=None) -> tuple[str, list]:
    f = request.filters
    clauses, values = ["a.active = 1"], []
    tests = [
        ("a.id = ?", asset_id),
        ("a.id > ?", f.after_id),
        (
            "coalesce(substr(v.captured_at,1,10),a.day) >= ?",
            f.start.isoformat() if f.start else None,
        ),
        ("coalesce(substr(v.captured_at,1,10),a.day) <= ?", f.end.isoformat() if f.end else None),
        (
            "coalesce(substr(v.captured_at,1,10),a.day) <= ?",
            (date.today() - timedelta(days=f.older_days)).isoformat() if f.older_days else None,
        ),
        ("a.rating BETWEEN ? AND ?", (f.min_rating, f.max_rating)),
        ("coalesce(v.width,a.width) >= ?", f.min_width or None),
        ("coalesce(v.height,a.height) >= ?", f.min_height or None),
        ("a.size >= ?", f.min_bytes or None),
        (
            "coalesce(v.bit_rate, 8 * a.size / nullif(a.duration, 0)) >= ?",
            f.min_kbps * 1000 or None,
        ),
        (
            "instr(lower(coalesce(v.make,a.make,'') || ' ' || "
            "coalesce(v.model,a.model,'')),lower(?)) > 0",
            f.camera or None,
        ),
        ("bm.burst_id = ?", f.burst_id),
    ]
    if f.folder and (folder := f.folder.rstrip("/")) not in {"", "."}:
        tests.append(
            ("(a.folder = ? OR substr(a.folder,1,length(?) + 1) = ? || '/')", (folder,) * 3)
        )
    for test, value in tests:
        if value is not None:
            clauses.append(test)
            values.extend(value if isinstance(value, tuple) else (value,))
    for expression, entries in (("a.id", f.ids), ("lower(v.codec)", f.codecs)):
        if entries:
            clauses.append(f"{expression} IN ({','.join('?' for _ in entries)})")
            values.extend(entries)
    if f.extensions:
        clauses.append(
            "(" + " OR ".join("substr(lower(a.path),-length(?)) = ?" for _ in f.extensions) + ")"
        )
        for ext in f.extensions:
            values.extend((f".{ext.lower()}",) * 2)
    selection = {
        "all": "1",
        "rejected": "a.rejected = 1",
        "suggested": f"({AUTO_REJECT_REASON}) IS NOT NULL",
        "dead": "vf.dead = 1",
        "burst_rest": "bm.burst_id IS NOT NULL AND a.id <> coalesce("
        "(SELECT bc.asset_id FROM burst_members cb JOIN burst_choices bc "
        "ON bc.asset_id = cb.asset_id WHERE cb.burst_id = bm.burst_id "
        "ORDER BY bc.chosen_revision DESC, bc.asset_id DESC LIMIT 1), b.recommended_best_id)",
    }
    clauses.append(selection[f.selection])
    if request.operation in VIDEO:
        clauses.append("a.kind = 'video'")
    elif request.operation == "dng":
        clauses.append("a.kind = 'raw'")
    elif request.operation in {"jpeg", "heic", "avif"}:
        clauses.append("a.kind IN ('heif', 'image')")
    if request.operation == "trash" and f.selection == "all" and not f.ids:
        raise ArchiveError("Select asset IDs, rejected photos, dead clips or burst remainder.")
    sql = f"""SELECT a.*,coalesce(v.duration,a.duration) AS archive_duration,
                     coalesce(substr(v.captured_at,1,10),a.day) AS archive_day,
                     coalesce(v.model,a.model) AS archive_model FROM assets a
              LEFT JOIN archive_video v ON v.asset_id = a.id AND v.content_hash = a.content_hash
              LEFT JOIN video_flags vf ON vf.asset_id = a.id AND vf.content_hash = a.content_hash
              LEFT JOIN asset_scores s ON s.asset_id = a.id
              LEFT JOIN burst_members bm ON bm.asset_id = a.id
              LEFT JOIN bursts b ON b.id = bm.burst_id
              WHERE {" AND ".join(clauses)} ORDER BY a.id LIMIT {MAX_ITEMS + 1}"""
    return sql, values


def rename_name(template: str, row, source: Path, ordinal: int) -> str:
    from string import Formatter

    fields = {
        "stem": source.stem,
        "date": row["archive_day"],
        "year": row["archive_day"][:4],
        "camera": (row["archive_model"] or "unknown").replace("/", "_"),
        "id": row["id"],
        "n": ordinal,
    }
    try:
        for _, field, format_spec, conversion in Formatter().parse(template):
            if field is not None and (field not in RENAME_TOKENS or format_spec or conversion):
                raise ValueError
        name = template.format_map(fields).strip()
    except ValueError as error:
        raise ArchiveError(
            "Use a literal template and {stem}, {date}, {year}, {camera}, {id}, {n}."
        ) from error
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or excluded(name)
        or any(ord(c) < 32 for c in name)
        or len(os.fsencode(name + source.suffix)) > 240
    ):
        raise ArchiveError("Template produces an invalid filename")
    return name + source.suffix


def destination_for(settings, library, request, row, source, ordinal):
    if request.operation == "trash":
        return None
    suffix = EXTENSION.get(
        request.operation, source.suffix.lower() if source.suffix.lower() != ".m4v" else ".mp4"
    )
    if request.operation == "rename":
        name = rename_name(request.template, row, source, ordinal)
    elif request.operation in VIDEO or request.operation in EXTENSION:
        name = source.with_suffix(suffix).name
    else:
        name = source.name
    if request.exports:
        return settings.archive.export_roots[library] / row["folder"] / name
    return source.with_name(name)


def asset_plan(settings, library, jail, request, row, ordinal, listing):
    with jail.open_media(row["path"]) as (fd, source):
        original = descriptor(source, os.fstat(fd), row["content_hash"])
    if original["key"] != row["source_key"]:
        raise ArchiveError("Source changed since indexing. Scan again.")
    if request.destructive:
        if source != jail.root / row["path"]:
            raise ArchiveError("Destructive actions require the original's direct path")
        permitted(source, settings.archive.write_roots)
    if any(source.is_relative_to(root) for root in settings.archive.trash_roots.values()):
        raise ArchiveError("Archive trash is reserved")
    target = destination_for(settings, library, request, row, source, ordinal)
    if target and target == source and request.operation == "rename":
        raise ArchiveError("Name is unchanged")
    files = [
        {
            "source": original,
            "target": str(target) if target else None,
            "remove": request.destructive,
        }
    ]
    related, shared = sidecar_state(source, listing)
    for file in related:
        prior = Path(file["path"])
        output = related_path(target, file) if target else None
        keep_shared = shared and not file["qualified"]
        if output == prior or (output is None and keep_shared):
            continue
        files.append(
            {
                "source": file,
                "target": str(output) if output else None,
                "remove": request.destructive and not keep_shared,
            }
        )
    return {
        "asset_id": row["id"],
        "relative": row["path"],
        "files": files,
        "duration": row["archive_duration"],
        "trash_id": None,
        "revision": row["revision"],
        "sidecars": related,
        "shared": shared,
    }


def from_trash(settings, library, request, row):
    if row["status"] != "available":
        raise ArchiveError("Trash entry is unavailable")
    files = json.loads(row["files"])
    renamed = json.loads(row["renamed_to"]) if row["renamed_to"] else None
    if request.operation == "undo" and not renamed:
        raise ArchiveError("This trash entry has no rename to undo")
    if request.operation == "restore" and renamed:
        raise ArchiveError("Use Undo rename to restore the original name")
    root = settings.archive.trash_roots.get(library)
    result = []
    for backup in files:
        source, original = Path(backup["path"]), Path(backup["original"])
        permitted(source, settings.archive.write_roots)
        if not (
            (root and source.is_relative_to(root))
            or (not root and ".photocull-trash" in source.parts)
        ):
            raise ArchiveError("Trash entry is outside the configured trash location")
        if request.operation != "purge":
            permitted(original, settings.archive.write_roots)
        info = child_stat(source)
        if info is None:
            raise ArchiveError("Trash file is missing")
        if info.st_size != backup["size"] or info.st_mtime_ns != backup["mtime_ns"]:
            raise ArchiveError("Trash file size or mtime changed since it was recorded")
        entry = {
            "source": descriptor(source, info, backup["sha256"])
            | {"qualified": backup.get("qualified", False)},
            "target": str(original) if request.operation != "purge" else None,
            "remove": True,
            "source_ticket": row["id"],
        }
        if request.operation == "undo":
            replacement = renamed[backup["original"]]
            path = Path(replacement["path"])
            permitted(path, settings.archive.write_roots)
            current = child_stat(path)
            if current is None:
                raise ArchiveError("Renamed file is missing")
            entry["undo"] = descriptor(path, current, replacement["sha256"])
        result.append(entry)
    if request.operation == "undo":
        for original, replacement in renamed.items():
            if replacement["backed_up"]:
                continue
            path = Path(replacement["path"])
            permitted(path, settings.archive.write_roots)
            if (info := child_stat(path)) is None:
                raise ArchiveError(f"Renamed sidecar is missing: {original}")
            entry = descriptor(path, info, replacement["sha256"])
            result.append({"source": entry, "target": None, "remove": True})
    return {
        "asset_id": None,
        "relative": files[0]["original"],
        "files": result,
        "duration": None,
        "trash_id": row["id"],
    }


def resolve_collisions(item: dict, request: ArchiveRequest, reserved: set[Path]) -> None:
    files = item["files"]
    if not files[0]["target"]:
        return
    primary = Path(files[0]["target"])
    for i in range(1000 if request.collision == "rename" else 1):
        candidate = primary.with_stem(f"{primary.stem}_{i}") if i else primary
        targets = [
            candidate,
            *(
                related_path(candidate, file["source"]) if file["target"] else None
                for file in files[1:]
            ),
        ]
        if request.operation in {"restore", "undo"}:
            for index, file in enumerate(files[1:], 1):
                if file["target"]:
                    original = Path(file["target"])
                    if i:
                        stem = candidate.name if file["source"]["qualified"] else candidate.stem
                        original = original.with_name(stem + original.suffix)
                    targets[index] = original
        outputs = [p for p in targets if p is not None]
        if len(set(outputs)) == len(outputs) and all(
            path is None
            or path not in reserved
            and (str(path) == file["source"]["path"] or child_stat(path) is None)
            for path, file in zip(targets, files, strict=True)
        ):
            for path, file in zip(targets, files, strict=True):
                file["target"] = str(path) if path is not None else None
            reserved.update(outputs)
            return
    raise ArchiveError(f"Destination already exists: {primary}")


def estimates(request: ArchiveRequest, items: list[dict]) -> dict:
    ratio = {"dng": 0.75, "jpeg": 0.75, "heic": 0.55, "avif": 0.50, "hevc": 0.65, "av1": 0.55}
    size = sum(item["files"][0]["source"]["size"] for item in items)
    removed = sum(
        file["source"]["size"] for item in items for file in item["files"] if file["remove"]
    )
    encoded = int(size * ratio.get(request.operation, 1))
    sidecar_bytes = sum(
        file["source"]["size"] for item in items for file in item["files"][1:] if file["target"]
    )
    if request.operation in {"trash", "purge"}:
        encoded = 0
    per_item = {"dng": 3, "heic": 3, "avif": 5, "jpeg": 1}.get(request.operation, 0.15)
    seconds = sum(
        max(per_item, (item["duration"] or 0) * (2 if request.operation == "av1" else 1))
        if request.operation in VIDEO
        else per_item
        for item in items
    )
    return {
        "source_bytes": size,
        "estimated_output_bytes": encoded + sidecar_bytes,
        "estimated_reduction_bytes": max(0, size - encoded),
        "estimated_after_purge_bytes": max(0, removed - encoded - sidecar_bytes)
        if request.destructive
        else 0,
        "estimated_seconds": round(seconds + size / (60 * 1024**2), 1),
    }


def preview(db, settings: Settings, library: str, request: ArchiveRequest) -> dict:
    validate_capability(settings, library, request)
    access = capabilities(db, settings, library)
    if request.destructive and not access["destructive"]:
        raise ArchiveError(access["destructive_reason"])
    if request.exports and not access["export"]:
        raise ArchiveError(access["export_reason"])
    items, skipped, reserved, inputs = [], [], set(), set()
    if request.trash_ids:
        rows = []
        for id_ in dict.fromkeys(request.trash_ids):
            row = db.execute("SELECT * FROM archive_trash WHERE id = ?", (id_,)).fetchone()
            if row is None:
                skipped.append({"path": id_, "reason": "Unknown trash entry"})
            elif db.execute(
                "SELECT 1 FROM archive_items WHERE status='recovery' "
                "AND json_extract(plan,'$.trash_id')=? LIMIT 1",
                (id_,),
            ).fetchone():
                skipped.append(
                    {"path": id_, "reason": "Retry the blocked recovery for this entry first."}
                )
            else:
                rows.append(row)
        jail = None
    else:
        sql, params = asset_query(request)
        rows = db.execute(sql, params).fetchall()
        jail = MediaJail(settings.libraries[library], settings.state_dir) if rows else None
    listing = lru_cache(maxsize=4)(sidecar_listing)
    for ordinal, row in enumerate(rows[:MAX_ITEMS], 1):
        label = row["path"] if jail else row["id"]
        try:
            item = (
                asset_plan(settings, library, jail, request, row, ordinal, listing)
                if jail
                else from_trash(settings, library, request, row)
            )
            affected = {file["source"]["path"] for file in item["files"] if file["remove"]}
            if affected & inputs:
                raise ArchiveError("Another item already changes this source")
            resolve_collisions(item, request, reserved)
            inputs.update(affected)
            items.append(item)
        except (OSError, ValueError, RuntimeError) as error:
            skipped.append({"path": label, "reason": str(error)})
    body = request.model_dump(mode="json")
    token = hashlib.sha256(
        packed([library, settings.archive.model_dump(mode="json"), body, items]).encode()
    ).hexdigest()
    return {
        "confirmation": token,
        "items": items,
        "skipped": skipped,
        "count": len(items),
        "next_id": rows[MAX_ITEMS - 1]["id"] if len(rows) > MAX_ITEMS else None,
        **estimates(request, items),
    }
