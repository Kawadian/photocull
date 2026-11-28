from __future__ import annotations

import sqlite3
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from photocull.config import DecisionKind, ProviderSelection
from photocull.db.catalog import mark_catalog_updated


class Reanalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: DecisionKind | Literal["both"]
    scope: Literal["all", "unjudged", "local", "openai"] = "all"
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def dates_in_order(self) -> Reanalysis:
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start date must be on or before end date")
        return self


def selection(db: sqlite3.Connection, defaults: ProviderSelection) -> ProviderSelection:
    row = db.execute("SELECT tags, flags FROM provider_selection WHERE id = 1").fetchone()
    return ProviderSelection(**dict(row)) if row else defaults


def select(db: sqlite3.Connection, value: ProviderSelection) -> None:
    db.execute(
        """INSERT INTO provider_selection VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET tags = excluded.tags, flags = excluded.flags""",
        (value.tags, value.flags),
    )
    cancel_remote(db, value)
    mark_catalog_updated(db)


def cancel_remote(db: sqlite3.Connection, providers: ProviderSelection) -> int:
    return db.execute(
        """DELETE FROM decision_jobs WHERE provider = 'openai'
           AND ((kind = 'tags' AND ? = 'local') OR (kind = 'flags' AND ? = 'local'))""",
        (providers.tags, providers.flags),
    ).rowcount


def tags_available(
    db: sqlite3.Connection, defaults: ProviderSelection, local_enabled: bool
) -> bool:
    return (
        local_enabled
        or selection(db, defaults).tags == "openai"
        or db.execute(
            """SELECT 1 FROM decision_tag_results
               WHERE provider IN ('openai', 'unjudged') LIMIT 1"""
        ).fetchone()
        is not None
    )


def enqueue(
    db: sqlite3.Connection, query: Reanalysis, providers: ProviderSelection, tag_key: str
) -> int:
    provider = getattr(providers, query.kind)
    if query.kind == "tags":
        joins = """LEFT JOIN asset_embeddings e ON e.asset_id = a.id
                   LEFT JOIN decision_tag_results d ON d.asset_id = a.id
                     AND d.content_hash = a.content_hash AND d.tag_key = :tag_key"""
        source = """coalesce(d.provider,
                    CASE WHEN e.tag_key = :tag_key AND e.content_hash = a.content_hash
                         THEN 'local' ELSE 'unjudged' END)"""
    else:
        joins = "LEFT JOIN asset_scores s ON s.asset_id = a.id"
        source = """coalesce(a.vlm_flag_provider,
                    CASE WHEN s.asset_id IS NULL THEN 'unjudged' ELSE 'local' END)"""
    count = db.execute(
        f"""INSERT INTO decision_jobs(asset_id, kind, provider, content_hash)
            SELECT a.id, :kind, :provider, a.content_hash FROM assets a
            JOIN jobs p ON p.asset_id = a.id AND p.stage = 'preview' AND p.status = 'done'
                AND p.content_hash = a.content_hash
            {joins}
            WHERE a.active = 1 AND a.kind <> 'video'
                AND (:start IS NULL OR a.day >= :start) AND (:end IS NULL OR a.day <= :end)
                AND (:scope = 'all' OR :scope = {source})
            ON CONFLICT(asset_id, kind) DO UPDATE SET provider = excluded.provider,
                content_hash = excluded.content_hash
            WHERE decision_jobs.provider <> excluded.provider
                OR decision_jobs.content_hash <> excluded.content_hash""",
        {
            **query.model_dump(mode="json"),
            "provider": provider,
            "tag_key": tag_key,
        },
    ).rowcount
    if count:
        mark_catalog_updated(db)
    return count


def enqueue_preview(
    db: sqlite3.Connection,
    asset_id: int,
    digest: str,
    providers: ProviderSelection,
    tag_key: str,
) -> None:
    if providers.tags == "openai":
        inserted = db.execute(
            """INSERT INTO decision_jobs
               SELECT :id, 'tags', 'openai', :digest
               WHERE NOT EXISTS (SELECT 1 FROM decision_tag_results WHERE asset_id = :id)
                 AND NOT EXISTS (SELECT 1 FROM asset_embeddings WHERE asset_id = :id
                       AND content_hash = :digest AND tag_key = :tags)
               ON CONFLICT DO NOTHING""",
            {"id": asset_id, "digest": digest, "tags": tag_key},
        ).rowcount
        if inserted:
            db.execute(
                "INSERT INTO decision_tag_results VALUES (?, 'unjudged', ?, ?)",
                (asset_id, digest, tag_key),
            )
    if providers.flags == "openai":
        inserted = db.execute(
            """INSERT INTO decision_jobs
               SELECT id, 'flags', 'openai', content_hash FROM assets
               WHERE id = ? AND content_hash = ? AND vlm_flag_provider IS NULL
                 AND NOT EXISTS (SELECT 1 FROM asset_scores WHERE asset_id = assets.id)
               ON CONFLICT DO NOTHING""",
            (asset_id, digest),
        ).rowcount
        if inserted:
            db.execute("UPDATE assets SET vlm_flag_provider = 'unjudged' WHERE id = ?", (asset_id,))
