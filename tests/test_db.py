from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files

from photocull.db import BUSY_TIMEOUT_MS, MIGRATIONS, connect, initialize, write
from photocull.db.bursts import choose_best, get_burst, undo_burst_reject
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


def test_phase_two_migration_preserves_existing_queue_and_ratings(tmp_path):
    path = tmp_path / "legacy.db"
    with connect(path) as db:
        for name in MIGRATIONS[:3]:
            db.executescript(files("photocull.db").joinpath("migrations", name).read_text())
        db.execute("PRAGMA user_version = 3")
        db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES (1, 'done', 0)")
        db.executemany(
            """INSERT INTO assets (id, path, folder, kind, size, mtime_ns, content_hash, scan_id,
                                   day, rating, active)
               VALUES (?, ?, '', ?, 8, 0, 'digest', 1, '2020-01-01', ?, ?)""",
            [
                (1, "old.jpg", "image", 5, 1),
                (2, "clip.mp4", "video", 0, 1),
                (3, "gone.jpg", "image", 0, 0),
            ],
        )
        db.execute(
            """INSERT INTO jobs (asset_id, stage, content_hash, status, attempts, interruptions)
               VALUES (1, 'preview', 'digest', 'done', 2, 1)"""
        )

    initialize(path)
    initialize(path)
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT rating FROM assets WHERE id = 1").fetchone()[0] == 5
        rows = db.execute(
            "SELECT asset_id, stage, status, attempts, interruptions FROM jobs ORDER BY 1, 2"
        )
        assert [tuple(row) for row in rows] == [
            (1, "preview", "done", 2, 1),
            (1, "score", "pending", 0, 0),
            (2, "score", "skipped", 0, 0),
        ]
        assert db.execute("SELECT sum(count) FROM stage_counts").fetchone()[0] == 3


def test_incremental_migration_preserves_existing_burst_decisions(tmp_path):
    path = tmp_path / "phase-two.db"
    with connect(path) as db:
        for name in MIGRATIONS[:4]:
            db.executescript(files("photocull.db").joinpath("migrations", name).read_text())
        db.execute("PRAGMA user_version = 4")
        db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES (1, 'done', 0)")
        db.executemany(
            """INSERT INTO assets (id, path, folder, kind, size, mtime_ns, content_hash,
                                   scan_id, day, captured_at)
               VALUES (?, ?, '', 'image', 8, 0, 'digest', 1,
                       '2020-01-01', '2020-01-01T00:00:00')""",
            [(1, "one.jpg"), (2, "two.jpg")],
        )
        key = "a" * 64
        db.execute("INSERT INTO bursts VALUES (1, 1, ?)", (key,))
        db.executemany("INSERT INTO burst_members VALUES (?, 1)", [(1,), (2,)])
        original = choose_best(db, 1, 2, key)
        token = "a" * 32
        db.execute("UPDATE assets SET burst_reject_token = ? WHERE id = 1", (token,))

    initialize(path)
    with connect(path) as db:
        assert get_burst(db, 1) == original
        assert db.execute("SELECT count(*) FROM burst_changes").fetchone()[0] == 0
        assert undo_burst_reject(db, token) == 1


def test_ml_upgrade_preserves_existing_vectors_tags_and_queued_searches(tmp_path):
    path = tmp_path / "phase-three.db"
    with connect(path) as db:
        for name in MIGRATIONS[:6]:
            db.executescript(files("photocull.db").joinpath("migrations", name).read_text())
        db.execute("PRAGMA user_version = 6")
        db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES (1, 'done', 0)")
        db.executemany(
            """INSERT INTO assets (id, path, folder, kind, size, mtime_ns, content_hash,
                                   scan_id, day)
               VALUES (?, ?, '', 'image', 8, 0, 'digest', 1, '2020-01-01')""",
            [(1, "a.jpg"), (2, "b.jpg")],
        )
        db.execute("INSERT INTO embedding_models VALUES ('model', 'source', 'rev', '{}')")
        db.execute("UPDATE ml_state SET enabled = 1, model_key = 'model'")
        db.executemany(
            "INSERT INTO asset_embeddings VALUES (?, 'model', 'digest', ?, 'tags')",
            [(1, b"\x00\x3c"), (2, b"\x00\x3c")],
        )
        db.execute("INSERT INTO asset_tags VALUES (1, 'cat', 1)")
        db.execute("INSERT INTO similar_groups VALUES (1, 2)")
        db.executemany("INSERT INTO similar_members VALUES (?, 1)", [(1,), (2,)])
        db.execute(
            """INSERT INTO search_requests
               (id,model_key,query,limit_results,created_at,status)
               VALUES ('request', 'model', 'cat', 60, 0, 'pending')"""
        )

    initialize(path)
    with connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM asset_embeddings").fetchone()[0] == 2
        assert db.execute("SELECT tag FROM asset_tags").fetchone()[0] == "cat"
        assert db.execute("SELECT attempts FROM search_requests").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM similar_groups").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM similar_dirty").fetchone()[0] == 2
        assert db.execute("SELECT length(anchor_revision) FROM ml_state").fetchone()[0] == 32
