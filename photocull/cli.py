from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

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
    xmp = commands.add_parser("xmp", help="preview or write XMP ratings for selected asset IDs")
    xmp.add_argument("library")
    xmp.add_argument("asset_ids", type=int, nargs="+")
    mode = xmp.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview changes (default)")
    mode.add_argument("--write", action="store_true", help="write confirmed by the operator")
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
