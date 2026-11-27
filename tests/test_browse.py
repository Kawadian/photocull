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


def test_cross_library_album_membership_and_saved_filters(timeline):
    albums = "/api/browse/albums"
    manual_response = timeline.post(albums, json={"name": "  Family trip  "})
    assert manual_response.status_code == 201
    manual = manual_response.json()
    assert manual["name"] == "Family trip"
    assert timeline.post(albums, json={"name": "family TRIP"}).status_code == 409
    endpoint = f"{albums}/{manual['id']}"
    selection = {
        "assets": [{"library": name, "id": asset} for name, asset in [("alpha", 1), ("zulu", 1)]]
    }
    member_request = endpoint + "/members"
    assert timeline.post(member_request, json=selection).json() == {"changed": 2}
    assert timeline.post(member_request, json=selection).json() == {"changed": 0}
    assert (
        timeline.get("/api/browse/alpha/assets/1/albums").json()["items"][0]["id"] == manual["id"]
    )
    assert timeline.get("/api/browse/alpha/assets/2/albums").json() == {"items": []}
    assert timeline.get(albums).json()["items"][0]["count"] == 2
    assert timeline.get(MONTHS, params={"album": manual["id"]}).json() == {"items": ["2025-04"]}

    params = {"album": manual["id"], "limit": 1}
    page = timeline.get(TIMELINE, params=params).json()
    assert [(item["library"], item["id"]) for item in page["items"]] == [("zulu", 1)]
    assert timeline.get(TIMELINE, params={"cursor": page["next_cursor"]}).status_code == 422
    page2 = timeline.get(TIMELINE, params=params | {"cursor": page["next_cursor"]}).json()
    assert [(item["library"], item["id"]) for item in page2["items"]] == [("alpha", 1)]
    assert timeline.post(member_request, json=selection | {"operation": "remove"}).json() == {
        "changed": 2
    }
    assert timeline.get(TIMELINE, params=params).json()["items"] == []

    smart_settings = {
        "kind": "smart",
        "name": "My favorites",
        "library": "alpha",
        "filters": {"favorite": True},
    }
    smart = timeline.post(albums, json=smart_settings).json()
    assert timeline.post(f"{albums}/{smart['id']}/members", json=selection).status_code == 422
    assert timeline.get(TIMELINE, params={"album": smart["id"]}).json()["items"] == []
    bulk = "/api/browse/selection/labels"
    body = {
        "assets": [asset | {"expected_revision": 0} for asset in selection["assets"]],
        "favorite": True,
        "rating": 4,
        "add_tags": ["Sunsets"],
    }
    assert timeline.patch(bulk, json=body).json()["items"] == [
        {"library": lib, "count": 1, "status": "done"} for lib in ("alpha", "zulu")
    ]
    assert timeline.patch(bulk, json=body).json()["items"] == [
        {"library": lib, "count": 1, "status": "conflict"} for lib in ("alpha", "zulu")
    ]
    smart_page = timeline.get(TIMELINE, params={"album": smart["id"], "library": "zulu"}).json()
    assert [(item["library"], item["id"]) for item in smart_page["items"]] == [("alpha", 1)]
    assert timeline.get(MONTHS, params={"album": smart["id"]}).json() == {"items": ["2025-04"]}
    for library in ("alpha", "zulu"):
        asset = timeline.get(f"/api/libraries/{library}/assets/1?details=browser").json()
        assert asset["revision"] == 1
        assert asset["rating"] == 4
        assert asset["user_tags"] == ["Sunsets"]

    update = smart_settings | {"filters": {"start": "2025-03-31"}, "expected_revision": 0}
    assert timeline.patch(f"{albums}/{smart['id']}", json=update).json()["revision"] == 1
    assert timeline.patch(f"{albums}/{smart['id']}", json=update).status_code == 409
    assert timeline.delete(f"{albums}/{smart['id']}?revision=0").status_code == 409
    assert timeline.delete(f"{albums}/{smart['id']}?revision=1").status_code == 204
    assert timeline.get(TIMELINE, params={"album": smart["id"]}).status_code == 404
    assert timeline.get(albums, headers={"X-Photocull-Token": ""}).status_code == 401


def test_batch_selection_reports_and_retries_independent_catalogs(timeline):
    bulk = "/api/browse/selection/labels"
    body = {
        "assets": [{"library": "alpha", "id": 1}, {"library": "zulu", "id": 900}],
        "favorite": True,
    }
    albums = "/api/browse/albums"
    album = timeline.post(albums, json={"name": "Source check"}).json()["id"]
    members = f"{albums}/{album}/members"
    assert timeline.post(members, json={"assets": body["assets"]}).status_code == 409
    assert timeline.get(TIMELINE, params={"album": album}).json()["items"] == []
    assert timeline.patch(bulk, json=body).json()["items"] == [
        {"library": "alpha", "count": 1, "status": "done"},
        {"library": "zulu", "count": 1, "status": "stale"},
    ]
    assert timeline.patch(bulk, json={"assets": body["assets"] * 101}).status_code == 422
    for asset_id in (1, 2, 3):
        labels = f"/api/browse/alpha/assets/{asset_id}/labels"
        for batch in range(3):
            tags = [f"tag-{i}" for i in range(batch * 22, min((batch + 1) * 22, 64))]
            assert timeline.patch(labels, json={"add_tags": tags}).status_code == 200
    body = {
        "assets": [{"library": "alpha", "id": i, "expected_revision": 0} for i in (1, 2)],
        "rating": 5,
        "add_tags": ["too-many"],
    }
    assert timeline.patch(bulk, json=body).json()["items"] == [
        {"library": "alpha", "count": 2, "status": "tag_limit"}
    ]
    assert timeline.get("/api/libraries/alpha/assets/1").json()["rating"] == 0


def test_batch_rating_uses_the_selection_revision_in_each_library(timeline):
    bulk = "/api/browse/selection/labels"
    items = timeline.get(TIMELINE).json()["items"]
    refs = {
        (item["library"], item["id"]): {
            "library": item["library"],
            "id": item["id"],
            "expected_revision": item["revision"],
        }
        for item in items
    }
    get_asset = "/api/libraries/alpha/assets/1"
    first = refs["alpha", 1]
    assert (
        timeline.patch(
            get_asset + "/rating",
            json={"rating": 4, "rejected": False, "expected_revision": first["expected_revision"]},
        ).status_code
        == 200
    )
    assert (
        timeline.patch(
            bulk, json={"assets": [{"library": "alpha", "id": 1}], "rating": 1}
        ).status_code
        == 422
    )
    assert timeline.patch(bulk, json={"assets": [first, first], "rating": 1}).status_code == 422
    body = {"assets": [first, refs["alpha", 2], refs["zulu", 1]], "rating": 1, "favorite": True}
    assert timeline.patch(bulk, json=body).json()["items"] == [
        {"library": "alpha", "count": 2, "status": "conflict"},
        {"library": "zulu", "count": 1, "status": "done"},
    ]
    assert timeline.get(get_asset).json()["rating"] == 4
    assert timeline.get("/api/libraries/alpha/assets/2").json()["rating"] == 0
    assert timeline.get("/api/libraries/zulu/assets/1").json()["rating"] == 1
    detail = timeline.get(get_asset, params={"details": "browser"}).json()
    assert not detail["favorite"]


def test_browser_migrates_existing_catalog_and_initializes_album_db(tmp_path):
    from photocull.db import MIGRATIONS, initialize

    settings = Settings(
        libraries={"alpha": Library(root=tmp_path / "photos")}, state_dir=tmp_path / "state"
    )
    path = settings.library_state("alpha") / "index.db"
    initialize(path, MIGRATIONS[:22])
    with connect(path) as db, write(db):
        db.execute("INSERT INTO scan_requests(id,status,created_at) VALUES(1,'done',0)")
        db.executemany(
            """INSERT INTO assets(id,path,folder,kind,size,mtime_ns,content_hash,scan_id,day,
                                  captured_at,active,rating)
               VALUES (?,'frame-' || ? || '.jpg','','image',64,0,?,1,'2025-01-02',?,?,4)""",
            ((1, 1, "a" * 64, "2025-01-02T03:04:05", 1), (2, 2, "b" * 64, None, 0)),
        )
        db.execute("""INSERT INTO jobs(asset_id,stage,content_hash,status,attempts)
                      SELECT id,'exif',content_hash,'done',1 FROM assets""")

    for startup in range(2):
        with TestClient(create_app(settings)) as client, connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            rows = db.execute("SELECT id, rating, timeline_time, favorite FROM assets ORDER BY id")
            assert [tuple(row) for row in rows] == [(1, 4, "2025-01-02T03:04:05", 0), (2, 4, "", 0)]
            rows = db.execute(
                "SELECT status,attempts FROM jobs WHERE stage='exif' ORDER BY asset_id"
            )
            assert [tuple(row) for row in rows] == [("pending", 0), ("done", 1)]
            stage_counts = dict(
                db.execute("SELECT status,count FROM stage_counts WHERE stage='exif'")
            )
            assert stage_counts == {"done": 1, "pending": 1}
            path_collections = settings.state_dir / "browser.db"
            with connect(path_collections) as browser:
                assert browser.execute("PRAGMA user_version").fetchone()[0] == 1
                assert browser.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            if startup == 0:
                result = client.post("/api/browse/albums", json={"name": "Migration album"})
                assert result.status_code == 201
            assert client.get("/api/browse/albums").json()["items"][0]["name"] == "Migration album"
            assert client.get(TIMELINE).json()["items"][0]["id"] == 1
