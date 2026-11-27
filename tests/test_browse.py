import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import Library, Settings
from photocull.db import connect, write

TIMELINE = "/api/browse/timeline"
MONTHS = "/api/browse/months"


@pytest.fixture
def timeline(tmp_path):
    token = tmp_path / "token"
    token.write_text("fixture-token")
    settings = Settings(
        libraries={name: Library(root=tmp_path / name) for name in ("alpha", "zulu", "empty")},
        state_dir=tmp_path / "state",
        token_file=token,
    )
    with TestClient(create_app(settings), headers={"X-Photocull-Token": "fixture-token"}) as client:
        for library, assets in {
            "alpha": [
                (1, "2025-04-03", "2025-04-03T10:01:02", 1),
                (2, "2025-04-03", None, 1),
                (3, "2025-03-31", None, 1),
                (4, "2026-04-03", None, 0),
                (5, "2025-04-03", "2025-04-03T10:01:03", 1),
            ],
            "zulu": [
                (1, "2025-04-03", "2025-04-03T10:01:02", 1),
                (2, "2025-04-03", "2025-04-03T10:01:02", 1),
                (3, "2024-02-29", None, 1),
                (4, "2025-04-03", "2025-04-03T10:01:02.001", 1),
            ],
        }.items():
            with connect(settings.library_state(library) / "index.db") as db, write(db):
                db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
                db.executemany(
                    """INSERT INTO assets(id, path, folder, kind, size, mtime_ns, content_hash,
                                          scan_id, day, captured_at, active, width, height)
                       VALUES(?, 'nested/写真-' || ? || '.jpg', 'nested', 'image', 10, 0, ?, 1,
                              ?, ?, ?, 400, 300)""",
                    ((id_, id_, "f" * 64, day, time, active) for id_, day, time, active in assets),
                )
                db.execute("""
                    INSERT INTO jobs(asset_id,stage,content_hash,status)
                    SELECT id,'preview',content_hash,'done' FROM assets WHERE id = 1
                """)
        yield client


@pytest.mark.parametrize("limit", [1, 2, 3, 72])
def test_timeline_merges_libraries_with_keyset_ties(timeline, limit):
    items = []
    cursor = None
    for _ in range(10):
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = timeline.get(TIMELINE, params=params)
        response.raise_for_status()
        assert response.headers["Cache-Control"] == "private, no-cache"
        page = response.json()
        assert len(page["items"]) <= limit
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert [(item["library"], item["id"]) for item in items] == [
        ("alpha", 5),
        ("zulu", 4),
        ("zulu", 2),
        ("zulu", 1),
        ("alpha", 1),
        ("alpha", 2),
        ("alpha", 3),
        ("zulu", 3),
    ]
    assert items[4]["name"] == "写真-1.jpg"
    assert items[4]["preview_status"] == "done"
    assert items[4]["width"] == 400
    assert items[0]["preview_status"] is None
    assert "path" not in items[0]


def test_month_seek_scope_and_cursor_validation(timeline):
    assert timeline.get(MONTHS).json() == {"items": ["2025-04", "2025-03", "2024-02"]}
    assert timeline.get(MONTHS, params={"library": "alpha"}).json() == {
        "items": ["2025-04", "2025-03"]
    }
    assert timeline.get(MONTHS, params={"library": "empty"}).json() == {"items": []}
    response = timeline.get(TIMELINE, params={"month": "2025-03", "limit": 1}).json()
    assert [item["day"] for item in response["items"]] == ["2025-03-31"]
    cursor = response["next_cursor"]
    params = {"month": "2025-03", "cursor": cursor}
    assert timeline.get(TIMELINE, params=params).json()["items"][0]["day"] == "2024-02-29"
    for changes in (
        {"month": "2025-02"},
        {"library": "alpha"},
        {"cursor": "invalid"},
        {"cursor": "b" * 385},
        {"month": "2025-00"},
        {"month": "2025-13"},
        {"limit": 0},
        {"limit": 121},
    ):
        assert timeline.get(TIMELINE, params=params | changes).status_code == 422
    empty = timeline.get(TIMELINE, params={"month": "2024-01"}).json()
    assert empty["items"] == [] and empty["next_cursor"] is None
    assert timeline.get(TIMELINE, params={"library": "missing"}).status_code == 404
    assert timeline.get(MONTHS, params={"library": "../alpha"}).status_code == 422
    for path in (TIMELINE, MONTHS):
        assert timeline.get(path, headers={"X-Photocull-Token": ""}).status_code == 401
