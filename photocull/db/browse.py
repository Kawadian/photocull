from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from heapq import merge
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from photocull.config import Settings
from photocull.db import connect
from photocull.db.browse_filters import Filters, asset_labels, clauses


class TimelineKey(NamedTuple):
    day: str
    time: str
    library: str
    asset_id: int


def _attach_collections(db: sqlite3.Connection, settings: Settings, album: str | None) -> None:
    if album:
        db.execute("ATTACH DATABASE ? AS collections", (str(settings.state_dir / "browser.db"),))


def _conditions(
    db: sqlite3.Connection,
    library: str,
    filters: Filters | None,
    settings: Settings,
    album: str | None,
) -> tuple[str, list]:
    conditions, params = clauses(db, library, filters, settings) if filters else ([], [])
    if album:
        conditions.append("""a.id IN (SELECT asset_id FROM collections.album_members
            WHERE album_id = ? AND library = ?)""")
        params.extend((album, library))
    return "".join(f" AND ({condition})" for condition in conditions), params


def _library_page(
    db: sqlite3.Connection,
    library: str,
    limit: int,
    month: str | None,
    after: TimelineKey | None,
    recipe: str | None,
    filters: Filters,
    settings: Settings,
    manual_album: str | None,
) -> list[tuple[TimelineKey, sqlite3.Row]]:
    where, params = _conditions(db, library, filters, settings, manual_album)

    def fetch(bound: str, args: tuple, count: int) -> list[sqlite3.Row]:
        return db.execute(
            f"""SELECT a.id, a.day, a.timeline_time, a.path, a.kind, a.width, a.height,
                       a.duration, a.content_hash, a.favorite, a.revision,
                       p.status AS preview_status, d.blurhash
                FROM assets AS a
                LEFT JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
                LEFT JOIN derivative_placeholders AS d
                    ON d.asset_id = a.id AND d.version = a.content_hash || '-' || ?
                WHERE a.active = 1 {where} {bound}
                ORDER BY a.day DESC, a.timeline_time DESC, a.id DESC LIMIT ?""",
            (recipe, *params, *args, count),
        ).fetchall()

    if after and library == after.library:
        # Equalities on both time fields let SQLite seek on ID even in a large tie.
        rows = fetch(
            "AND a.day = ? AND a.timeline_time = ? AND a.id < ?",
            (after.day, after.time, after.asset_id),
            limit,
        )
        if (remaining := limit - len(rows)) > 0:
            rows.extend(
                fetch("AND (a.day, a.timeline_time) < (?, ?)", (after.day, after.time), remaining)
            )
    elif after:
        op = "<" if library > after.library else "<="
        rows = fetch(f"AND (a.day, a.timeline_time) {op} (?, ?)", (after.day, after.time), limit)
    elif month:
        rows = fetch("AND a.day <= ?", (f"{month}-31",), limit)
    else:
        rows = fetch("", (), limit)
    return [
        (TimelineKey(row["day"], row["timeline_time"], library, row["id"]), row) for row in rows
    ]


def timeline_page(
    libraries: Mapping[str, Path],
    limit: int,
    month: str | None,
    after: TimelineKey | None,
    recipe: str | None,
    filters: Filters,
    settings: Settings,
    manual_album: str | None = None,
) -> tuple[list[dict], TimelineKey | None]:
    pages = []
    for library, path in libraries.items():
        with connect(path) as db:
            _attach_collections(db, settings, manual_album)
            db.execute("BEGIN")
            pages.append(
                _library_page(
                    db, library, limit + 1, month, after, recipe, filters, settings, manual_album
                )
            )
    rows = list(islice(merge(*pages, key=lambda item: item[0], reverse=True), limit + 1))
    next_key = rows[limit - 1][0] if len(rows) > limit else None
    items = []
    for key, row in rows[:limit]:
        item = dict(row)
        item["library"] = key.library
        item["name"] = PurePosixPath(item.pop("path")).name
        item["favorite"] = bool(item["favorite"])
        item.pop("timeline_time")
        items.append(item)
    return items, next_key


def _library_months(db: sqlite3.Connection, where: str, params: list) -> Iterator[str]:
    # Seek to the preceding month without visiting all the photos in the current month.
    before = ":"
    while row := db.execute(
        f"""SELECT a.day FROM assets AS a
            WHERE a.active = 1 AND a.day < ? {where} ORDER BY a.day DESC LIMIT 1""",
        (before, *params),
    ).fetchone():
        month = row["day"][:7]
        month_start = f"{month}-01"
        if month_start <= row["day"]:
            yield month
        before = min(month_start, row["day"])


def timeline_months(
    libraries: Mapping[str, Path],
    settings: Settings,
    filters: Filters | None = None,
    manual_album: str | None = None,
) -> list[str]:
    months: set[str] = set()
    for library, path in libraries.items():
        with connect(path) as db:
            _attach_collections(db, settings, manual_album)
            db.execute("BEGIN")
            where, params = _conditions(db, library, filters, settings, manual_album)
            months.update(_library_months(db, where, params))
    return sorted(months, reverse=True)


def photo_details(db: sqlite3.Connection, asset: sqlite3.Row, face_key: str | None) -> dict:
    camera = db.execute(
        """SELECT lens, shutter_seconds, aperture, focal_length_mm, iso
           FROM assets WHERE id = ?""",
        (asset["id"],),
    ).fetchone()
    faces = (
        db.execute(
            """SELECT f.id, p.name, f.person_id, f.left, f.top, f.right, f.bottom
               FROM faces AS f JOIN people AS p ON p.id = f.person_id
               WHERE f.asset_id = ? AND f.key = ? AND f.content_hash = ?
               ORDER BY f.id LIMIT 100""",
            (asset["id"], face_key, asset["content_hash"]),
        ).fetchall()
        if face_key is not None
        else []
    )
    return {**dict(camera), **asset_labels(db, asset["id"]), "faces": [dict(row) for row in faces]}
