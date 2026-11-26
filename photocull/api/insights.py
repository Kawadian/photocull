from __future__ import annotations

import json
from contextlib import ExitStack
from html import escape
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi import Path as RoutePath
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, StringConstraints

from photocull.config import Settings
from photocull.db import connect, write
from photocull.db.catalog import mark_catalog_updated
from photocull.db.insights import public_keys, queue, stage_key
from photocull.db.stories import days, segments
from photocull.db.transfers import ExportRequest, ReportRequest, cancel, enqueue, public
from photocull.insights import taste_input
from photocull.media.artifacts import artifact_name, open_regular, transfer_directory
from photocull.media.cache import video_path

MAX_ID = 2**63 - 1
Id = Annotated[int, RoutePath(gt=0, le=MAX_ID)]
BodyId = Annotated[int, Field(gt=0, le=MAX_ID)]
After = Annotated[int, Query(ge=0, le=MAX_ID)]
Limit = Annotated[int, Query(ge=1, le=100)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Day = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
TaskId = Annotated[str, RoutePath(pattern=r"^[a-f0-9]{32}$")]
PREFIX = "/api/libraries/{library}/insights"


class PersonName(BaseModel):
    name: Name | None
    expected_revision: int | None = Field(default=None, ge=0, le=MAX_ID)


class MergePeople(BaseModel):
    source: BodyId
    expected_revision: int | None = Field(default=None, ge=0, le=MAX_ID)
    expected_source_revision: int | None = Field(default=None, ge=0, le=MAX_ID)


class SplitPerson(BaseModel):
    faces: list[BodyId] = Field(min_length=1, max_length=200)
    name: Name | None = None


class TransferDownload(Response):
    def __init__(self, state, job):
        super().__init__()
        self.state = state
        self.name = artifact_name(job["id"], job["kind"])
        self.report = job["kind"] == "report"
        self.identity = job["output_device"], job["output_inode"]

    async def __call__(self, scope, receive, send):
        with ExitStack() as stack:
            try:
                parent = stack.enter_context(transfer_directory(self.state))
                fd, info = stack.enter_context(open_regular(parent, self.name))
                # Completed transfers prior to the publication migration have no marker.
                if self.identity != (None, None) and self.identity != (info.st_dev, info.st_ino):
                    raise OSError("The completed artifact was replaced")
            except (OSError, RuntimeError):
                response = Response("The completed artifact is missing", status_code=404)
            else:
                response = FileResponse(
                    f"/proc/self/fd/{fd}",
                    stat_result=info,
                    media_type="text/html" if self.report else "application/zip",
                    filename="photocull-trip.html" if self.report else "photocull-keepers.zip",
                    headers={
                        "Cache-Control": "private, no-store",
                        "X-Content-Type-Options": "nosniff",
                        "X-Accel-Buffering": "no",
                        "Content-Security-Policy": (
                            "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
                        ),
                    },
                )
                # The descriptor must stay open until the response body is sent.
                scope = {
                    **scope,
                    "extensions": {
                        key: value
                        for key, value in scope.get("extensions", {}).items()
                        if key != "http.response.pathsend"
                    },
                }
            await response(scope, receive, send)


class VideoFlag(BaseModel):
    dead: bool | None


def vtt_timestamp(seconds: float) -> str:
    minutes, millis = divmod(round(max(0, seconds) * 1000), 60_000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{millis // 1000:02d}.{millis % 1000:03d}"


def captions(segments: list[dict]) -> str:
    lines = ["WEBVTT", ""]
    for item in segments:
        start = vtt_timestamp(item["start"])
        end = vtt_timestamp(item["end"])
        if end > start:
            lines.extend((f"{start} --> {end}", escape(" ".join(item["text"].split())), ""))
    return "\n".join(lines) + "\n"


def get_video(db, asset_id):
    row = db.execute(
        """SELECT a.content_hash, f.dead FROM assets AS a LEFT JOIN video_flags AS f
           ON a.id = f.asset_id AND a.content_hash = f.content_hash
           WHERE a.id = ? AND a.active = 1 AND a.kind = 'video'""",
        (asset_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown video")
    return row


def page(rows, limit, cursor="id"):
    return {
        "items": [dict(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1][cursor] if len(rows) > limit else None,
    }


def require(enabled: bool, stage: str):
    if not enabled:
        raise HTTPException(409, f"{stage} is disabled")


def face_key(db, settings):
    require(settings.insights.faces, "Face grouping")
    return public_keys(db, settings)["faces"]


def check_person(db, person_id, revision):
    row = db.execute("SELECT revision FROM people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown person")
    if revision is not None and row[0] != revision:
        raise HTTPException(409, "This person changed. Reload and try again.")


def submit_transfer(db, kind, body):
    try:
        return {"id": enqueue(db, kind, body), "status": "pending"}
    except ValueError as error:
        raise HTTPException(429, str(error)) from error


def get_transfer(db, job_id, config):
    row = db.execute("SELECT * FROM transfers WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown transfer")
    enabled = config.report and config.story if row["kind"] == "report" else config.export
    require(enabled, "Transfer")
    return row


def mount_insights(app: FastAPI, settings: Settings, library_path):
    config = settings.insights

    @app.get(PREFIX)
    def capabilities(library: str):
        with connect(library_path(library)) as db:
            keys = public_keys(db, settings)
            row = db.execute(
                """SELECT status, positives, negatives, requested, completed, input_key, error
                   FROM taste_state"""
            ).fetchone()
            taste = dict(row)
            if not config.taste or not settings.ml.enabled:
                taste["status"] = "disabled"
            elif taste["input_key"] != taste_input(settings) or (
                taste["requested"] != taste["completed"]
            ):
                taste["status"] = "pending"
            del taste["input_key"]
            story_state = db.execute(
                "SELECT error, EXISTS(SELECT 1 FROM story_dirty) AS pending FROM story_state"
            ).fetchone()
            stages = {"story": dict(story_state)} if config.story else {}
            for stage, key in keys.items():
                pending = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM insight_work WHERE stage = ?)", (stage,)
                ).fetchone()[0]
                error = db.execute(
                    """SELECT error FROM insight_results WHERE stage = ? AND key = ?
                       AND error IS NOT NULL LIMIT 1""",
                    (stage, key),
                ).fetchone()
                stages[stage] = {
                    "pending": bool(pending or stage_key(db, stage) != key),
                    "error": error[0] if error else None,
                }
        return {
            "aesthetic": config.aesthetic and settings.ml.enabled,
            "faces": config.faces,
            "story": config.story,
            "video": config.video,
            "transcript": config.transcript and config.video,
            "watch": config.watch,
            "report": config.report and config.story,
            "export": config.export,
            "destinations": list(config.export_roots),
            "taste": taste,
            "stages": stages,
        }

    @app.post(PREFIX + "/taste/train", status_code=202)
    def retrain(library: str):
        require(config.taste and settings.ml.enabled, "Taste learning")
        with connect(library_path(library)) as db, write(db):
            db.execute("UPDATE taste_state SET requested = requested + 1")
            mark_catalog_updated(db)
        return {"status": "pending"}

    @app.post(PREFIX + "/assets/{asset_id}/{stage}/retry", status_code=202)
    def retry(
        library: str, asset_id: Id, stage: Literal["aesthetic", "faces", "video", "transcript"]
    ):
        with connect(library_path(library)) as db:
            requested_key = public_keys(db, settings).get(stage)
            require(requested_key is not None, stage)
            status = queue(db, stage, requested_key, asset_id)
            if status == "waiting":
                raise HTTPException(409, "Waiting for the worker to configure the stage")
            if status == "missing":
                raise HTTPException(404, "Unknown asset")
            if status == "wrong_kind":
                raise HTTPException(422, "This stage does not apply to the asset type")
        return {"status": "pending"}

    @app.get(PREFIX + "/people")
    def people(library: str, cursor: After = 0, limit: Limit = 40):
        with connect(library_path(library)) as db:
            key = face_key(db, settings)
            rows = db.execute(
                """SELECT p.id, p.name, p.revision,
                   (SELECT count(DISTINCT f.asset_id) FROM faces AS f JOIN assets AS a
                    ON a.id = f.asset_id WHERE f.person_id = p.id AND f.key = :key
                    AND a.active = 1) AS count,
                   (SELECT json_object('asset_id', a.id, 'content_hash', a.content_hash,
                       'left', f.left, 'top', f.top, 'right', f.right, 'bottom', f.bottom)
                    FROM faces AS f JOIN assets AS a ON a.id = f.asset_id
                    WHERE f.person_id = p.id AND f.key = :key AND a.active = 1
                    ORDER BY f.quality DESC, f.id LIMIT 1) AS cover
                   FROM people AS p WHERE p.id > :cursor AND EXISTS (
                    SELECT 1 FROM faces AS f JOIN assets AS a ON a.id = f.asset_id
                    WHERE f.person_id = p.id AND f.key = :key AND a.active = 1)
                   ORDER BY p.id LIMIT :limit""",
                {"key": key, "cursor": cursor, "limit": limit + 1},
            ).fetchall()
        result = page(rows, limit)
        for item in result["items"]:
            item["cover"] = json.loads(item["cover"])
        return result

    @app.get(PREFIX + "/people/{person_id}/faces")
    def person_faces(library: str, person_id: Id, cursor: After = 0, limit: Limit = 40):
        with connect(library_path(library)) as db:
            db.execute("BEGIN")
            key = face_key(db, settings)
            person = db.execute(
                "SELECT id, name, revision FROM people WHERE id=?", (person_id,)
            ).fetchone()
            if person is None:
                raise HTTPException(404, "Unknown person")
            rows = db.execute(
                """SELECT f.id, f.asset_id, f.left, f.top, f.right, f.bottom, f.quality,
                          f.confidence, a.path, a.content_hash
                   FROM faces AS f JOIN assets AS a ON a.id = f.asset_id
                   WHERE f.person_id = ? AND f.key = ? AND a.active = 1 AND f.id > ?
                   ORDER BY f.id LIMIT ?""",
                (person_id, key, cursor, limit + 1),
            ).fetchall()
        return {**page(rows, limit), "person": dict(person)}

    @app.patch(PREFIX + "/people/{person_id}")
    def rename(library: str, person_id: Id, body: PersonName):
        with connect(library_path(library)) as db, write(db):
            face_key(db, settings)
            check_person(db, person_id, body.expected_revision)
            row = db.execute(
                """UPDATE people SET name = ?, revision = revision + 1 WHERE id = ?
                   RETURNING id, name, revision""",
                (body.name, person_id),
            ).fetchone()
            mark_catalog_updated(db)
            return dict(row)

    @app.post(PREFIX + "/people/{person_id}/merge")
    def merge(library: str, person_id: Id, body: MergePeople):
        if body.source == person_id:
            raise HTTPException(422, "Choose two different people")
        with connect(library_path(library)) as db, write(db):
            face_key(db, settings)
            check_person(db, person_id, body.expected_revision)
            check_person(db, body.source, body.expected_source_revision)
            moved = db.execute(
                "UPDATE faces SET person_id = ?, manual = 1 WHERE person_id = ?",
                (person_id, body.source),
            ).rowcount
            if not moved:
                raise HTTPException(409, "This person has no faces to merge")
            db.execute(
                """UPDATE people SET name = COALESCE(name, (SELECT name FROM people WHERE id = ?)),
                   revision = revision + 1 WHERE id = ?""",
                (body.source, person_id),
            )
            db.execute("UPDATE faces SET manual = 1 WHERE person_id = ?", (person_id,))
            db.execute("DELETE FROM people WHERE id = ?", (body.source,))
            person = db.execute(
                "SELECT id, name, revision FROM people WHERE id = ?", (person_id,)
            ).fetchone()
            mark_catalog_updated(db)
            return {**dict(person), "moved": moved}

    @app.post(PREFIX + "/people/{person_id}/split")
    def split(library: str, person_id: Id, body: SplitPerson):
        faces = set(body.faces)
        with connect(library_path(library)) as db, write(db):
            key = face_key(db, settings)
            new = db.execute("INSERT INTO people(name) VALUES (?) RETURNING id", (body.name,))
            target = new.fetchone()[0]
            moved = db.execute(
                """UPDATE faces SET person_id = ?, manual = 1 WHERE person_id = ? AND key = ?
                   AND id IN (SELECT value FROM json_each(?))
                   AND asset_id IN (SELECT id FROM assets WHERE active = 1)""",
                (target, person_id, key, json.dumps(sorted(faces))),
            ).rowcount
            if moved != len(faces):
                raise HTTPException(409, "Face membership changed. Reload and select faces again.")
            db.execute("UPDATE people SET revision = revision + 1 WHERE id = ?", (person_id,))
            mark_catalog_updated(db)
            return {"id": target, "moved": moved}

    @app.get(PREFIX + "/stories")
    def stories(library: str, cursor: Day | None = None, limit: Limit = 30):
        require(config.story, "Stories")
        with connect(library_path(library)) as db:
            rows = days(db, cursor or "9999-99-99", limit + 1)
        return page(rows, limit, "day")

    @app.get(PREFIX + "/stories/{day}")
    def story(library: str, day: Day, cursor: After = 0, limit: Limit = 30):
        require(config.story, "Stories")
        with connect(library_path(library)) as db:
            db.execute("BEGIN")
            items = segments(db, day, cursor, limit + 1)
        return page(items, limit, "position")

    @app.get(PREFIX + "/assets/{asset_id}/video")
    def video(library: str, asset_id: Id):
        require(config.video, "Video analysis")
        with connect(library_path(library)) as db:
            db.execute("BEGIN")
            asset = get_video(db, asset_id)
            keys = public_keys(db, settings)
            output = {
                "override": None if asset["dead"] is None else bool(asset["dead"]),
            }
            for stage in ("video", "transcript"):
                if stage not in keys:
                    continue
                row = db.execute(
                    """SELECT payload, error FROM insight_results
                       WHERE asset_id = ? AND stage = ? AND key = ? AND content_hash = ?""",
                    (asset_id, stage, keys[stage], asset["content_hash"]),
                ).fetchone()
                pending = db.execute(
                    "SELECT 1 FROM insight_work WHERE stage = ? AND asset_id = ?",
                    (stage, asset_id),
                ).fetchone()
                output[stage] = {
                    "status": "pending"
                    if pending or row is None
                    else "error"
                    if row["error"]
                    else "done",
                    "error": row["error"] if row else None,
                    "data": json.loads(row["payload"]) if row and row["payload"] else None,
                }
            return output

    @app.get(PREFIX + "/assets/{asset_id}/video/preview")
    def video_preview(library: str, asset_id: Id):
        require(config.video, "Video analysis")
        path = library_path(library)
        with connect(path) as db:
            asset = get_video(db, asset_id)
            key = public_keys(db, settings)["video"]
            row = db.execute(
                """SELECT 1 FROM insight_results WHERE asset_id=? AND stage='video' AND key=?
                   AND content_hash=? AND json_extract(payload, '$.preview')=1""",
                (asset_id, key, asset["content_hash"]),
            ).fetchone()
        target = video_path(path.parent, asset_id, asset["content_hash"], key)
        if row is None or not target.is_file():
            raise HTTPException(404, "Video preview unavailable")
        return FileResponse(
            target, media_type="video/mp4", headers={"Cache-Control": "private, no-cache"}
        )

    @app.get(PREFIX + "/assets/{asset_id}/video/captions")
    def video_captions(library: str, asset_id: Id):
        require(config.video and config.transcript, "Transcription")
        with connect(library_path(library)) as db:
            asset = get_video(db, asset_id)
            key = public_keys(db, settings)["transcript"]
            row = db.execute(
                """SELECT payload FROM insight_results WHERE asset_id=? AND stage='transcript'
                   AND key=? AND content_hash=? AND payload IS NOT NULL""",
                (asset_id, key, asset["content_hash"]),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Captions unavailable")
        output = captions(json.loads(row["payload"])["segments"])
        return Response(
            output, media_type="text/vtt", headers={"Cache-Control": "private, no-cache"}
        )

    @app.patch(PREFIX + "/assets/{asset_id}/video")
    def video_flag(library: str, asset_id: Id, body: VideoFlag):
        require(config.video, "Video analysis")
        with connect(library_path(library)) as db, write(db):
            asset = get_video(db, asset_id)
            if body.dead is None:
                db.execute("DELETE FROM video_flags WHERE asset_id = ?", (asset_id,))
            else:
                db.execute(
                    """INSERT INTO video_flags VALUES(?, ?, ?)
                       ON CONFLICT(asset_id) DO UPDATE SET dead = excluded.dead,
                       content_hash = excluded.content_hash""",
                    (asset_id, asset["content_hash"], body.dead),
                )
            mark_catalog_updated(db)
        return {"override": body.dead}

    @app.post(PREFIX + "/exports", status_code=202)
    def export(library: str, body: ExportRequest):
        require(config.export, "Keeper export")
        if body.kind == "copy" and body.destination not in config.export_roots:
            raise HTTPException(422, "Choose a configured copy destination")
        if body.kind == "zip" and body.destination is not None:
            raise HTTPException(422, "ZIP downloads are stored in library state")
        if body.selection.person_id is not None:
            require(config.faces, "Face grouping")
        if body.selection.dead is not None:
            require(config.video, "Video analysis")
        with connect(library_path(library)) as db:
            return submit_transfer(db, body.kind, body)

    @app.post(PREFIX + "/reports", status_code=202)
    def report(library: str, body: ReportRequest):
        require(config.report and config.story, "Trip report")
        with connect(library_path(library)) as db:
            return submit_transfer(db, "report", body)

    @app.get(PREFIX + "/transfers")
    def transfers(library: str, kind: Literal["export", "report"], limit: Limit = 30):
        require(config.export if kind == "export" else config.report and config.story, kind)
        with connect(library_path(library)) as db:
            rows = db.execute(
                """SELECT * FROM transfers WHERE (kind = 'report') = ?
                   ORDER BY (status IN ('pending','running','cancelling')) DESC,
                            created_at DESC, id DESC LIMIT ?""",
                (kind == "report", limit),
            ).fetchall()
        return {"items": [public(row) for row in rows]}

    @app.delete(PREFIX + "/transfers/{job_id}")
    def cancel_transfer(library: str, job_id: TaskId):
        with connect(library_path(library)) as db:
            get_transfer(db, job_id, config)
            request = cancel(db, job_id)
        if request is None:
            raise HTTPException(409, "The transfer has already finished")
        return public(request)

    @app.get(PREFIX + "/transfers/{job_id}/download")
    def download(library: str, job_id: TaskId):
        path = library_path(library)
        with connect(path) as db:
            row = get_transfer(db, job_id, config)
        if row["status"] != "done" or row["kind"] == "copy":
            raise HTTPException(409, "This transfer has no available download")
        return TransferDownload(path.parent, row)
