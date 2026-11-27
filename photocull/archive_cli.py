from __future__ import annotations

import json
import os
import pwd
from pathlib import Path

from photocull.archive import ArchiveError, ArchiveRequest
from photocull.db import connect
from photocull.db.archive import audit_page, cancel, enqueue, jobs, retry_recovery, trash
from photocull.db.archive_preview import build, page, queue
from photocull.db.archive_report import duplicates, storage


def add_command(commands):
    parser = commands.add_parser("archive", help="preview and manage archive operations")
    parser.add_argument("library")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("preview", "apply"):
        command = actions.add_parser(action)
        command.add_argument("plan", type=Path, help="JSON file containing the archive request")
        if action == "apply":
            command.add_argument("--confirmation", required=True)
            command.add_argument("--yes", action="store_true", required=True)
    detail = actions.add_parser("preview-page")
    detail.add_argument("preview_id")
    detail.add_argument("--after", type=int, default=0)
    job = actions.add_parser("jobs")
    job.add_argument("job_id", nargs="?")
    job.add_argument("--after", type=int, default=0)
    for action in ("cancel", "recover"):
        actions.add_parser(action).add_argument("job_id")
    actions.add_parser("trash").add_argument("--before")
    actions.add_parser("audit").add_argument("--after", type=int, default=0)
    actions.add_parser("storage").add_argument(
        "--by",
        choices=("year", "folder", "format", "camera"),
        default="year",
    )
    dup = actions.add_parser("duplicates")
    dup.add_argument("--by", choices=("exact", "near", "pairs"), default="exact")
    dup.add_argument("--after", type=int, default=0)


def run(args, settings, db_path):
    if not db_path.is_file():
        raise ValueError("Library index is missing. Start the API or worker and scan the library.")
    with connect(db_path) as db:
        action = args.action
        uid = os.getuid()
        try:
            actor = f"cli:{pwd.getpwuid(uid).pw_name}({uid})"
        except KeyError:
            # Container UIDs can run without a corresponding /etc/passwd entry.
            actor = f"cli:uid={uid}"
        if action in {"preview", "apply"}:
            plan = ArchiveRequest.model_validate_json(
                args.plan.read_text(encoding="utf-8")
            ).with_location_policy(settings)
            if action == "preview":
                id_ = queue(db, settings, args.library, plan)
                build(db_path, settings, args.library, id_=id_)
                result = page(db, id_)
                if result["status"] == "error":
                    raise ArchiveError(result["error"])
                return result
            return {"id": enqueue(db, settings, args.library, plan, args.confirmation, actor)}
        if action == "preview-page":
            return page(db, args.preview_id, args.after)
        if action == "jobs":
            return jobs(db, args.job_id, args.after)
        if action in {"cancel", "recover"}:
            (cancel if action == "cancel" else retry_recovery)(db, args.job_id, actor)
            return {"id": args.job_id}
        if action == "trash":
            return trash(db, before=args.before)
        if action == "storage":
            return storage(db, args.by)
        if action == "duplicates":
            return duplicates(db, args.by, args.after)
        return audit_page(db, args.after)


def print_result(args, settings, db_path):
    print(json.dumps(run(args, settings, db_path), ensure_ascii=False, indent=2))


def selftest(argv):
    import argparse

    from photocull.config import load_settings
    from photocull.worker.archive_selftest import describe

    parser = argparse.ArgumentParser(prog="photocull archive selftest")
    parser.add_argument("path", type=Path)
    parser.add_argument("--library")
    args = parser.parse_args(argv)
    settings = load_settings()
    library = args.library or (
        next(iter(settings.libraries)) if len(settings.libraries) == 1 else None
    )
    if library not in settings.libraries:
        parser.error("Specify a configured library with --library.")
    try:
        result = describe(settings, library, args.path.expanduser().absolute())
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)
