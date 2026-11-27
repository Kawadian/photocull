from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from photocull.config import Settings
from photocull.db import connect, write
from photocull.db.catalog import AUTO_REJECT_REASON, mark_catalog_updated
from photocull.db.insights import public_keys

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
TagName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Ratio = Annotated[float, Field(ge=0, le=1)]
Stars = Annotated[int, Field(ge=0, le=5)]
CAMERA = "trim(coalesce(a.make, '') || ' ' || coalesce(a.model, ''))"
PERSON_ID = re.compile(r"id:([A-Za-z0-9][A-Za-z0-9_-]{0,63}):([1-9][0-9]{0,18})")


def tag_key(name: str) -> str:
    return unicodedata.normalize("NFC", name.casefold())


class Filters(BaseModel):
    kind: Literal["photo", "image", "raw", "heif", "video"] | None = None
    favorite: bool | None = None
    flag: Literal["manual", "automatic", "clear"] | None = None
    tag: Text | None = None
    person: Text | None = None
    camera: Text | None = None
    lens: Text | None = None
    place: Text | None = None
    start: date | None = None
    end: date | None = None
    min_rating: Stars | None = None
    max_rating: Stars | None = None
    min_sharpness: Ratio | None = None
    max_sharpness: Ratio | None = None
    min_aesthetic: Ratio | None = None
    max_aesthetic: Ratio | None = None

    @model_validator(mode="after")
    def validate_ranges_and_keys(self) -> Filters:
        for lower, upper in (
            (self.start, self.end),
            (self.min_rating, self.max_rating),
            (self.min_sharpness, self.max_sharpness),
            (self.min_aesthetic, self.max_aesthetic),
        ):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("The lower filter bound must be at most the upper bound")
        if self.tag:
            source, _, value = self.tag.partition(":")
            if source not in {"auto", "user"} or not value:
                raise ValueError("Tag must identify an automatic or user tag")
        if self.person:
            if self.person.startswith("name:") and self.person[5:]:
                return self
            if not (match := PERSON_ID.fullmatch(self.person)) or int(match[2]) > 2**63 - 1:
                raise ValueError("Person must identify a name or a person in a library")
        return self


class LabelUpdate(BaseModel):
    favorite: bool | None = None
    add_tags: list[TagName] = Field(default_factory=list, max_length=24)
    remove_tags: list[TagName] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_labels(self) -> LabelUpdate:
        names = self.add_tags + self.remove_tags
        if any(not text.isprintable() for text in names):
            raise ValueError("Tags must use printable characters")
        if {tag_key(name) for name in self.add_tags}.intersection(map(tag_key, self.remove_tags)):
            raise ValueError("Add and remove different tags in the same request")
        return self


def asset_labels(db: sqlite3.Connection, asset_id: int) -> dict:
    favorite = db.execute("SELECT favorite FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]
    tags = db.execute(
        "SELECT name FROM browser_tags WHERE asset_id = ? ORDER BY name_key", (asset_id,)
    ).fetchall()
    return {"favorite": bool(favorite), "user_tags": [row["name"] for row in tags]}


def apply_labels(db: sqlite3.Connection, asset_id: int, change: LabelUpdate) -> dict | None:
    with write(db):
        if (
            db.execute("SELECT 1 FROM assets WHERE active = 1 AND id = ?", (asset_id,)).fetchone()
            is None
        ):
            return None
        removed = db.executemany(
            "DELETE FROM browser_tags WHERE asset_id = ? AND name_key = ?",
            ((asset_id, tag_key(name)) for name in change.remove_tags),
        ).rowcount
        added = db.executemany(
            "INSERT OR IGNORE INTO browser_tags(asset_id, name_key, name) VALUES (?, ?, ?)",
            (
                (asset_id, tag_key(name), unicodedata.normalize("NFC", name))
                for name in change.add_tags
            ),
        ).rowcount
        toggled = (
            change.favorite is not None
            and db.execute(
                "UPDATE assets SET favorite = ? WHERE id = ? AND favorite <> ?",
                (change.favorite, asset_id, change.favorite),
            ).rowcount
        )
        labels = asset_labels(db, asset_id)
        if len(labels["user_tags"]) > 64:
            raise ValueError("A photo can have at most 64 user tags")
        if removed or added or toggled:
            mark_catalog_updated(db)
        return labels


def clauses(db: sqlite3.Connection, library: str, filters: Filters, settings: Settings):
    conditions, params = [], []
    model = settings.ml
    if filters.kind == "photo":
        conditions.append("a.kind <> 'video'")
    elif filters.kind is not None:
        conditions.append("a.kind = ?")
        params.append(filters.kind)
    if filters.favorite is not None:
        conditions.append("a.favorite = 1" if filters.favorite else "a.favorite = 0")
    if filters.flag == "manual":
        conditions.append("a.rejected = 1")
    elif filters.flag:
        automatic = f"""EXISTS (SELECT 1 FROM assets AS x
            LEFT JOIN asset_scores AS s ON s.asset_id = x.id WHERE x.id = a.id
            AND ({AUTO_REJECT_REASON}) IS NOT NULL)"""
        if filters.flag == "automatic":
            conditions.append(automatic)
        else:
            conditions.append(f"a.rejected = 0 AND NOT {automatic}")
    for condition, value in (
        (CAMERA + " = ?", filters.camera),
        ("a.lens = ?", filters.lens),
        ("a.day >= ?", filters.start.isoformat() if filters.start else None),
        ("a.day <= ?", filters.end.isoformat() if filters.end else None),
        ("a.rating >= ?", filters.min_rating),
        ("a.rating <= ?", filters.max_rating),
    ):
        if value is not None:
            conditions.append(condition)
            params.append(value)
    if filters.tag:
        source, _, value = filters.tag.partition(":")
        if source == "user":
            conditions.append("""EXISTS (SELECT 1 FROM browser_tags AS t
                WHERE t.asset_id = a.id AND t.name_key = ?)""")
            params.append(tag_key(value))
        elif model.enabled and model.tags_enabled and value in model.vocabulary:
            conditions.append("""EXISTS (SELECT 1 FROM asset_tags AS t
                JOIN asset_embeddings AS e ON e.asset_id = t.asset_id WHERE t.asset_id = a.id
                AND t.tag = ? AND e.tag_key = ? AND e.content_hash = a.content_hash)""")
            params.extend((value, model.tag_key))
        else:
            conditions.append("0")
    keys = (
        public_keys(db, settings)
        if filters.person
        or (filters.min_aesthetic is not None or filters.max_aesthetic is not None)
        else {}
    )
    if filters.person:
        key = keys.get("faces")
        match = PERSON_ID.fullmatch(filters.person)
        if not key or (match and match[1] != library):
            conditions.append("0")
        else:
            field = "f.person_id" if match else "p.name"
            conditions.append(f"""EXISTS (SELECT 1 FROM faces AS f
                JOIN people AS p ON p.id = f.person_id
                WHERE f.asset_id = a.id AND f.key = ? AND f.content_hash = a.content_hash
                AND {field} = ?)""")
            params.extend((key, int(match[2]) if match else filters.person[5:]))
    if filters.place:
        conditions.append("""EXISTS (SELECT 1 FROM story_segments AS t
            JOIN story_days AS d ON d.day = t.day
            JOIN story_state AS s ON s.key = d.key
            WHERE t.day = a.day AND t.title = ?
                AND ((t.started IS NULL AND a.captured_at IS NULL)
                     OR a.captured_at BETWEEN t.started AND t.ended))""")
        params.append(filters.place)
    for score, relation, lower, upper, key in (
        ("s.sharpness", "asset_scores", filters.min_sharpness, filters.max_sharpness, None),
        (
            "s.score",
            "insight_results",
            filters.min_aesthetic,
            filters.max_aesthetic,
            keys.get("aesthetic"),
        ),
    ):
        if lower is None and upper is None:
            continue
        if relation == "insight_results" and key is None:
            conditions.append("0")
            continue
        checks = ["s.asset_id = a.id"]
        if key is not None:
            checks += ["s.stage = 'aesthetic'", "s.key = ?", "s.content_hash = a.content_hash"]
            params.append(key)
        if lower is not None:
            checks.append(f"{score} >= ?")
            params.append(lower)
        if upper is not None:
            checks.append(f"{score} <= ?")
            params.append(upper)
        conditions.append(f"EXISTS (SELECT 1 FROM {relation} AS s WHERE {' AND '.join(checks)})")
    return conditions, params


def facets(libraries: Mapping[str, Path], settings: Settings) -> dict[str, list[dict]]:
    values: dict[str, dict[str, dict]] = {
        name: {} for name in ("tags", "people", "cameras", "lenses", "places")
    }

    def add(group: str, value: str, label: str, count: int, source=None, key=None):
        entry = values[group].setdefault(key or value, {"value": value, "label": label, "count": 0})
        entry["count"] += count
        if source:
            entry["source"] = source

    for name, path in libraries.items():
        with connect(path) as db:
            db.execute("BEGIN")
            for group, field in (("cameras", CAMERA), ("lenses", "a.lens")):
                rows = db.execute(f"""SELECT {field} AS label, count(*) AS count FROM assets AS a
                    WHERE a.active = 1 AND {field} IS NOT NULL AND {field} <> ''
                    GROUP BY label ORDER BY count DESC, label LIMIT 250""")
                for label, count in rows:
                    add(group, label, label, count)
            for normalized, label, count in db.execute("""
                SELECT t.name_key, min(t.name), count(*) FROM browser_tags AS t
                JOIN assets AS a ON a.id = t.asset_id WHERE a.active = 1
                GROUP BY t.name_key ORDER BY count(*) DESC, t.name_key LIMIT 250
            """):
                # Use the first catalog's spelling while merging normalized names.
                add("tags", "user:" + label, label, count, "user", "user:" + normalized)
            model = settings.ml
            if model.enabled and model.tags_enabled:
                vocabulary = model.vocabulary
                for tag, count in db.execute(
                    """
                    SELECT t.tag, count(*) FROM asset_tags AS t
                    JOIN asset_embeddings AS e ON e.asset_id = t.asset_id
                    JOIN assets AS a ON a.id = t.asset_id
                    WHERE a.active = 1 AND e.tag_key = ? AND e.content_hash = a.content_hash
                    GROUP BY t.tag
                """,
                    (model.tag_key,),
                ):
                    if tag in vocabulary:
                        add("tags", "auto:" + tag, " / ".join(vocabulary[tag]), count, "auto")
            if key := public_keys(db, settings).get("faces"):
                for id_, person, count in db.execute(
                    """
                    SELECT p.id, p.name, count(DISTINCT a.id) FROM people AS p
                    JOIN faces AS f ON f.person_id = p.id JOIN assets AS a ON a.id = f.asset_id
                    WHERE a.active = 1 AND f.key = ? AND f.content_hash = a.content_hash
                    GROUP BY p.id ORDER BY count(DISTINCT a.id) DESC, p.id LIMIT 250
                """,
                    (key,),
                ):
                    value = "name:" + person if person else f"id:{name}:{id_}"
                    add("people", value, person or f"{name} · {id_}", count)
            if settings.insights.story:
                for title, count in db.execute("""
                    SELECT t.title, sum(t.count) FROM story_segments AS t
                    JOIN story_days AS d ON d.day = t.day JOIN story_state AS s ON s.key = d.key
                    WHERE t.title IS NOT NULL GROUP BY t.title ORDER BY sum(t.count) DESC LIMIT 250
                """):
                    add("places", title, title, count)
    return {
        name: sorted(entries.values(), key=lambda item: (-item["count"], item["label"].casefold()))[
            :250
        ]
        for name, entries in values.items()
    }
