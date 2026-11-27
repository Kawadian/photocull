from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from photocull.archive import ArchiveError, ArchiveRequest, TrashID, capabilities
from photocull.db import connect, write
from photocull.db.archive import audit_page, cancel, enqueue, jobs, retry_recovery, trash
from photocull.db.archive_preview import page, queue
from photocull.db.archive_report import duplicates, storage
from photocull.db.archive_rules import ArchiveRule, rules
from photocull.db.archive_rules import action as rule_action
from photocull.db.archive_rules import save as save_rule

Key = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: ArchiveRequest
    confirmation: Digest


def invoke(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (ArchiveError, OSError) as error:
        raise HTTPException(409, str(error)) from error
    except sqlite3.OperationalError as error:
        if "locked" not in str(error):
            raise
        raise HTTPException(503, "Archive database is busy. Try again.") from error


def mount_archive(app: FastAPI, settings, library_path):
    base = "/api/libraries/{library}/archive"

    def actor(request: Request) -> str:
        address = request.client.host if request.client else "local"
        policy = "token" if settings.token_file else "local"
        return f"api:{policy}:{address}"

    @app.get(base)
    def capabilities_route(library: str):
        with connect(library_path(library)) as db:
            return capabilities(db, settings, library)

    @app.post(f"{base}/selftest", status_code=202)
    def recheck(library: str):
        with connect(library_path(library)) as db, write(db):
            if not capabilities(db, settings, library)["write_checks"]:
                raise HTTPException(409, "Configure PHOTOCULL_WRITE_ROOTS before testing.")
            if db.execute("SELECT 1 FROM archive_jobs WHERE finished_at IS NULL").fetchone():
                raise HTTPException(409, "Wait for the active archive job to finish.")
            db.execute("DELETE FROM archive_write_checks")
        return {"status": "queued"}

    @app.post(f"{base}/preview", status_code=202)
    def dry_run(library: str, body: ArchiveRequest):
        with connect(library_path(library)) as db:
            return {"id": invoke(queue, db, settings, library, body)}

    @app.get(f"{base}/previews/{{preview_id}}")
    def preview_details(
        library: str, preview_id: Key, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0
    ):
        with connect(library_path(library)) as db:
            return invoke(page, db, preview_id, after)

    @app.post(f"{base}/jobs", status_code=202)
    def submit(library: str, body: Confirmation, request: Request):
        with connect(library_path(library)) as db:
            job_id = invoke(
                enqueue, db, settings, library, body.plan, body.confirmation, actor(request)
            )
            return {"id": job_id}

    @app.get(f"{base}/jobs")
    def list_jobs(library: str):
        with connect(library_path(library)) as db:
            return {"items": jobs(db)}

    @app.get(f"{base}/jobs/{{job_id}}")
    def job_details(
        library: str, job_id: Key, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0
    ):
        with connect(library_path(library)) as db:
            return invoke(jobs, db, job_id, after)

    @app.post(f"{base}/jobs/{{job_id}}/cancel", status_code=202)
    def stop(library: str, job_id: Key, request: Request):
        with connect(library_path(library)) as db:
            invoke(cancel, db, job_id, actor(request))
            return {"id": job_id}

    @app.post(f"{base}/jobs/{{job_id}}/recover", status_code=202)
    def recover(library: str, job_id: Key, request: Request):
        with connect(library_path(library)) as db:
            invoke(retry_recovery, db, job_id, actor(request))
            return {"id": job_id}

    @app.get(f"{base}/trash")
    def list_trash(library: str, before: TrashID | None = None):
        with connect(library_path(library)) as db:
            return trash(db, before=before)

    @app.get(f"{base}/audit")
    def list_audit(library: str, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        with connect(library_path(library)) as db:
            return audit_page(db, after)

    @app.get(f"{base}/storage")
    def storage_report(library: str, by: Literal["year", "folder", "format", "camera"] = "year"):
        with connect(library_path(library)) as db:
            return storage(db, by)

    @app.get(f"{base}/duplicates")
    def duplicate_report(
        library: str,
        by: Literal["exact", "near", "pairs"] = "exact",
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    ):
        with connect(library_path(library)) as db:
            return duplicates(db, by, after)

    @app.get(f"{base}/rules")
    def list_rules(library: str):
        with connect(library_path(library)) as db:
            return {"items": rules(db)}

    @app.post(f"{base}/rules", status_code=201)
    def add_rule(library: str, body: ArchiveRule):
        with connect(library_path(library)) as db:
            return {"id": invoke(save_rule, db, body)}

    @app.post(f"{base}/rules/{{rule_id}}/{{command}}", status_code=202)
    def change_rule(
        library: str, rule_id: Key, command: Literal["preview", "seen", "pause", "delete"]
    ):
        with connect(library_path(library)) as db:
            invoke(rule_action, db, rule_id, command)
            return {"id": rule_id}
