from random import Random

import pytest

from photocull.config import BurstSettings
from photocull.db import connect, initialize, write
from photocull.db.bursts import choose_best, get_burst
from photocull.worker.bursts import refresh_bursts


def _asset(db, asset_id, timestamp, fingerprint):
    db.execute(
        """INSERT INTO assets (id, path, folder, kind, size, mtime_ns, content_hash,
                               scan_id, day, captured_at)
           VALUES (?, ?, '', 'image', 1, 0, ?, 1, '2025-01-01', ?)""",
        (asset_id, f"{asset_id}.jpg", f"hash-{asset_id}", timestamp),
    )
    db.execute(
        """INSERT INTO asset_scores (asset_id, sharpness, exposure, clipped_highlights,
                                     clipped_shadows, dhash)
           VALUES (?, 0.5, 0.5, 0, 0, ?)""",
        (asset_id, f"{fingerprint:016x}"),
    )


def _members(db):
    return {row["asset_id"]: row["burst_id"] for row in db.execute("SELECT * FROM burst_members")}


def test_dense_capture_time_hamming_boundary_and_stable_tie(tmp_path):
    path = tmp_path / "index.db"
    initialize(path)
    timestamp = "2025-01-01T00:00:00"
    rng = Random(7)
    with connect(path) as db:
        with write(db):
            db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES (1, 'done', 0)")
            _asset(db, 1, timestamp, 0)
            _asset(db, 2, timestamp, 0xFFFF)
            for asset_id in range(3, 500):
                _asset(db, asset_id, timestamp, rng.getrandbits(64))
            _asset(db, 500, timestamp, 0xFF)  # Distance 8 to both, oldest anchor wins.
            _asset(db, 501, timestamp, (1 << 32) | 0xFF)  # Distance 9 to both.
            _asset(db, 502, None, 0)  # Matching pixels and file day, no capture time.

        assert refresh_bursts(db, BurstSettings(), lambda: None)
        groups = _members(db)
        assert groups[500] == 1
        assert 501 not in groups and 502 not in groups
        assert refresh_bursts(db, BurstSettings(max_hamming_distance=9), lambda: None)
        assert _members(db)[501] == 1
        assert 502 not in _members(db)


def test_incremental_rebuild_is_scoped_resumable_and_matches_full_build(tmp_path):
    path = tmp_path / "index.db"
    initialize(path)
    settings = BurstSettings()
    with connect(path) as db:
        with write(db):
            db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES (1, 'done', 0)")
            for asset_id, second in enumerate((0, 1, 3, 4, 20, 21, 34, 35), 1):
                _asset(db, asset_id, f"2025-01-01T00:00:{second:02}", 0)
        assert refresh_bursts(db, settings, lambda: None)
        retained = get_burst(db, 5)
        retained = choose_best(db, 5, 6, retained["membership_key"])
        untouched = get_burst(db, 7)
        with write(db):
            # Deleting the bridge and moving another frame affect old and new regions.
            db.execute("UPDATE assets SET active = 0 WHERE id = 2")
            db.execute("UPDATE assets SET captured_at = '2025-01-01T00:00:22' WHERE id = 4")
            _asset(db, 9, "2025-01-01T00:00:02", 0)
        assert get_burst(db, 7) == untouched

    with connect(path) as db:
        dirty = db.execute("SELECT count(*) FROM burst_changes").fetchone()[0]

        def interrupted():
            raise InterruptedError("worker stopped")

        with pytest.raises(InterruptedError):
            refresh_bursts(db, settings, interrupted)
        assert db.execute("SELECT count(*) FROM burst_changes").fetchone()[0] == dirty

        fetched_times = []
        new_choice = None

        def change_best_during_build():
            nonlocal new_choice
            if new_choice is None:
                with connect(path) as api_db:
                    new_choice = choose_best(api_db, 5, 5, retained["membership_key"])

        db.set_trace_callback(fetched_times.append)
        assert refresh_bursts(db, settings, change_best_during_build)
        db.set_trace_callback(None)
        candidate_queries = [sql for sql in fetched_times if "AS quality" in sql]
        assert candidate_queries
        assert all("'2025-01-01T00:00:35'" not in sql for sql in candidate_queries)
        assert get_burst(db, 7) == untouched
        assert new_choice and get_burst(db, 5)["best_id"] == new_choice["best_id"] == 5
        assert db.execute("SELECT count(*) FROM burst_changes").fetchone()[0] == 0
        memberships = _members(db)
        assert memberships == {1: 1, 9: 1, 4: 5, 5: 5, 6: 5, 7: 7, 8: 7}
        snapshots = [tuple(row) for row in db.execute("SELECT * FROM bursts ORDER BY id")]

        db.execute("UPDATE analysis_state SET bursts_dirty = 1")
        assert refresh_bursts(db, settings, lambda: None)
        assert _members(db) == memberships
        assert [tuple(row) for row in db.execute("SELECT * FROM bursts ORDER BY id")] == snapshots
