from concurrent.futures import ThreadPoolExecutor

from photocull.db import BUSY_TIMEOUT_MS, connect, initialize, write
from photocull.db.catalog import set_rating


def test_database_migrations_and_writer_concurrency(tmp_path):
    path = tmp_path / "library" / "index.db"
    initialize(path)
    initialize(path)
    with connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES (1, 'done', 0)")
        db.execute("""
            INSERT INTO assets (id, path, folder, kind, size, mtime_ns, content_hash, scan_id, day)
            VALUES (1, 'a.jpg', '', 'image', 1, 0, 'hash', 1, '1970-01-01')
            """)
        db.execute(
            "INSERT INTO jobs (asset_id, stage, content_hash, status)"
            " VALUES (1, 'exif', 'hash', 'pending')"
        )

    def ratings():
        with connect(path) as db:
            for revision in range(100):
                result = set_rating(db, 1, 4 + revision % 2, False, revision)
                assert result["revision"] == revision + 1

    def jobs():
        with connect(path) as db:
            for _ in range(100):
                with write(db):
                    db.execute("UPDATE jobs SET status = 'running', attempts = attempts + 1")
                    db.execute("UPDATE jobs SET status = 'done'")

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(ratings)
        second = pool.submit(jobs)
        first.result()
        second.result()

    with connect(path) as db:
        assert tuple(db.execute("SELECT rating, revision FROM assets").fetchone()) == (5, 100)
        assert tuple(db.execute("SELECT status, attempts FROM jobs").fetchone()) == ("done", 100)
        counts = db.execute("SELECT status, count FROM stage_counts WHERE count > 0").fetchall()
        assert [tuple(row) for row in counts] == [("done", 1)]
