import base64
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photocull.api import create_app
from photocull.config import InsightSettings, Library, MLSettings, Settings, WorkerSettings
from photocull.db import connect
from photocull.insights import models
from photocull.worker.insights.story import distance
from tests.fake_embedding import FakeEmbedding
from tests.fake_faces import FakeFaces
from tests.worker_harness import photo, start_worker, wait_for

BASE = "/api/libraries/test"
URL = f"{BASE}/insights"


def make_settings(tmp_path, **options):
    root = tmp_path / "photos"
    root.mkdir()
    return Settings(
        libraries={"test": Library(root=root)},
        state_dir=tmp_path / "state",
        worker=WorkerSettings(memory_mb=3072),
        insights=InsightSettings(**options),
    )


def payload(response):
    response.raise_for_status()
    return response.json()


def get(client, path, **options):
    return payload(client.get(path, **options))


def scan(client):
    job = payload(client.post(f"{BASE}/scans"))
    wait_for(
        lambda: (
            (row := get(client, f"{BASE}/jobs")["scan"])
            and row["id"] >= job["id"]
            and row["status"] == "done"
        )
    )


def set_rating(client, asset, rating, rejected=False):
    return payload(
        client.patch(
            f"{BASE}/assets/{asset['id']}/rating",
            json={
                "rating": rating,
                "rejected": rejected,
                "expected_revision": asset["revision"],
            },
        )
    )


def test_people_aesthetic_and_learned_taste_survive_restarts(tmp_path):
    settings = make_settings(
        tmp_path, aesthetic=True, taste=True, faces=True, taste_min_per_class=2, offline=True
    )
    settings = settings.model_copy(
        update={"ml": MLSettings(enabled=True, offline=True, tags_enabled=False)}
    )
    root = settings.libraries["test"].root
    for index in range(6):
        color = (250, 80, 10) if index % 2 == 0 else (10, 80, 250)
        photo(root / f"{index}.jpg", color, f"01 10:00:0{index}")
    state = settings.library_state("test")
    with (
        TestClient(create_app(settings)) as client,
        start_worker(
            settings,
            model_factory=FakeEmbedding,
            face_factory=FakeFaces,
        ),
    ):
        scan(client)
        wait_for(lambda: len(get(client, f"{BASE}/assets?sort=aesthetic")["items"]) == 6)
        first = get(client, f"{BASE}/assets?sort=aesthetic&limit=2")
        second = get(client, f"{BASE}/assets?sort=aesthetic&limit=2&cursor={first['next_cursor']}")
        assert [item["path"] for item in first["items"] + second["items"]] == [
            "0.jpg",
            "2.jpg",
            "4.jpg",
            "1.jpg",
        ]
        assert len(get(client, f"{BASE}/assets?sort=aesthetic&min_aesthetic=0.75")["items"]) == 3
        assert get(client, URL)["taste"]["status"] in {"pending", "insufficient"}

        assets = get(client, f"{BASE}/assets")["items"]
        wait_for(
            lambda: (
                (items := get(client, f"{URL}/people")["items"])
                and sum(item["count"] for item in items) == 6
            )
        )
        people = get(client, f"{URL}/people")["items"]
        assert len(people) == 2
        left, right = (person["id"] for person in people)
        assert get(client, f"{BASE}/assets?person_id={left}")["items"][0]["path"] == "0.jpg"
        payload(client.patch(f"{URL}/people/{left}", json={"name": "Mika"}))
        faces = get(client, f"{URL}/people/{left}/faces")["items"]
        assert faces[0]["right"] == 0.6
        assert "vector" not in faces[0]
        split = payload(
            client.post(
                f"{URL}/people/{left}/split",
                json={
                    "faces": [faces[0]["id"]],
                    "name": "Split",
                },
            )
        )["id"]
        assert (
            client.post(
                f"{URL}/people/{left}/split",
                json={
                    "faces": [faces[0]["id"]],
                },
            ).status_code
            == 409
        )
        assert get(client, f"{BASE}/assets?person_id={split}")["items"][0]["id"] == assets[0]["id"]
        payload(client.post(f"{URL}/people/{left}/merge", json={"source": right}))
        assert len(get(client, f"{BASE}/assets?person_id={left}")["items"]) == 5
        for index, item in enumerate(assets[:4]):
            set_rating(client, item, 5 if index % 2 == 0 else 0, rejected=index % 2 == 1)
        payload(client.post(f"{URL}/taste/train"))
        wait_for(lambda: get(client, URL)["taste"]["status"] == "ready", timeout=20)

        def all_recommendations():
            items = get(client, f"{BASE}/assets?sort=taste")["items"]
            return items if len(items) == 6 else None

        ranked = wait_for(all_recommendations)
        assert ranked[0]["path"] in {"0.jpg", "2.jpg", "4.jpg"}
        assert ranked[0]["insights"]["taste"]["factors"][0]["feature"] == "Image embedding"
        assert ranked[-1]["insights"]["taste"]["score"] < ranked[0]["insights"]["taste"]["score"]

    revision = "0" * 40
    settings = settings.model_copy(
        update={
            "insights": settings.insights.model_copy(update={"face_revision": revision}),
        }
    )
    with (
        TestClient(create_app(settings)) as client,
        start_worker(
            settings,
            model_factory=FakeEmbedding,
            face_factory=FakeFaces,
        ),
    ):
        wait_for(lambda: len(get(client, f"{BASE}/assets?person_id={left}")["items"]) == 5)
        assert get(client, f"{URL}/people")["items"][0]["name"] == "Mika"
        wait_for(lambda: len(get(client, f"{BASE}/assets?person_id={split}")["items"]) == 1)
        with connect(state / "index.db") as db:
            assert (
                db.execute(
                    "SELECT revision FROM insight_models WHERE key = ?",
                    (models(settings)["faces"].key,),
                ).fetchone()[0]
                == revision
            )
            assert db.execute("SELECT count(*) FROM faces").fetchone()[0] == 6


def gps_photo(path, stamp, lat, lon):
    exif = Image.Exif()
    exif[34665] = {36867: stamp}
    exif[34853] = {1: "N", 2: (lat, 0, 0), 3: "E", 4: (lon, 0, 0)}
    with Image.new("RGB", (80, 50), (100, 80, 35)) as image:
        image.save(path, exif=exif)


def test_day_stories_report_and_keeper_outputs(tmp_path):
    export = tmp_path / "destination"
    export.mkdir()
    geonames = tmp_path / "cities.txt"
    geonames.write_text("1\tTown & <West>\tTown\t\t35\t139\tP\tPPL\tJP\n", encoding="utf-8")
    settings = make_settings(
        tmp_path,
        story=True,
        report=True,
        export=True,
        story_geonames=geonames,
        export_roots={"archive": export},
    )
    root = settings.libraries["test"].root
    gps_photo(root / "日本.jpg", "2025:01:01 10:00:00", 35, 139)
    gps_photo(root / "away.jpg", "2025:01:01 10:34:00", 36, 139)
    gps_photo(root / "late.jpg", "2025:01:01 13:00:00", 36, 139)
    (root / "日本.xmp").write_text("<xmp>existing</xmp>", encoding="utf-8")
    originals = {path.name: path.read_bytes() for path in root.iterdir()}
    with TestClient(create_app(settings)) as client, start_worker(settings):
        scan(client)
        items = wait_for(
            lambda: (
                (data if len(data) == 3 and all(x["representatives"] for x in data) else None)
                if (data := get(client, f"{URL}/stories/2025-01-01")["items"])
                else None
            )
        )
        assert items[0]["title"] == "Town & <West>, JP"
        assert items[1]["title"] is None
        assert items[1]["latitude"] == 36
        assert distance((0, 179.98), (0, -179.98)) < 5
        keeper = next(
            item for item in get(client, f"{BASE}/assets")["items"] if item["path"] == "日本.jpg"
        )
        set_rating(client, keeper, 5)
        assert (
            client.post(f"{URL}/exports", json={"kind": "copy", "destination": "../"}).status_code
            == 422
        )
        assert (
            client.post(
                f"{URL}/exports", json={"kind": "copy", "destination": "unknown"}
            ).status_code
            == 422
        )

        def completed(kind, job_id):
            for job in get(client, f"{URL}/transfers?kind={kind}")["items"]:
                if job["id"] == job_id:
                    assert job["status"] != "error", job["error"]
                    return job if job["status"] == "done" else None
            return None

        job = payload(
            client.post(
                f"{URL}/exports",
                json={
                    "kind": "zip",
                    "xmp": True,
                    "include_location": True,
                    "selection": {"start": "2025-01-01", "end": "2025-01-01"},
                },
            )
        )["id"]
        assert wait_for(lambda: completed("export", job))["processed"] == 1
        download = client.get(f"{URL}/transfers/{job}/download")
        download.raise_for_status()
        assert download.headers["content-type"] == "application/zip"
        prefix = client.get(f"{URL}/transfers/{job}/download", headers={"Range": "bytes=0-3"})
        assert prefix.status_code == 206 and prefix.content == b"PK\x03\x04"
        with ZipFile(BytesIO(download.content)) as archive:
            assert any(name.endswith(".xmp") for name in archive.namelist())
            assert {archive.read(name) for name in archive.namelist()} == {
                originals["日本.jpg"],
                originals["日本.xmp"],
            }

        for kind, path, body in (
            (
                "export",
                "exports",
                {"kind": "copy", "destination": "archive", "xmp": True, "include_location": True},
            ),
            ("report", "reports", {"start": "2025-01-01"}),
            ("report", "reports", {"start": "2025-01-01", "include_location": True}),
            ("report", "reports", {"start": "2025-01-01", "include_location": False}),
        ):
            job = payload(client.post(f"{URL}/{path}", json=body))["id"]
            result = wait_for(lambda kind=kind, job=job: completed(kind, job))
            if kind == "export":
                files = list((export / job).iterdir())
                assert {file.read_bytes() for file in files} == {
                    originals["日本.jpg"],
                    originals["日本.xmp"],
                }
            else:
                report = client.get(f"{URL}/transfers/{job}/download")
                report.raise_for_status()
                include = body.get("include_location", False)
                assert result["include_location"] is include
                for value in (
                    "Town &amp; &lt;West&gt;, JP",
                    "36.000, 139.000",
                    "GeoNames",
                    "日本.jpg",
                ):
                    assert (value in report.text) is include
                thumbnails = re.findall(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", report.text)
                assert thumbnails
                for encoded in thumbnails:
                    with Image.open(BytesIO(base64.b64decode(encoded))) as thumbnail:
                        assert not thumbnail.getexif().get_ifd(34853)
                assert report.headers["Cache-Control"] == "private, no-store"
                assert "Content-Security-Policy" in report.headers
        assert {path.name: path.read_bytes() for path in root.iterdir()} == originals


def test_transfer_cancellation_and_symlink_allowlist(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    settings = make_settings(tmp_path, export=True, export_roots={"archive": allowed})
    library = settings.libraries["test"].root
    photo(library / "target.jpg", "green", "01 10:00:00")
    with TestClient(create_app(settings)) as client:
        job = payload(client.post(f"{URL}/exports", json={"kind": "zip"}))["id"]
        assert payload(client.delete(f"{URL}/transfers/{job}"))["status"] == "cancelled"
        assert client.get(f"{URL}/transfers/{job}/download").status_code == 409
        assert client.delete(f"{URL}/transfers/{job}").status_code == 409
        allowed.rmdir()
        allowed.symlink_to(library, target_is_directory=True)
        with start_worker(settings):
            scan(client)
            job = payload(
                client.post(
                    f"{URL}/exports",
                    json={
                        "kind": "copy",
                        "destination": "archive",
                        "selection": {"minimum_rating": 0},
                    },
                )
            )["id"]
            wait_for(
                lambda: any(
                    item["id"] == job and item["status"] == "error"
                    for item in get(client, f"{URL}/transfers?kind=export")["items"]
                )
            )
        assert list(library.iterdir()) == [library / "target.jpg"]


@pytest.mark.parametrize("mode", ["poll", "auto"])
def test_watch_discovers_changes_without_manually_scanning(tmp_path, mode):
    settings = make_settings(
        tmp_path, watch=True, watch_mode=mode, watch_interval=1, watch_debounce=0.2
    )
    root = settings.libraries["test"].root
    with TestClient(create_app(settings)) as client, start_worker(settings):
        temp = root / "new.partial"
        photo(root / "new.jpg", "red", "01 10:00:00")
        (root / "new.jpg").rename(temp)
        temp.rename(root / "new.jpg")
        found = wait_for(lambda: get(client, f"{BASE}/assets")["items"])
        assert found[0]["path"] == "new.jpg"
        prior = found[0]["content_hash"]
        photo(root / "new.jpg", "blue", "01 10:00:00")
        wait_for(lambda: get(client, f"{BASE}/assets")["items"][0]["content_hash"] != prior)
        (root / "new.jpg").unlink()
        wait_for(lambda: not get(client, f"{BASE}/assets")["items"])


def test_insights_can_be_disabled_without_affecting_review(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        assert get(client, URL)["taste"]["status"] == "disabled"
        assert client.get(f"{URL}/people").status_code == 409
        assert client.get(f"{URL}/stories").status_code == 409
        assert client.post(f"{URL}/reports", json={}).status_code == 409
        assert client.post(f"{URL}/exports", json={"kind": "zip"}).status_code == 409
        assert get(client, f"{BASE}/assets")["items"] == []


def test_aesthetic_weight_changes_burst_recommendation(tmp_path):
    from photocull.config import BurstSettings
    from photocull.db import initialize, write
    from photocull.db.insights import finish, register
    from photocull.worker.bursts import refresh_bursts

    db_path = Path(tmp_path) / "index.db"
    initialize(db_path)
    timestamp = datetime(2025, 1, 1, tzinfo=UTC).isoformat()
    settings = Settings(ml=MLSettings(enabled=True), insights=InsightSettings(aesthetic=True))
    with connect(db_path) as db, write(db):
        db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES(1, 'done', 0)")
        key = register(db, models(settings)["aesthetic"])
        for i in (1, 2):
            db.execute(
                """INSERT INTO assets (id,path,folder,kind,size,mtime_ns,content_hash,
                          scan_id,day,captured_at) VALUES(?, ?, '', 'image', 1, 0, ?, 1, ?, ?)""",
                (i, f"{i}.jpg", str(i), "2025-01-01", timestamp),
            )
            db.execute(
                """INSERT INTO asset_scores
                          (asset_id,sharpness,exposure,clipped_highlights,clipped_shadows,dhash)
                          VALUES(?, ?, 0.5, 0, 0, '0000000000000000')""",
                (i, 1 - i * 0.3),
            )
            finish(db, "aesthetic", key, {"id": i, "content_hash": str(i)}, score=i - 1)
    with connect(db_path) as db:
        refresh_bursts(db, BurstSettings(aesthetic_weight=0), lambda: None, aesthetic_key=key)
        assert db.execute("SELECT recommended_best_id FROM bursts").fetchone()[0] == 1
        refresh_bursts(db, BurstSettings(aesthetic_weight=0.8), lambda: None, aesthetic_key=key)
        assert db.execute("SELECT recommended_best_id FROM bursts").fetchone()[0] == 2


def test_cancel_running_copy_and_recover_staged_files(tmp_path):
    from types import SimpleNamespace

    from photocull.db import initialize
    from photocull.db.transfers import ExportRequest, Selection, cancel, enqueue
    from photocull.media.walk import source_key
    from photocull.worker.insights.transfers import Transfers

    target = tmp_path / "archive"
    target.mkdir()
    settings = make_settings(tmp_path, export=True, export_roots={"archive": target})
    library = settings.libraries["test"]
    source = library.root / "keeper.jpg"
    data = b"sample" * 500_000
    source.write_bytes(data)
    state = settings.library_state("test")
    index = state / "index.db"
    initialize(index)
    config = ExportRequest(
        kind="copy", destination="archive", selection=Selection(minimum_rating=0)
    )
    with connect(index) as db:
        db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES(1, 'done', 0)")
        db.execute(
            """INSERT INTO assets(path,folder,kind,size,mtime_ns,content_hash,
                                  scan_id,day,source_key)
               VALUES(?, '', 'image', ?, 0, 'test', 1, '2025-01-01', ?)""",
            (source.name, len(data), source_key(source.stat())),
        )
        transfers = Transfers(
            SimpleNamespace(db=db, state=state, library=library, settings=settings)
        )
        job = enqueue(db, "copy", config)
        pulses = 0

        def cancel_during_copy():
            nonlocal pulses
            pulses += 1
            if pulses == 3:
                cancel(db, job)

        transfers.step(cancel_during_copy)
        assert (
            db.execute("SELECT status FROM transfers WHERE id=?", (job,)).fetchone()[0]
            == "cancelled"
        )
        assert list(target.iterdir()) == []
        assert source.read_bytes() == data

        interrupted = enqueue(db, "copy", config)
        stage = target / f".{interrupted}.part"
        stage.mkdir()
        (stage / "partial.jpg").write_bytes(b"partial")
        db.execute("UPDATE transfers SET status='running', attempts=1 WHERE id=?", (interrupted,))
        transfers.recover()
        assert not stage.exists()
        assert (
            db.execute("SELECT status FROM transfers WHERE id=?", (interrupted,)).fetchone()[0]
            == "pending"
        )
        transfers.step(lambda: None)
        assert next((target / interrupted).iterdir()).read_bytes() == data
