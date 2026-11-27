from __future__ import annotations

import base64
import ctypes
import errno
import os
import re
import shutil
import stat
import time
import zipfile
from contextlib import ExitStack, contextmanager, suppress
from html import escape
from pathlib import Path

from photocull.db import write
from photocull.db.catalog import mark_catalog_updated
from photocull.db.insights import public_keys
from photocull.db.stories import segments
from photocull.db.transfers import ExportRequest, ReportRequest, select_assets
from photocull.media.artifacts import artifact_name, open_regular, transfer_directory
from photocull.media.cache import cache_path
from photocull.media.walk import MediaJail, SourceChanged, open_directory, source_key
from photocull.worker.location import clean_copy

COPY_BYTES = 1024 * 1024
REPORT_HEADER = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Photocull trip report</title><style>
body{font:1rem/1.5 system-ui;max-width:70rem;margin:auto;padding:1rem;
 background:#f8f6f0;color:#282622}
h1,h2,h3{line-height:1.15} .details{color:#594e44}
.gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,14rem),1fr));gap:.75rem}
figure{margin:0}img{width:100%;display:block;border-radius:.3rem}figcaption{overflow-wrap:anywhere}
article{border-top:1px solid #c8c0b7;margin-block:1rem;padding-bottom:1rem}
</style><main><h1>Trip report</h1>
"""


class Cancelled(Exception):
    pass


RENAME_NOREPLACE = 1


def rename_output(parent: int, staged: str, name: str) -> None:
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "Atomic transfer publication is unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(staged), parent, os.fsencode(name), RENAME_NOREPLACE):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), name)


def identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def file_name(asset, *, sidecar=False):
    # A stable ID keeps names unique. Portable filenames also make ZIP extraction safe.
    name = Path(asset["path"]).name
    stem, extension = os.path.splitext(name)
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).rstrip(". ")
    stem = stem.encode()[:180].decode(errors="ignore")
    # Preserve supported camera/file extensions. Never copy separators or
    # control characters from a filesystem name into an archive member.
    suffix = extension.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", extension) else ""
    return f"{asset['id']}-{stem or 'media'}{'.xmp' if sidecar else suffix}"


@contextmanager
def copy_destination(settings, name):
    target = settings.insights.export_roots.get(name)
    if target is None:
        raise ValueError("Export destination is no longer configured")
    area = Path(os.path.abspath(target))
    for source in (
        settings.state_dir,
        *(root for library in settings.libraries.values() for root in library.roots),
    ):
        source = source.resolve()
        if area.is_relative_to(source) or source.is_relative_to(area):
            raise ValueError("Export destination overlaps a media or state directory")
    with open_directory(area) as root_fd:
        yield root_fd


def remove_private(root_fd, name):
    with suppress(FileNotFoundError):
        shutil.rmtree(name, dir_fd=root_fd)


@contextmanager
def sidecar(jail, path):
    parent = Path(path).parent
    stem = Path(path).stem
    with ExitStack() as stack:
        for extension in (".xmp", ".XMP"):
            candidate = (parent / f"{stem}{extension}").as_posix()
            try:
                fd, _ = stack.enter_context(jail.open_media(candidate))
            except FileNotFoundError:
                continue
            yield fd
            return
        yield None


class Transfers:
    def __init__(self, worker):
        self.worker = worker
        self.db = worker.db
        self.state = worker.state
        self.settings = worker.settings

    def recover(self):
        for staged in self.state.glob(".keeper-copy-*"):
            # TemporaryDirectory uses private worker-owned directories and removes them normally.
            # A stopped process can leave its render behind before publication.
            if staged.is_dir() and not staged.is_symlink():
                shutil.rmtree(staged)
        for job in self.db.execute(
            "SELECT * FROM transfers WHERE status IN ('running','cancelling')"
        ).fetchall():
            try:
                name = artifact_name(job["id"], job["kind"])
                if self.recover_output(job, name):
                    self.finish(job["id"], "done")
                    continue
                # A cancel request may arrive from the API during recovery. Decide
                # its outcome and reset the attempt in the same write transaction.
                with write(self.db):
                    self.db.execute(
                        """UPDATE transfers SET output_device = NULL, output_inode = NULL,
                           status = CASE WHEN status='cancelling' THEN 'cancelled'
                                         WHEN attempts>=3 THEN 'error' ELSE 'pending' END,
                           finished_at = CASE WHEN status='cancelling' OR attempts>=3 THEN ? END,
                           error = CASE WHEN status='running' AND attempts>=3
                                        THEN 'Worker interrupted this transfer three times' END
                           WHERE id = ?""",
                        (time.time(), job["id"]),
                    )
                    mark_catalog_updated(self.db)
            except (OSError, ValueError) as error:
                self.finish(job["id"], "error", str(error))

    def recover_output(self, job, name):
        if job["kind"] == "copy":
            config = ExportRequest.model_validate_json(job["request"])
            with copy_destination(self.settings, config.destination) as parent:
                return self.check_output(parent, name, job, directory=True)
        try:
            with transfer_directory(self.state) as parent:
                return self.check_output(parent, name, job)
        except FileNotFoundError:
            return False

    def check_output(self, parent, name, job, *, directory=False):
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        staged = f".{job['id']}.part"
        if directory:
            remove_private(parent, staged)
        else:
            with suppress(FileNotFoundError):
                os.unlink(staged, dir_fd=parent)
            # Staging files from the initial Phase 4 implementation.
            with suppress(FileNotFoundError):
                os.unlink(f"{job['id']}.part", dir_fd=parent)
        if info is None:
            return False
        file_type = stat.S_ISDIR if directory else stat.S_ISREG
        if file_type(info.st_mode) and identity(info) == (
            job["output_device"],
            job["output_inode"],
        ):
            return True
        raise ValueError("Transfer destination already has a conflicting entry")

    def finish(self, job_id, status, error=None):
        with write(self.db):
            self.db.execute(
                "UPDATE transfers SET status=?, error=?, finished_at=? WHERE id=?",
                (status, error, time.time(), job_id),
            )
            mark_catalog_updated(self.db)

    def step(self, pulse):
        config = self.settings.insights
        if not (config.export or config.report):
            return False
        args = (config.export, config.report and config.story)
        row = self.db.execute(
            """SELECT id FROM transfers WHERE status='pending' AND (
                (kind IN ('zip','copy') AND ?)
                OR (kind='report' AND ? AND NOT EXISTS (SELECT 1 FROM story_dirty))
            ) ORDER BY created_at, id LIMIT 1""",
            args,
        ).fetchone()
        if row is None:
            return False
        with write(self.db):
            job = self.db.execute(
                """UPDATE transfers SET status='running', attempts=attempts+1, processed=0,
                   total=0, written_bytes=0, total_bytes=0 WHERE id=? AND status='pending'
                   RETURNING *""",
                (row[0],),
            ).fetchone()
        if job is None:
            return True
        try:
            if job["kind"] == "report":
                self.report(job, pulse)
            else:
                self.export(job, pulse)
        except Cancelled:
            self.finish(job["id"], "cancelled")
        except (OSError, ValueError, RuntimeError, zipfile.LargeZipFile) as error:
            self.finish(job["id"], "error", str(error))
        return True

    def check(self, job_id, pulse):
        pulse()
        if (
            self.db.execute("SELECT status FROM transfers WHERE id=?", (job_id,)).fetchone()[0]
            == "cancelling"
        ):
            raise Cancelled

    @contextmanager
    def output(self, job, pulse):
        name = artifact_name(job["id"], job["kind"])
        staged = f".{job['id']}.part"
        with transfer_directory(self.state, create=True) as parent:
            fd = os.open(
                staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    yield handle
                    handle.flush()
                    os.fsync(handle.fileno())
                    info = os.fstat(handle.fileno())
                self.publish(job["id"], pulse, parent, staged, name, info)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(staged, dir_fd=parent)

    def publish(self, job_id, pulse, parent, staged, name, info):
        self.check(job_id, pulse)
        # This short SQLite update is the cancellation boundary. The filesystem
        # operation can block on an external destination without holding the DB lock.
        if not self.db.execute(
            """UPDATE transfers SET output_device=?, output_inode=?
               WHERE id=? AND status='running' AND output_inode IS NULL""",
            (*identity(info), job_id),
        ).rowcount:
            raise Cancelled
        if identity(os.stat(staged, dir_fd=parent, follow_symlinks=False)) != identity(info):
            raise OSError("Staged output was replaced")
        rename_output(parent, staged, name)
        if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(info):
            raise OSError("The destination changed during publication")
        self.finish(job_id, "done")

    def export(self, job, pulse):
        job_id = job["id"]
        config = ExportRequest.model_validate_json(job["request"])
        keys = public_keys(self.db, self.settings)
        if config.selection.person_id is not None and "faces" not in keys:
            raise ValueError("Face grouping is disabled")
        if config.selection.dead is not None and "video" not in keys:
            raise ValueError("Video analysis is disabled")
        query, arguments = select_assets(self.db, config.selection, keys, self.settings.insights)
        # A temporary on-disk plan takes a consistent selection snapshot. Later ratings
        # are independent, and the source descriptor is checked for each copied file.
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("DROP TABLE IF EXISTS transfer_plan")
        self.db.execute(f"CREATE TEMP TABLE transfer_plan AS {query}", arguments)
        try:
            self.db.execute("CREATE UNIQUE INDEX temp.transfer_plan_id ON transfer_plan(id)")
            count, total = self.db.execute(
                "SELECT count(*), coalesce(sum(size),0) FROM transfer_plan"
            ).fetchone()
            self.db.execute(
                "UPDATE transfers SET total=?, total_bytes=? WHERE id=?", (count, total, job_id)
            )
            if config.kind == "copy":
                with copy_destination(self.settings, config.destination) as root_fd:
                    self.copy_to(root_fd, config, job_id, pulse)
            else:
                with (
                    self.output(job, pulse) as handle,
                    zipfile.ZipFile(
                        handle,
                        mode="w",
                        compression=zipfile.ZIP_STORED,
                        allowZip64=True,
                    ) as archive,
                ):
                    self.copy_files(
                        config,
                        job_id,
                        pulse,
                        lambda name: archive.open(name, "w", force_zip64=True),
                    )
        finally:
            self.db.execute("DROP TABLE IF EXISTS transfer_plan")

    def copy_to(self, root_fd, config, job_id, pulse):
        name = artifact_name(job_id, "copy")
        staged = f".{job_id}.part"
        os.mkdir(staged, mode=0o700, dir_fd=root_fd)
        try:
            fd = os.open(staged, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:

                def open_output(name):
                    return os.fdopen(
                        os.open(
                            name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=fd,
                        ),
                        "wb",
                    )

                self.copy_files(config, job_id, pulse, open_output)
                self.publish(job_id, pulse, root_fd, staged, name, os.fstat(fd))
            finally:
                os.close(fd)
        finally:
            remove_private(root_fd, staged)

    def planned_assets(self):
        after = 0
        # Finish each SELECT before copying or writing progress. A cursor held across
        # encoding can retain a WAL snapshot that SQLite cannot upgrade to a writer.
        while assets := self.db.execute(
            "SELECT * FROM transfer_plan WHERE id > ? ORDER BY id LIMIT 64", (after,)
        ).fetchall():
            yield from assets
            after = assets[-1]["id"]

    def copy_files(self, config, job_id, pulse, output):
        jail = MediaJail(self.worker.library, self.settings.state_dir)
        written = 0
        processed = 0
        updated = 0.0

        def transfer(fd, name, *, count_bytes):
            nonlocal written, updated
            before = source_key(os.fstat(fd))
            with output(name) as target, os.fdopen(fd, "rb", closefd=False) as source:
                while chunk := source.read(COPY_BYTES):
                    self.check(job_id, pulse)
                    target.write(chunk)
                    if count_bytes:
                        written += len(chunk)
                    if time.monotonic() - updated >= 0.4:
                        self.db.execute(
                            "UPDATE transfers SET written_bytes=?, processed=? WHERE id=?",
                            (written, processed, job_id),
                        )
                        updated = time.monotonic()
            if before != source_key(os.fstat(fd)):
                raise SourceChanged(f"Source changed while exporting {name}")

        for asset in self.planned_assets():
            self.check(job_id, pulse)
            with jail.open_media(asset["path"]) as (fd, _):
                if source_key(os.fstat(fd)) != asset["source_key"]:
                    raise SourceChanged(f"{asset['path']} changed since indexing; rescan and retry")
                if config.include_location is False:
                    with (
                        clean_copy(
                            self.settings,
                            fd,
                            Path(asset["path"]).suffix,
                            self.state,
                            lambda: self.check(job_id, pulse),
                        ) as staged,
                        staged.open("rb") as clean,
                    ):
                        transfer(clean.fileno(), file_name(asset), count_bytes=True)
                    if source_key(os.fstat(fd)) != asset["source_key"]:
                        raise SourceChanged(f"{asset['path']} changed while exporting")
                else:
                    transfer(fd, file_name(asset), count_bytes=True)
                with jail.open_media(asset["path"]) as (current, _):
                    if source_key(os.fstat(current)) != asset["source_key"]:
                        raise SourceChanged(f"{asset['path']} was replaced while exporting")
            if config.xmp and config.include_location is not False:
                with sidecar(jail, asset["path"]) as fd:
                    if fd is not None:
                        transfer(fd, file_name(asset, sidecar=True), count_bytes=False)
            processed += 1
        self.db.execute(
            "UPDATE transfers SET processed=?, written_bytes=? WHERE id=?",
            (processed, written, job_id),
        )

    def report(self, job, pulse):
        job_id = job["id"]
        request = ReportRequest.model_validate_json(job["request"])
        include_location = self.settings.browser.includes_location(request.include_location)
        args = (
            request.start.isoformat() if request.start else "",
            request.end.isoformat() if request.end else "9999-99-99",
        )
        condition = (
            "FROM story_days AS d JOIN story_state AS s USING(key) WHERE d.day BETWEEN ? AND ?"
        )
        self.db.execute(
            "UPDATE transfers SET total=? WHERE id=?",
            (
                self.db.execute(f"SELECT count(*) {condition}", args).fetchone()[0],
                job_id,
            ),
        )
        with self.output(job, pulse) as output:
            output.write(REPORT_HEADER.encode())
            days = self.db.execute(f"SELECT day {condition} ORDER BY day", args)
            count = 0
            used_geonames = False
            for (day,) in days:
                self.check(job_id, pulse)
                output.write(f"<section><h2>{escape(day)}</h2>".encode())
                position = 0
                while items := segments(self.db, day, position, 16):
                    for item in items:
                        self.check(job_id, pulse)
                        self.report_segment(output, item, include_location)
                        used_geonames |= include_location and item["title"] is not None
                        position = item["position"]
                output.write(b"</section>\n")
                count += 1
                self.db.execute(
                    "UPDATE transfers SET processed=?, written_bytes=? WHERE id=?",
                    (count, output.tell(), job_id),
                )
            if not count:
                output.write(b"<p>No indexed stories in this date range.</p>")
            if used_geonames:
                output.write(
                    b"<footer>Place names from GeoNames (geonames.org), CC BY 4.0.</footer>"
                )
            output.write(b"</main>\n")

    def report_segment(self, output, item, include_location):
        title = f"Part {item['position']}"
        if include_location:
            title = item["title"] or (
                f"{item['latitude']:.3f}, {item['longitude']:.3f}"
                if item["latitude"] is not None
                else "Location unavailable"
            )
        time_range = " – ".join(value[11:16] for value in (item["started"], item["ended"]) if value)
        count = item["count"]
        output.write(
            f'<article><h3>{escape(title)}</h3><p class="details">'
            f'{escape(time_range)} · {count} items</p><div class="gallery">'.encode()
        )
        for index, asset in enumerate(item["representatives"], 1):
            path = cache_path(self.state, "thumbs", asset["id"], asset["content_hash"])
            try:
                with (
                    open_directory(self.state.resolve() / "thumbs") as thumbnails,
                    open_regular(thumbnails, path.name) as (fd, _),
                    os.fdopen(fd, "rb", closefd=False) as source,
                ):
                    image = source.read(512 * 1024 + 1)
            except OSError:
                continue
            if len(image) > 512 * 1024:
                continue
            label = escape(Path(asset["path"]).name) if include_location else f"Photo {index}"
            output.write(
                b'<figure><img loading="lazy" alt="'
                + label.encode()
                + b'" src="data:image/jpeg;base64,'
                + base64.b64encode(image)
                + b'"><figcaption>'
                + label.encode()
                + b"</figcaption></figure>"
            )
        output.write(b"</div></article>\n")
