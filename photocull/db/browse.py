from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from heapq import merge
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from photocull.db import connect


class TimelineKey(NamedTuple):
    day: str
    time: str
    library: str
    asset_id: int


def _library_page(
    db: sqlite3.Connection,
    library: str,
    limit: int,
    month: str | None,
    after: TimelineKey | None,
    recipe: str | None,
) -> list[tuple[TimelineKey, sqlite3.Row]]:
    def fetch(bound: str, args: tuple, count: int) -> list[sqlite3.Row]:
        return db.execute(
            f"""SELECT a.id, a.day, a.timeline_time, a.path, a.kind, a.width, a.height,
                       a.duration, a.content_hash, p.status AS preview_status, d.blurhash
                FROM assets AS a
                LEFT JOIN jobs AS p ON p.asset_id = a.id AND p.stage = 'preview'
                LEFT JOIN derivative_placeholders AS d
                    ON d.asset_id = a.id AND d.version = a.content_hash || '-' || ?
                WHERE a.active = 1 {bound}
                ORDER BY a.day DESC, a.timeline_time DESC, a.id DESC LIMIT ?""",
            (recipe, *args, count),
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
    recipe: str | None = None,
) -> tuple[list[dict], TimelineKey | None]:
    pages = []
    for library, path in libraries.items():
        with connect(path) as db:
            db.execute("BEGIN")
            pages.append(_library_page(db, library, limit + 1, month, after, recipe))
    rows = list(islice(merge(*pages, key=lambda item: item[0], reverse=True), limit + 1))
    next_key = rows[limit - 1][0] if len(rows) > limit else None
    items = []
    for key, row in rows[:limit]:
        item = dict(row)
        item["library"] = key.library
        item["name"] = PurePosixPath(item.pop("path")).name
        item.pop("timeline_time")
        items.append(item)
    return items, next_key


def _library_months(db: sqlite3.Connection) -> Iterator[str]:
    # Seek to the preceding month without visiting all the photos in the current month.
    before = ":"
    while row := db.execute(
        """SELECT day FROM assets WHERE active = 1 AND day < ? ORDER BY day DESC LIMIT 1""",
        (before,),
    ).fetchone():
        month = row["day"][:7]
        month_start = f"{month}-01"
        if month_start <= row["day"]:
            yield month
        before = min(month_start, row["day"])


def timeline_months(libraries: Mapping[str, Path]) -> list[str]:
    months: set[str] = set()
    for path in libraries.values():
        with connect(path) as db:
            db.execute("BEGIN")
            months.update(_library_months(db))
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
    return {**dict(camera), "faces": [dict(row) for row in faces]}
