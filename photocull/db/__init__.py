from __future__ import annotations

import fcntl
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path

MIGRATIONS = (
    "0001_initial.sql",
    "0002_source_identity.sql",
    "0003_job_interruptions.sql",
    "0004_scores_and_bursts.sql",
    "0005_incremental_bursts.sql",
    "0006_embeddings.sql",
    "0007_ml_recovery.sql",
    "0008_incremental_similarity.sql",
    "0009_insights.sql",
    "0010_people.sql",
    "0011_stories.sql",
    "0012_video.sql",
    "0013_transfers.sql",
    "0014_face_index.sql",
    "0015_transfer_publication.sql",
    "0016_insight_inputs.sql",
    "0017_archive.sql",
)
BUSY_TIMEOUT_MS = 10_000


@contextmanager
def connect(path: Path, *, timeout_ms: int = BUSY_TIMEOUT_MS) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path, timeout=timeout_ms / 1000, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA foreign_keys = ON")
        yield db


@contextmanager
def write(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        if (
            db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
            and db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        ):
            return
    # Serialize first-time WAL setup and schema changes across API and worker startup.
    with path.with_suffix(".migration.lock").open("a") as lock, connect(path) as db:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db.execute("PRAGMA journal_mode = WAL")
        with write(db):
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise RuntimeError(f"{path} was created with a newer Photocull")
            directory = files(__package__).joinpath("migrations")
            for number, filename in enumerate(MIGRATIONS[version:], start=version + 1):
                statement = ""
                for line in directory.joinpath(filename).read_text(encoding="utf-8").splitlines():
                    statement += f"{line}\n"
                    if sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError(f"Incomplete migration: {filename}")
                db.execute(f"PRAGMA user_version = {number}")
