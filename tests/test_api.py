import asyncio
import json
import subprocess
import sys

from fastapi.testclient import TestClient

from photocull.api import _events, create_app
from photocull.config import Library, Settings
from photocull.db import connect, write
from photocull.media.cache import cache_path


def test_api_import_never_loads_decoders_or_ml():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import builtins
import sys

forbidden = {
    'onnxruntime', 'insightface', 'faster_whisper', 'torch', 'transformers',
    'rawpy', 'pyexiv2', 'PIL', 'numpy', 'pillow_heif'
}
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in forbidden:
        raise AssertionError(f'Unexpected API import: {name}')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import photocull.api
assert forbidden.isdisjoint({name.split('.')[0] for name in sys.modules})
assert 'photocull.worker' not in sys.modules
""",
        ],
        check=True,
    )


def test_browse_media_rating_progress_and_static(tmp_path):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<title>Photocull</title>")
    (static / "assets/app.js").write_text("void 0")
    settings = Settings(
        libraries={"sony": Library(root=tmp_path / "photos")},
        state_dir=tmp_path / "state",
        static_dir=static,
    )
    base = "/api/libraries/sony"
    state = settings.library_state("sony")
    assert settings.bind_host == "127.0.0.1"
    with TestClient(create_app(settings)) as client:
        assert client.get("/").text == "<title>Photocull</title>"
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/api/fs").status_code == 404
        assert client.get("/api/libraries").json() == {"items": [{"name": "sony"}]}
        assert client.get("/api/libraries/missing/assets").status_code == 404
        assert client.get("/api/health").json()["rss_mb"] > 0
        scan = client.post(f"{base}/scans").json()
        assert scan["status"] == "pending"
        assert client.post(f"{base}/scans").json()["id"] == scan["id"]
        with connect(state / "index.db") as db, write(db):
            db.executemany(
                """INSERT INTO assets (id, path, folder, kind, size, mtime_ns, content_hash,
                                       scan_id, day)
                   VALUES (?, ?, ?, 'image', 1, 0, 'hash', ?, ?)""",
                [
                    (7, "a.jpg", "", scan["id"], "2020-01-01"),
                    (8, "東京/b.jpg", "東京", scan["id"], "2025-03-04"),
                ],
            )
            db.execute(
                "INSERT INTO jobs (asset_id, stage, content_hash, status)"
                " VALUES (8, 'preview', 'hash', 'done')"
            )
        preview = cache_path(state, "previews", 8, "hash")
        preview.parent.mkdir()
        preview.write_bytes(b"cached preview")

        first = client.get(f"{base}/assets", params={"limit": 1}).json()
        assert first["items"][0]["id"] == 7
        second = client.get(f"{base}/assets", params={"cursor": first["next_cursor"]}).json()
        assert second["items"][0]["path"] == "東京/b.jpg"
        assert second["next_cursor"] is None
        assert client.get(f"{base}/assets", params={"folder": ""}).json()["items"][0]["id"] == 7
        by_day = client.get(f"{base}/assets", params={"day": "2025-03-04"}).json()
        assert by_day["items"][0]["id"] == 8
        groups = client.get(f"{base}/groups", params={"limit": 1}).json()
        assert groups == {"items": [{"name": "", "count": 1}], "next_cursor": ""}
        assert client.get(f"{base}/groups", params={"cursor": ""}).json()["items"] == [
            {"name": "東京", "count": 1}
        ]
        assert client.get(f"{base}/assets/8/media/detail").content == b"cached preview"
        assert client.get(f"{base}/assets/7/media/preview").status_code == 404

        rating = f"{base}/assets/8/rating"
        assert client.patch(rating, json={"rating": 4, "expected_revision": 0}).json() == {
            "rating": 4,
            "rejected": False,
            "revision": 1,
        }
        assert client.patch(rating, json={"rating": 0, "expected_revision": 0}).status_code == 409
        assert client.patch(
            rating, json={"rating": 0, "rejected": True, "expected_revision": 1}
        ).json()["rejected"]
        assert (
            client.patch(
                rating,
                json={"rating": 0, "expected_revision": 2},
                headers={"Origin": "https://external.example"},
            ).status_code
            == 403
        )
        assert client.get(f"{base}/assets/8").json()["revision"] == 2

        assert client.post(f"{base}/assets/8/jobs/preview").status_code == 202
        assert client.get(f"{base}/jobs").json()["stages"]["preview"] == {"pending": 1}

        async def initial_event():
            stream = _events(state / "index.db")
            try:
                return await anext(stream)
            finally:
                await stream.aclose()

        event = asyncio.run(initial_event())
        assert json.loads(event.removeprefix("data: "))["scan"]["id"] == scan["id"]


def test_optional_token_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("test-only-value\n")
    settings = Settings(state_dir=tmp_path / "state", token_file=token_file)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 401
        assert (
            client.get("/api/health", headers={"X-Photocull-Token": "test-only-value"}).status_code
            == 200
        )
