from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from sqlite3 import OperationalError

from photocull.archive_cli import add_command, print_result
from photocull.config import load_settings
from photocull.db import connect, initialize
from photocull.db.jobs import progress, request_scan
from photocull.media.xmp import XMPError, export_xmp


def main() -> None:
    parser = argparse.ArgumentParser(prog="photocull")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("libraries", help="list configured libraries")
    commands.add_parser("scan", help="queue a library scan").add_argument("library")
    commands.add_parser("status", help="show job progress").add_argument("library")
    commands.add_parser("fetch-model", help="prepare the worker's pinned ONNX model cache")
    insights = commands.add_parser(
        "fetch-insight-models", help="prefetch enabled face and speech models for offline use"
    )
    insights.add_argument("--faces", action="store_true")
    insights.add_argument("--transcript", action="store_true")
    xmp = commands.add_parser("xmp", help="preview or write XMP ratings for selected asset IDs")
    xmp.add_argument("library")
    xmp.add_argument("asset_ids", type=int, nargs="+")
    mode = xmp.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview changes (default)")
    mode.add_argument("--write", action="store_true", help="write confirmed by the operator")
    add_command(commands)
    args = parser.parse_args()
    settings = load_settings()
    if args.command == "libraries":
        for library in settings.libraries:
            print(library)
        return
    if args.command == "fetch-model":
        from photocull.worker.ml.model import FILES, ModelUnavailable, model_file

        model = settings.ml.model_copy(update={"offline": False})
        try:
            for name in FILES:
                print(model_file(model, settings.model_cache, name))
        except ModelUnavailable as error:
            parser.exit(1, f"{error}\n")
        return
    if args.command == "fetch-insight-models":
        from photocull.worker.insights.model_cache import face_files, whisper_folder

        config = settings.insights
        explicit = args.faces or args.transcript
        fetch_faces = args.faces if explicit else config.faces
        fetch_speech = args.transcript if explicit else config.video and config.transcript
        if not fetch_faces and not fetch_speech:
            parser.error("Enable a face or transcript model, or pass --faces or --transcript")
        try:
            if fetch_faces:
                for path in face_files(settings.model_cache, config.face_revision, offline=False):
                    print(path)
            if fetch_speech:
                print(
                    whisper_folder(
                        settings.model_cache,
                        config.transcript_model,
                        config.transcript_revision,
                        offline=False,
                    )
                )
        except (OSError, ValueError, ImportError, RuntimeError) as error:
            parser.exit(1, f"{error}\n")
        return
    if args.library not in settings.libraries:
        parser.error(f"Unknown library: {args.library}")
    path = settings.library_state(args.library) / "index.db"
    if args.command == "archive":
        try:
            print_result(args, settings, path)
        except (LookupError, OSError, ValueError, OperationalError) as error:
            parser.exit(1, f"{error}\n")
        return
    initialize(path)
    if args.command == "xmp":
        failed = False
        for asset_id in dict.fromkeys(args.asset_ids):
            try:
                result = export_xmp(settings, args.library, asset_id, dry_run=not args.write)
            except (LookupError, XMPError) as error:
                print(f"Asset {asset_id}: {error}", file=sys.stderr)
                failed = True
            else:
                print(json.dumps({"id": asset_id, **asdict(result)}, ensure_ascii=False))
        if failed:
            raise SystemExit(1)
        return
    with connect(path) as db:
        result = dict(request_scan(db)) if args.command == "scan" else progress(db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
