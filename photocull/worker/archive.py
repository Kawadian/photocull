from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import date
from pathlib import Path
from threading import Lock, get_native_id

from photocull.archive import (
    VIDEO,
    ArchiveError,
    ArchiveRequest,
    asset_query,
    child_stat,
    packed,
    permitted,
    preview,
    sidecar_state,
    source_write_roots,
    validate_capability,
)
from photocull.db import connect, write
from photocull.db.archive import audit, insert_job, materialize
from photocull.db.archive_preview import build as build_preview
from photocull.db.archive_preview import prune as prune_previews
from photocull.db.archive_rules import schedule as schedule_rules
from photocull.media.walk import MediaJail, OutsideLibrary, open_directory, source_key
from photocull.worker import archive_fs as fs
from photocull.worker import archive_selftest
from photocull.worker.archive_media import Tools, convert, probe_video, video_stream
from photocull.worker.archive_media import duration as video_duration
from photocull.worker.location import strip_copy

LOG = logging.getLogger(__name__)
CONVERSIONS = VIDEO | {"dng", "jpeg", "heic", "avif"}


def lower_thread_priority():
    tid = get_native_id()
    os.setpriority(os.PRIO_PROCESS, tid, max(10, os.getpriority(os.PRIO_PROCESS, tid)))


def prepare_preview(service):
    lower_thread_priority()
    build_preview(service.db_path, service.settings, service.library, stop=service.stop)


class StalePlan(ArchiveError):
    pass


class Paused(Exception):
    pass


class Cancelled(Exception):
    pass


class ArchiveWork:
    def __init__(self, settings, library, stop, gate=None):
        self.settings, self.library, self.stop = settings, library, stop
        self.gate = gate if gate is not None else Lock()
        self.config = settings.archive
        self.db_path = settings.library_state(library) / "index.db"
        self.source_roots = self.config.write_roots
        self.export = self.config.export_roots.get(library)
        self.trash = self.config.trash_roots.get(library)
        self.next_maintenance = 0
        self.next_permissions = 0

    def root(self, path, *, export=False):
        return permitted(path, (self.export,) if export and self.export else self.source_roots)

    def check_read(self, db, item, plan, request):
        try:
            self.check_selection(db, item, plan, request)
        except (FileNotFoundError, NotADirectoryError, OutsideLibrary) as error:
            raise StalePlan(str(error)) from error

    def check_selection(self, db, item, plan, request):
        if item["asset_id"] is None:
            row = db.execute(
                "SELECT status FROM archive_trash WHERE id = ?", (item["trash_id"],)
            ).fetchone()
            if row is None or row["status"] != "available":
                raise StalePlan("Trash entry changed")
            return
        jail = MediaJail(self.settings.libraries[self.library], self.settings.state_dir)
        with jail.open_media(item["relative"]) as (fd, actual):
            if (
                str(actual) != plan[0]["source"]["path"]
                or source_key(os.fstat(fd)) != plan[0]["source"]["key"]
            ):
                raise StalePlan("Indexed source changed. Preview again.")
        row = db.execute(*asset_query(request, asset_id=item["asset_id"])).fetchone()
        if not row or row["revision"] != item["revision"]:
            raise StalePlan("Asset rating or selection changed. Preview again.")
        if not request.strip_location or request.destructive:
            files, shared = sidecar_state(actual)
            if files != item["sidecars"] or (shared and not item["shared"]):
                raise StalePlan("Sidecars or file pairing changed. Preview again.")

    def trash_root(self, source, device):
        if self.trash:
            return self.trash
        root = self.root(source)
        folder = source.parent
        while folder != root and folder.parent.stat().st_dev == device:
            folder = folder.parent
        return folder / ".photocull-trash"

    def verify_write_mount(self, db, path):
        root = self.root(path)
        row = db.execute(
            "SELECT result FROM archive_write_checks WHERE path=?", (str(root),)
        ).fetchone()
        report = json.loads(row["result"]) if row else {}
        if not report.get("passed"):
            raise ArchiveError(f"Write root has no passing worker filesystem self-test: {root}")
        with open_directory(root) as directory:
            if (
                not archive_selftest.writable(directory)
                or archive_selftest.mount_identity(directory) != report["identity"]
            ):
                raise ArchiveError(f"Write root changed or became read-only: {root}")
        mount = archive_selftest.nearest_mount(path.parent, root)
        if not archive_selftest.same_mount(mount, report["identity"]):
            raise ArchiveError(
                f"Configure and test the nested mount as a write root: {path.parent}"
            )

    def staging(self, path, ticket, *, create=False, export=False):
        root = self.root(path, export=export)
        folder = path.parent / f".photocull-stage-{ticket}"
        if create:
            with fs.directories(folder.parent, root), open_directory(folder.parent) as parent:
                try:
                    os.mkdir(folder.name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            with open_directory(folder) as fd:
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ArchiveError(f"Unsafe staging directory: {folder}")
        return folder

    def paths(self, item, request, ticket):
        """Derive all private temporary paths using the confirmed layout."""
        for i, file in enumerate(item["files"]):
            record = dict(file)
            source, target = Path(file["source"]["path"]), file["target"]
            if target:
                target = Path(target)
                stage = self.staging(target, ticket, export=request.exports)
                record["staged"] = str(stage / f"out-{i}{target.suffix}")
            if file["remove"]:
                record["quarantine"] = str(self.staging(source, ticket) / f"old-{i}")
            if undo := file.get("undo"):
                folder = self.staging(Path(undo["path"]), ticket)
                record["undo_quarantine"] = str(folder / f"undo-{i}")
            yield record

    def cleanup(self, plan, request):
        stages = {}
        for record in plan:
            for key in ("quarantine", "undo_quarantine", "staged"):
                if record.get(key):
                    stage = Path(record[key]).parent
                    stages[stage] = self.root(stage, export=(key == "staged" and request.exports))
        for folder, root in stages.items():
            fs.clean_stage(folder, root)

    def prepare(self, db, item, request, ticket, key, pulse):
        plan = list(self.paths(item, request, ticket))
        self.check_read(db, item, plan, request)
        self.cleanup(plan, request)
        try:
            for i, record in enumerate(plan):
                old = record["source"]
                source = Path(old["path"])
                if record["remove"]:
                    self.verify_write_mount(db, source)
                try:
                    record["source"] = original = old | fs.fingerprint(source, pulse, old)
                except (FileNotFoundError, ArchiveError) as error:
                    raise StalePlan(str(error)) from error
                if record["remove"]:
                    self.staging(source, ticket, create=True)
                if undo := record.get("undo"):
                    path = Path(undo["path"])
                    self.verify_write_mount(db, path)
                    try:
                        record["undo"] = fs.fingerprint(path, pulse, undo)
                    except (FileNotFoundError, ArchiveError) as error:
                        raise StalePlan(str(error)) from error
                    self.staging(path, ticket, create=True)
                if record["remove"] and request.operation not in {"restore", "purge", "undo"}:
                    root = self.trash_root(source, os.stat(source).st_dev)
                    path = root / ticket / f"{i}{source.suffix.lower()}"
                    self.verify_write_mount(db, path)
                    with fs.directories(path.parent, self.root(path), mode=0o700):
                        pass
                    if child_stat(path):
                        raise ArchiveError(f"Trash path already exists: {path}")
                    record["backup"] = str(path)
                if not record["target"]:
                    continue
                target = Path(record["target"])
                if not request.exports:
                    self.verify_write_mount(db, target)
                self.staging(target, ticket, create=True, export=request.exports)
                if target != source and child_stat(target):
                    raise StalePlan(f"Destination appeared after preview: {target}")
                staged = Path(record["staged"])
                if i == 0 and request.strip_location and request.operation in {"copy", "rename"}:
                    snapshot = staged.with_name(f"input{source.suffix}")
                    fs.copy_file(source, snapshot, original, pulse)
                    with open_directory(staged.parent) as directory:
                        pinned = Path(f"/proc/self/fd/{directory}")
                        strip_copy(
                            Tools(self.config, pulse, pass_fds=(directory,)),
                            pinned / snapshot.name,
                            pinned / staged.name,
                        )
                    snapshot.unlink()
                    fs.sync_result(staged, original)
                    record["output"] = fs.fingerprint(staged, pulse)
                elif i == 0 and request.operation in CONVERSIONS:
                    db.execute(
                        "UPDATE archive_items SET phase='encoding and verification' "
                        "WHERE job_id=? AND ordinal=?",
                        key,
                    )
                    # Use an immutable private snapshot with the original extension for format
                    # detection, and verify source identity again before committing.
                    snapshot = staged.with_name(f"input{source.suffix}")
                    fs.copy_file(source, snapshot, original, pulse)
                    with open_directory(staged.parent) as directory:
                        # Pin the private directory across path lookups in external codecs.
                        pinned = Path(f"/proc/self/fd/{directory}")
                        convert(
                            Tools(self.config, pulse, pass_fds=(directory,)),
                            pinned / snapshot.name,
                            pinned / staged.name,
                            request,
                        )
                    fs.sync_result(staged, original)
                    record["output"] = fs.fingerprint(staged, pulse)
                    if record["output"]["size"] >= original["size"]:
                        raise ArchiveError("Verified output is no smaller. Original retained.")
                    snapshot.unlink()
                else:
                    fs.copy_file(source, staged, original, pulse)
                    record["output"] = fs.fingerprint(staged, pulse, original | {"key": None})
                with open_directory(staged.parent) as fd:
                    os.fsync(fd)
            # Check exact preview names and source identities after decoding, before intent.
            self.check_read(db, item, list(self.paths(item, request, ticket)), request)
            for record in plan:
                for file in (record["source"], record.get("undo")):
                    if file and (
                        not (info := child_stat(Path(file["path"])))
                        or source_key(info) != file["key"]
                    ):
                        raise StalePlan(f"Source changed during preparation: {file['path']}")
            pulse()
        except BaseException:
            self.cleanup(plan, request)
            raise
        return plan

    def commit(self, db, plan, request, pulse):
        # Recheck live mount identity before any durable filesystem action or journal replay.
        for record in plan:
            paths = [record.get("backup"), record["source"]["path"] if record["remove"] else None]
            if not request.exports:
                paths.append(record["target"])
            if undo := record.get("undo"):
                paths.append(undo["path"])
            for value in paths:
                if value:
                    self.verify_write_mount(db, Path(value))
        # All stages use the same durable intent. Replay accepts already-completed effects
        # only when their filesystem identities and verified content agree with the intent.
        for record in plan:
            if record.get("backup"):
                source = record["source"]
                backup = Path(record["backup"])
                fs.backup(
                    Path(source["path"]),
                    backup,
                    source,
                    self.root(backup),
                    pulse,
                    move=request.operation == "trash",
                )
        for record in plan:
            if record["target"]:
                target = Path(record["target"])
                original = record["source"]
                self.root(target, export=request.exports)
                fs.publish(
                    Path(record["staged"]),
                    target,
                    record["output"],
                    original if str(target) == original["path"] else None,
                    pulse,
                )
        for record in plan:
            original = record["source"]
            if record["remove"] and original["path"] != record["target"]:
                path = Path(original["path"])
                self.root(path)
                trash = child_stat(Path(record["backup"])) if record.get("backup") else None
                # rename directly to same-device trash already removed the name. Do not
                # touch a newly imported file that subsequently occupies that name.
                if not (
                    request.operation == "trash"
                    and trash
                    and original["key"].split(":")[:2] == [str(trash.st_dev), str(trash.st_ino)]
                ):
                    fs.remove(path, Path(record["quarantine"]), original, pulse)
            if undo := record.get("undo"):
                path = Path(undo["path"])
                self.root(path)
                fs.remove(path, Path(record["undo_quarantine"]), undo, pulse)
        self.cleanup(plan, request)
        if request.operation in {"restore", "purge", "undo"}:
            directories = {
                Path(record["source"]["path"]).parent
                for record in plan
                if record.get("source_ticket")
            }
            for directory in directories:
                with suppress(FileNotFoundError), open_directory(directory.parent) as parent:
                    fs.remove_empty(directory.name, parent)

    def finish(self, db, row, item, plan, request, ticket):
        backups, renamed = [], {}
        details = []
        for record in plan:
            source = record["source"]
            output = record.get("output")
            target = output | {"path": record["target"]} if output else None
            details.append(
                {
                    "before": source,
                    "after": target,
                    "trash": record.get("backup"),
                    "undo_removed": record.get("undo"),
                }
            )
            if record.get("backup"):
                backups.append(
                    source
                    | {
                        "path": record["backup"],
                        "original": source["path"],
                        "qualified": source.get("qualified", False),
                    }
                )
            if request.operation == "rename" and request.destructive and target:
                renamed[source["path"]] = target | {"backed_up": bool(record.get("backup"))}
        args = (row["job_id"], row["ordinal"], row["actor"])
        with write(db):
            if backups:
                db.execute(
                    "INSERT INTO archive_trash(id,job_id,created_at,reason,files,renamed_to)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        ticket,
                        row["job_id"],
                        time.time(),
                        request.reason or request.operation,
                        packed(backups),
                        packed(renamed) if renamed else None,
                    ),
                )
            if item["trash_id"]:
                db.execute(
                    "UPDATE archive_trash SET status = ? WHERE id = ?",
                    ("purged" if request.operation == "purge" else "restored", item["trash_id"]),
                )
            relink = self.reindex_entry(db, item, plan, request)
            if relink:
                updated = db.execute(
                    "UPDATE archive_reindex SET ready=1 "
                    "WHERE path=? AND asset_id=? AND after_hash=?",
                    (relink[0], relink[2], relink[4]),
                )
                if not updated.rowcount:
                    # Journals from before catalog relinking can finish after an upgrade.
                    db.execute(
                        "INSERT INTO archive_reindex VALUES(?,?,?,?,?,1) "
                        "ON CONFLICT(path) DO NOTHING",
                        relink,
                    )
            audit(db, *args, "completed", {"files": details})
            db.execute(
                "UPDATE archive_items SET status = 'done', journal = NULL, phase = NULL "
                "WHERE job_id = ? AND ordinal = ?",
                args[:2],
            )

    def reindex_entry(self, db, item, plan, request):
        if not plan[0]["target"] or (request.mode != "overwrite" and request.operation != "undo"):
            return None
        base = self.settings.libraries[self.library].root.resolve()
        target = str(Path(plan[0]["target"]).relative_to(base))
        source_path, asset_id = item["relative"], item["asset_id"]
        before = plan[0]["source"]["sha256"]
        if request.operation == "undo":
            undo = plan[0]["undo"]
            source_path, before = str(Path(undo["path"]).relative_to(base)), undo["sha256"]
            source = db.execute(
                "SELECT id FROM assets WHERE path=? AND content_hash=?", (source_path, before)
            ).fetchone()
            if source is None:
                return None
            asset_id = source["id"]
        return target, source_path, asset_id, before, plan[0]["output"]["sha256"]

    @contextmanager
    def publish_gate(self, pulse):
        while not self.gate.acquire(timeout=0.2):
            pulse()
        try:
            pulse()
            yield
        finally:
            self.gate.release()

    def save_intent(self, db, item, request, plan, args):
        with write(db):
            if db.execute("SELECT cancelled FROM archive_jobs WHERE id=?", args[:1]).fetchone()[0]:
                raise Cancelled
            db.execute(
                "UPDATE archive_items SET journal=?,phase='publishing' "
                "WHERE job_id=? AND ordinal=?",
                (packed(plan), *args[:2]),
            )
            if relink := self.reindex_entry(db, item, plan, request):
                inserted = db.execute(
                    "INSERT INTO archive_reindex(path,source_path,asset_id,before_hash,after_hash) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(path) DO NOTHING RETURNING path",
                    relink,
                ).fetchone()
                if inserted is None:
                    raise ArchiveError("Another archive operation owns this catalog path.")
            audit(db, *args, "prepared", {"files": plan})

    def process(self, row):
        lower_thread_priority()
        request = ArchiveRequest.model_validate_json(row["request"])
        item = json.loads(row["plan"])
        args = (row["job_id"], row["ordinal"], row["actor"])
        ticket = f"{row['job_id']}-{row['ordinal']}"
        plan = json.loads(row["journal"]) if row["journal"] else None
        with connect(self.db_path) as db:
            db.execute("PRAGMA synchronous = FULL")
            last = 0.0

            def pause():
                if self.stop.is_set():
                    raise Paused

            def pulse():
                nonlocal last
                pause()
                if time.monotonic() - last >= 0.2:
                    last = time.monotonic()
                    if db.execute(
                        "SELECT cancelled FROM archive_jobs WHERE id = ?", args[:1]
                    ).fetchone()[0]:
                        raise Cancelled

            try:
                validate_capability(self.settings, self.library, request)
                if plan is None:
                    if self.stop.is_set():
                        raise Paused
                    if row["cancelled"]:
                        self.cleanup(list(self.paths(item, request, ticket)), request)
                        raise Cancelled
                    if row["attempts"] > 3:
                        self.cleanup(list(self.paths(item, request, ticket)), request)
                        raise ArchiveError(
                            "Worker interrupted this item three times. "
                            "Inspect logs and preview again."
                        )
                    prepared = self.prepare(db, item, request, ticket, args[:2], pulse)
                    try:
                        with self.publish_gate(pulse):
                            self.check_read(db, item, item["files"], request)
                            self.save_intent(db, item, request, prepared, args)
                            plan = prepared
                            self.commit(db, plan, request, pause)
                            self.finish(db, row, item, plan, request, ticket)
                    finally:
                        if plan is None:
                            self.cleanup(prepared, request)
                else:
                    with self.publish_gate(pause):
                        self.commit(db, plan, request, pause)
                        self.finish(db, row, item, plan, request, ticket)
            except Paused:
                return
            except (ValueError, Cancelled, OSError, RuntimeError, KeyError, TypeError) as error:
                status = "error"
                if plan:
                    status = "recovery"
                elif isinstance(error, (Cancelled, StalePlan)):
                    status = "cancelled" if isinstance(error, Cancelled) else "skipped"
                detail = str(error) or status
                with write(db):
                    db.execute(
                        "UPDATE archive_items SET status = ?, error = ?, phase = NULL "
                        "WHERE job_id = ? AND ordinal = ?",
                        (status, detail, *args[:2]),
                    )
                    audit(db, *args, status, {"path": item["relative"], "error": detail})
                if status != "cancelled":
                    LOG.warning(
                        "Archive %s item %s (%s): %s", row["job_id"], row["ordinal"], status, detail
                    )

    def recover(self):
        with connect(self.db_path) as db, write(db):
            db.execute(
                "UPDATE archive_items SET status = 'pending' WHERE status = 'running'",
            )

    def claim(self, db, limit):
        materialize(db)
        sql = """
            SELECT i.*,j.actor,j.request,j.cancelled FROM archive_items i
            JOIN archive_jobs j ON j.id=i.job_id
            WHERE i.status='pending' AND j.finished_at IS NULL
            ORDER BY i.journal IS NULL,j.created_at,i.ordinal LIMIT ?
        """
        if not db.execute(sql, (1,)).fetchone():
            return []
        with write(db):
            rows = db.execute(sql, (limit,)).fetchall()
            db.executemany(
                "UPDATE archive_items SET status='running',attempts=attempts+1, "
                "phase=CASE WHEN journal IS NULL THEN 'preparing' ELSE 'publishing' END "
                "WHERE job_id=? AND ordinal=?",
                [(r["job_id"], r["ordinal"]) for r in rows],
            )
        return rows

    def check_filesystems(self):
        lower_thread_priority()
        with connect(self.db_path) as db:
            self.update_permissions(db)
            prune_previews(db)

    def update_permissions(self, db):
        now = time.time()
        if now < self.next_permissions:
            return
        self.next_permissions = now + 30
        roots = set(source_write_roots(self.settings, self.library))
        if self.trash:
            roots.add(self.root(self.trash))
        paths = roots | ({self.export} if self.export else set())
        values, checks = [], []
        for path in paths:
            writable, identity = False, None
            try:
                with open_directory(path) as fd:
                    writable = archive_selftest.writable(fd)
                    identity = archive_selftest.mount_identity(fd)
            except (OSError, ArchiveError):
                pass
            values.append((str(path), writable, now))
            if path in roots:
                row = db.execute(
                    "SELECT result FROM archive_write_checks WHERE path=?", (str(path),)
                ).fetchone()
                report = json.loads(row["result"]) if row else {}
                if report.get("identity") != identity or report.get("writable") != writable:
                    report = archive_selftest.describe(self.settings, self.library, path)
                    checks.append((str(path), packed(report), now))
        with write(db):
            db.execute("DELETE FROM archive_permissions")
            db.executemany("INSERT INTO archive_permissions VALUES(?,?,?)", values)
            db.executemany("INSERT OR REPLACE INTO archive_write_checks VALUES(?,?,?)", checks)

    def maintenance(self, db):
        now = time.time()
        if now < self.next_maintenance:
            return
        self.next_maintenance = now + 3600
        days = self.config.retention_days
        if not days or not self.source_roots:
            return
        rows = db.execute(
            "SELECT id FROM archive_trash WHERE status='available' AND created_at < ? "
            "ORDER BY created_at LIMIT 100",
            (now - days * 86400,),
        ).fetchall()
        if rows:
            request = ArchiveRequest(
                operation="purge",
                trash_ids=tuple(row["id"] for row in rows),
                reason=f"Automatic {days}-day retention",
            )
            try:
                result = preview(db, self.settings, self.library, request)
                if result["count"]:
                    insert_job(db, request, result, "retention")
                    self.next_maintenance = now + 60
            except (ArchiveError, OSError) as error:
                LOG.warning("Archive retention for %s: %s", self.library, error)

    def settle(self, db):
        row = db.execute(
            "SELECT * FROM archive_jobs WHERE finished_at IS NULL "
            "AND (manifest_remaining=0 OR cancelled=1) AND NOT EXISTS ("
            "SELECT 1 FROM archive_items WHERE job_id=archive_jobs.id "
            "AND status IN ('pending','running'))"
        ).fetchone()
        if row:
            request = ArchiveRequest.model_validate_json(row["request"])
            with write(db):
                db.execute(
                    "UPDATE archive_jobs SET finished_at=?,preview_id=NULL WHERE id=?",
                    (time.time(), row["id"]),
                )
                audit(db, row["id"], 0, row["actor"], "finished", {})
                if request.destructive and request.operation != "purge":
                    db.execute(
                        "INSERT OR IGNORE INTO scan_requests(status,created_at) "
                        "VALUES('pending',?)",
                        (time.time(),),
                    )

    def video_metadata(self, row):
        lower_thread_priority()
        codec, rate, width, height, duration, captured_at, make, model = (
            "",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        try:
            jail = MediaJail(self.settings.libraries[self.library], self.settings.state_dir)
            with jail.open_media(row["path"]) as (fd, _):
                if source_key(os.fstat(fd)) != row["source_key"]:
                    raise ArchiveError("Source changed since scan")

                def pulse():
                    if self.stop.is_set():
                        raise Paused

                data = probe_video(
                    Tools(self.config, pulse), f"/proc/self/fd/{fd}", pass_fds=(fd,), timeout=30
                )
                stream = next(s for s in data["streams"] if video_stream(s))
                codec = stream["codec_name"]
                rate = int(data["format"].get("bit_rate", 0)) or None
                width, height = stream.get("width"), stream.get("height")
                duration = video_duration(data)
                tags = {k.casefold(): v for k, v in data["format"].get("tags", {}).items()}
                captured_at = tags.get("creation_time") or tags.get(
                    "com.apple.quicktime.creationdate"
                )
                make = tags.get("com.apple.quicktime.make") or tags.get("make")
                model = tags.get("com.apple.quicktime.model") or tags.get("model")
                if captured_at:
                    captured_at = date.fromisoformat(captured_at[:10]).isoformat()
        except Paused:
            return
        except (OSError, ValueError, StopIteration, KeyError, TypeError):
            captured_at = None
        with connect(self.db_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO archive_video VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["content_hash"],
                    codec,
                    rate,
                    width,
                    height,
                    duration,
                    captured_at,
                    make,
                    model,
                ),
            )


class Archives:
    def __init__(self, settings, stop, stack, gates):
        self.pool = stack.enter_context(
            ThreadPoolExecutor(max_workers=settings.archive.parallelism)
        )
        self.libraries = [
            ArchiveWork(settings, name, stop, gates[name]) for name in sorted(settings.libraries)
        ]
        self.pending = set()
        self.limit = settings.archive.parallelism
        self.cursor = 0
        self.active = None
        self.next_step = 0.0
        for worker in self.libraries:
            worker.recover()
            with connect(worker.db_path) as db:
                db.execute("DELETE FROM archive_write_checks")

    def step(self, idle):
        now = time.monotonic()
        if now < self.next_step:
            return
        self.next_step = now + 0.25
        for future in tuple(self.pending):
            if future.done():
                self.pending.remove(future)
                future.result()
        if not self.pending:
            self.active = None
        available = self.limit - len(self.pending)
        if not available or not idle:
            return
        for i in range(len(self.libraries)):
            index = (i + self.cursor) % len(self.libraries)
            service = self.libraries[index]
            if self.active is not None and self.active is not service:
                continue
            if not self.pending and time.time() >= service.next_permissions:
                self.active = service
                self.pending.add(self.pool.submit(service.check_filesystems))
                self.cursor = (index + 1) % len(self.libraries)
                return
            with connect(service.db_path) as db:
                service.settle(db)
                if not db.execute(
                    "SELECT 1 FROM archive_jobs WHERE finished_at IS NULL"
                ).fetchone():
                    service.maintenance(db)
                if not self.pending:
                    schedule_rules(db, service.settings, service.library)
                if (
                    not self.pending
                    and db.execute(
                        "SELECT 1 FROM archive_previews WHERE status IN ('pending','running')"
                    ).fetchone()
                ):
                    self.active = service
                    self.pending.add(self.pool.submit(prepare_preview, service))
                    self.cursor = (index + 1) % len(self.libraries)
                    return
                rows = service.claim(db, available)
                if rows:
                    self.active = service
                    self.pending.update(self.pool.submit(service.process, row) for row in rows)
                    self.cursor = (index + 1) % len(self.libraries)
                    return
        for service in self.libraries:
            with connect(service.db_path) as db:
                row = db.execute(
                    "SELECT a.* FROM assets a LEFT JOIN archive_video v ON v.asset_id=a.id "
                    "AND v.content_hash=a.content_hash WHERE a.active=1 "
                    "AND a.kind='video' AND v.asset_id IS NULL "
                    "ORDER BY a.id LIMIT 1"
                ).fetchone()
            if row and not self.pending:
                self.active = service
                self.pending.add(self.pool.submit(service.video_metadata, row))
                return
