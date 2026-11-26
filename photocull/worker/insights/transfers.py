from __future__ import annotations

import base64
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
from photocull.db.transfers import ExportRequest, ReportRequest, artifact, select_assets
from photocull.media.cache import cache_path
from photocull.media.walk import MediaJail, SourceChanged, open_directory, source_key

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


def file_name(asset, *, sidecar=False):
    # A stable ID keeps names unique. Portable filenames also make ZIP extraction safe.
    name = Path(asset["path"]).name
    stem, extension = os.path.splitext(name)
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).rstrip(". ")
    while len(stem.encode()) > 180:
        stem = stem[:-1]
    return f"{asset['id']}-{stem or 'media'}{'.xmp' if sidecar else extension.lower()}"


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
        if not self.settings.insights.export and not self.settings.insights.report:
            return
        rows = self.db.execute(
            "SELECT * FROM transfers WHERE status IN ('running','cancelling')"
        ).fetchall()
        for job in rows:
            try:
                if job["kind"] == "copy":
                    config = ExportRequest.model_validate_json(job["request"])
                    with copy_destination(self.settings, config.destination) as root_fd:
                        # Publication can succeed before the corresponding DB commit.
                        try:
                            info = os.stat(job["id"], dir_fd=root_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            remove_private(root_fd, f".{job['id']}.part")
                        else:
                            if stat.S_ISDIR(info.st_mode):
                                self.finish(job["id"], "done")
                                continue
                            raise ValueError("Copy destination has a conflicting entry")
                else:
                    target = artifact(self.state, job["id"], job["kind"])
                    if target.is_file():
                        self.finish(job["id"], "done")
                        continue
                    target.with_suffix(".part").unlink(missing_ok=True)
                if job["status"] == "cancelling":
                    self.finish(job["id"], "cancelled")
                elif job["attempts"] >= 3:
                    self.finish(job["id"], "error", "Worker interrupted this transfer three times")
                else:
                    self.db.execute(
                        "UPDATE transfers SET status='pending' WHERE id=?", (job["id"],)
                    )
            except (OSError, ValueError) as error:
                self.finish(job["id"], "error", str(error))

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
        condition = """status='pending' AND (
            (kind IN ('zip','copy') AND ?)
            OR (kind='report' AND ? AND NOT EXISTS (SELECT 1 FROM story_dirty))
        )"""
        row = self.db.execute(
            f"SELECT id FROM transfers WHERE {condition} ORDER BY created_at, id LIMIT 1",
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
        job_id = job["id"]
        target = artifact(self.state, job_id, job["kind"])
        target.parent.mkdir(mode=0o700, exist_ok=True)
        staged = target.with_suffix(".part")
        try:
            with staged.open("xb") as handle:
                yield handle
            # Coordinate publication and cancellation with the API's short transaction.
            self.publish(job_id, pulse, lambda: staged.replace(target))
        finally:
            staged.unlink(missing_ok=True)

    def publish(self, job_id, pulse, publication):
        pulse()
        with write(self.db):
            if (
                self.db.execute("SELECT status FROM transfers WHERE id=?", (job_id,)).fetchone()[0]
                == "cancelling"
            ):
                raise Cancelled
            publication()
            self.db.execute(
                "UPDATE transfers SET status='done', finished_at=? WHERE id=?",
                (time.time(), job_id),
            )
            mark_catalog_updated(self.db)

    def export(self, job, pulse):
        job_id = job["id"]
        config = ExportRequest.model_validate_json(job["request"])
        keys = public_keys(self.db, self.settings)
        if config.selection.person_id is not None and "faces" not in keys:
            raise ValueError("Face grouping is disabled")
        if config.selection.dead is not None and "video" not in keys:
            raise ValueError("Video analysis is disabled")
        query, arguments = select_assets(config.selection, keys)
        # A temporary on-disk plan takes a consistent selection snapshot. Later ratings
        # are independent, and the source descriptor is checked for each copied file.
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("DROP TABLE IF EXISTS transfer_plan")
        self.db.execute(f"CREATE TEMP TABLE transfer_plan AS {query}", arguments)
        try:
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
            finally:
                os.close(fd)

            def publish():
                # The allowlist folder is operator-controlled. A unique name never
                # replaces a pre-existing user entry.
                try:
                    os.stat(job_id, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.rename(staged, job_id, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                else:
                    raise ValueError("Copy destination already exists")

            self.publish(job_id, pulse, publish)
        finally:
            remove_private(root_fd, staged)

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

        for asset in self.db.execute("SELECT * FROM transfer_plan ORDER BY id"):
            self.check(job_id, pulse)
            with jail.open_media(asset["path"]) as (fd, _):
                if source_key(os.fstat(fd)) != asset["source_key"]:
                    raise SourceChanged(f"{asset['path']} changed since indexing; rescan and retry")
                transfer(fd, file_name(asset), count_bytes=True)
            if config.xmp:
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
                        self.report_segment(output, item)
                        used_geonames |= item["title"] is not None
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

    def report_segment(self, output, item):
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
        for asset in item["representatives"]:
            path = cache_path(self.state, "thumbs", asset["id"], asset["content_hash"])
            try:
                with path.open("rb") as source:
                    image = source.read(512 * 1024 + 1)
            except FileNotFoundError:
                continue
            if len(image) > 512 * 1024:
                continue
            label = escape(Path(asset["path"]).name)
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
