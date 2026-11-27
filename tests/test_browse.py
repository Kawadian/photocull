import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import InsightSettings, Library, Settings
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
        insights=InsightSettings(story=True),
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
                if library == "alpha":
                    db.execute("""UPDATE assets SET make='Sony',model='A7',lens='FE 35 mm',
                                   rating=4 WHERE id = 5""")
                    db.execute("""
                        INSERT INTO asset_scores
                        VALUES (5,.72,.6,0,0,'3333333333333333',NULL),
                               (3,.12,.4,0,0,'2222222222222222','soft_focus')
                    """)
                    db.execute("UPDATE assets SET rejected = 1 WHERE id = 2")
                    db.execute("UPDATE story_state SET key = 'fixture'")
                    db.execute("INSERT INTO story_days VALUES ('2025-04-03',2,1,'fixture')")
                    db.execute("""
                        INSERT INTO story_segments
                        VALUES ('2025-04-03',1,'Kyoto','2025-04-03T09:00:00',
                                '2025-04-03T12:00:00',35,135,2,'[]')
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


def test_library_labels_facets_and_filtered_cursors(timeline):
    def labels(library, asset_id):
        return f"/api/browse/{library}/assets/{asset_id}/labels"

    for library, id_, spelling in (("alpha", 5, "Trip"), ("zulu", 4, "TRIP")):
        route = labels(library, id_)
        change = {"favorite": True, "add_tags": [spelling, "旅"]}
        for _ in range(2):
            assert timeline.patch(route, json=change).json() == {
                "favorite": True,
                "user_tags": [spelling, "旅"],
            }
    assert timeline.patch(labels("alpha", 4), json={"favorite": True}).status_code == 404
    assert timeline.patch(labels("alpha", 5), json={"add_tags": [" ”bad\nname"]}).status_code == 422
    assert (
        timeline.patch(
            labels("alpha", 5),
            json={
                "add_tags": ["Trip"],
                "remove_tags": ["TRIP"],
            },
        ).status_code
        == 422
    )

    combined = {"favorite": True, "tag": "user:trip", "start": "2025-04-01"}
    page = timeline.get(TIMELINE, params=combined | {"limit": 1}).json()
    assert [(item["library"], item["id"]) for item in page["items"]] == [("alpha", 5)]
    assert page["items"][0]["favorite"]
    next_page = timeline.get(TIMELINE, params=combined | {"cursor": page["next_cursor"]}).json()
    assert [(item["library"], item["id"]) for item in next_page["items"]] == [("zulu", 4)]
    assert (
        timeline.get(
            TIMELINE,
            params=combined
            | {
                "cursor": page["next_cursor"],
                "tag": "user:旅",
            },
        ).status_code
        == 422
    )
    scope = timeline.get(
        TIMELINE,
        params=combined
        | {
            "camera": "Sony A7",
            "lens": "FE 35 mm",
            "min_rating": 3,
            "min_sharpness": 0.7,
            "max_sharpness": 0.8,
            "place": "Kyoto",
        },
    ).json()
    assert [item["id"] for item in scope["items"]] == [5]
    for params, expected in (
        ({"flag": "manual"}, [2]),
        ({"flag": "automatic"}, [3]),
        ({"kind": "video"}, []),
        ({"camera": "' OR 1=1 --"}, []),
    ):
        assert [
            item["id"]
            for item in timeline.get(
                TIMELINE,
                params={
                    **params,
                    "library": "alpha",
                },
            ).json()["items"]
        ] == expected
    for params in (
        {"min_rating": 5, "max_rating": 2},
        {"start": "2026-09-01", "end": "2026-08-31"},
        {"person": "id:alpha:9999999999999999999"},
        {"tag": "wrong:tag"},
    ):
        assert timeline.get(TIMELINE, params=params).status_code == 422

    facets = timeline.get("/api/browse/facets").json()
    assert facets["tags"] == [
        {"label": "Trip", "value": "user:Trip", "source": "user", "count": 2},
        {"label": "旅", "value": "user:旅", "source": "user", "count": 2},
    ]
    assert facets["cameras"] == [{"label": "Sony A7", "value": "Sony A7", "count": 1}]
    assert facets["places"] == [{"label": "Kyoto", "value": "Kyoto", "count": 2}]
    assert timeline.get("/api/browse/facets", headers={"X-Photocull-Token": ""}).status_code == 401
    updated = timeline.patch(
        labels("alpha", 5),
        json={
            "favorite": False,
            "remove_tags": ["TRIP"],
        },
    ).json()
    assert updated == {"favorite": False, "user_tags": ["旅"]}
    assert [
        item["library"] for item in timeline.get(TIMELINE, params=combined).json()["items"]
    ] == ["zulu"]
