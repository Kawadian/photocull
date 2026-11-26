from __future__ import annotations

import argparse
import json

from photocull.config import load_settings
from photocull.db import connect, initialize
from photocull.db.jobs import progress, request_scan


def main() -> None:
    parser = argparse.ArgumentParser(prog="photocull")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("libraries", help="list configured libraries")
    commands.add_parser("scan", help="queue a library scan").add_argument("library")
    commands.add_parser("status", help="show job progress").add_argument("library")
    args = parser.parse_args()
    settings = load_settings()
    if args.command == "libraries":
        for library in settings.libraries:
            print(library)
        return
    if args.library not in settings.libraries:
        parser.error(f"Unknown library: {args.library}")
    path = settings.library_state(args.library) / "index.db"
    initialize(path)
    with connect(path) as db:
        result = dict(request_scan(db)) if args.command == "scan" else progress(db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
