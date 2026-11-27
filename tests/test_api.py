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
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

forbidden = {
    'onnxruntime', 'insightface', 'faster_whisper', 'torch', 'torchvision',
    'open_clip', 'transformers', 'tokenizers', 'usearch', 'numkong', 'huggingface_hub',
    'faiss', 'hnswlib', 'rawpy', 'pyexiv2', 'PIL', 'numpy', 'pillow_heif', 'cv2', 'imagehash',
    'ctranslate2', 'watchdog', 'av',
}
original = builtins.__import__
def guarded(name, *args, **kwargs):
    namespace = (args[0] if args else kwargs.get('globals')) or {}
    importer = namespace.get('__name__', '')
    archive = importer.startswith(
        ('photocull.archive', 'photocull.api.archive', 'photocull.db.archive')
    )
    if name.split('.')[0] in forbidden or (archive and (
        name.split('.')[0] in {'subprocess', 'multiprocessing', 'ctypes', 'resource'}
        or name.startswith('photocull.worker')
    )):
        raise AssertionError(f'Unexpected API import from {importer}: {name}')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
def no_processes(event, args):
    if event in {'subprocess.Popen', 'os.exec', 'os.system', 'os.posix_spawn'}:
        raise AssertionError(f'Unexpected process launch from API: {event}')
sys.addaudithook(no_processes)
import photocull.api
from photocull.config import ArchiveSettings, InsightSettings, Library, MLSettings, Settings
with TemporaryDirectory() as directory:
    state = Path(directory)
    settings = Settings(
        state_dir=state / 'state', libraries={'photos': Library(root=state / 'media')},
        ml=MLSettings(enabled=True),
        archive=ArchiveSettings(export_roots={'photos': state / 'export'}),
        insights=InsightSettings(
            aesthetic=True, taste=True, faces=True, video=True, transcript=True,
            story=True, report=True, export=True, watch=True,
        ),
    )
    with TestClient(photocull.api.create_app(settings)) as client:
        base = '/api/libraries/photos'
        assert client.get('/api/browse/timeline').json()['items'] == []
        assert client.get('/api/browse/months').json()['items'] == []
        assert client.get(f'{base}/jobs').json()['ml']['waiting']
        assert client.get(f'{base}/tags').status_code == 200
        assert client.get(f'{base}/similar-groups').json()['items'] == []
        assert client.get(f'{base}/insights').json()['transcript']
        assert client.get(f'{base}/insights/people').json()['items'] == []
        assert client.get(f'{base}/insights/stories').json()['items'] == []
        assert client.post(f'{base}/insights/exports', json={'kind': 'zip'}).status_code == 202
        assert client.post(f'{base}/search', json={'query': '猫'}).status_code == 503
        assert client.post(f'{base}/assets/1/similar', json={}).status_code == 503
        archive = f'{base}/archive'
        assert client.get(archive).json()['destructive'] is False
        preview = client.post(f'{archive}/preview', json={'operation': 'copy'})
        assert preview.status_code == 202
        id_ = preview.json()['id']
        assert client.get(f'{archive}/previews/{id_}').json()['status'] == 'pending'
        assert client.get(f'{archive}/rules').json()['items'] == []
        assert client.post(f'{archive}/selftest').status_code == 409
assert forbidden.isdisjoint({name.split('.')[0] for name in sys.modules})
assert not any(name.startswith('photocull.worker') for name in sys.modules)
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


def test_score_filters_burst_best_and_reversible_flags(tmp_path):
    settings = Settings(
        libraries={"photos": Library(root=tmp_path / "media")}, state_dir=tmp_path / "state"
    )
    base = "/api/libraries/photos"
    path = settings.library_state("photos") / "index.db"
    group_key = "1" * 64
    with TestClient(create_app(settings)) as client:
        with connect(path) as db, write(db):
            db.execute("INSERT INTO scan_requests (id, status, created_at) VALUES (1, 'done', 0)")
            db.executemany(
                """INSERT INTO assets (id, path, folder, kind, size, mtime_ns, content_hash,
                                       scan_id, day, captured_at, rating, rejected)
                   VALUES (?, ?, 'trip', 'image', 1, 0, 'hash', 1, '2024-01-01',
                           '2024-01-01T00:00:00', ?, ?)""",
                [
                    (1, "a.jpg", 4, 0),
                    (2, "b.jpg", 5, 0),
                    (3, "c.jpg", 0, 1),
                    (4, "d.jpg", 0, 0),
                    (5, "e.jpg", 0, 0),
                    (6, "f.jpg", 0, 0),
                ],
            )
            db.executemany(
                """INSERT INTO asset_scores (asset_id, sharpness, exposure, clipped_highlights,
                                             clipped_shadows, dhash, suggested_reject_reason)
                   VALUES (?, ?, ?, 0, 0, '0000000000000000', ?)""",
                [
                    (1, 0.9, 0.5, None),
                    (2, 0.8, 0.6, None),
                    (3, 0.01, 0.5, "blur"),
                    (4, 0.7, 0.4, None),
                    (5, 0.2, 0.02, "underexposed"),
                ],
            )
            db.executemany(
                "INSERT INTO bursts VALUES (?, ?, ?)", [(1, 1, group_key), (5, 5, "2" * 64)]
            )
            db.executemany(
                "INSERT INTO burst_members VALUES (?, ?)",
                [(1, 1), (2, 1), (3, 1), (4, 1), (5, 5), (6, 5)],
            )

        filtered = client.get(
            f"{base}/assets",
            params={
                "min_sharpness": 0.7,
                "min_exposure": 0.4,
                "max_exposure": 0.6,
                "auto_reject": "false",
                "limit": 1,
                "folder": "trip",
            },
        ).json()
        assert filtered["next_cursor"] == 1
        assert filtered["items"][0]["sharpness"] == 0.9
        assert not filtered["items"][0]["auto_reject"]
        assert filtered["items"][0]["burst_id"] == 1
        assert client.get(f"{base}/assets", params={"min_sharpness": 1.1}).status_code == 422
        assert client.get(f"{base}/assets", params={"cursor": 2**63}).status_code == 422
        assert client.get(f"{base}/assets/{2**63}").status_code == 422
        assert client.get(f"{base}/bursts/{2**63}").status_code == 422
        assert client.get(f"{base}/groups", params={"by": "captured_at"}).status_code == 422
        assert client.get(f"{base}/assets", params={"folder": "Aki's shoot"}).json()["items"] == []
        assert (
            client.get(
                f"{base}/assets",
                params={
                    "min_exposure": 0.7,
                    "max_exposure": 0.2,
                },
            ).status_code
            == 422
        )
        assert (
            client.get(f"{base}/assets", params={"burst_id": 1, "cursor": 2}).json()["items"][1][
                "id"
            ]
            == 4
        )

        bursts = client.get(f"{base}/bursts", params={"auto_reject": "true", "limit": 1}).json()
        assert bursts["next_cursor"] == 1
        assert bursts["items"][0]["recommended_best_id"] == 1
        assert client.get(f"{base}/bursts", params={"cursor": 1}).json()["items"][0]["id"] == 5
        assert (
            client.get(f"{base}/bursts", params={"min_sharpness": 0.8}).json()["items"][0][
                "member_count"
            ]
            == 4
        )

        best = f"{base}/bursts/1/best"
        assert (
            client.patch(best, json={"asset_id": 5, "membership_key": group_key}).status_code == 422
        )
        assert (
            client.patch(
                best,
                json={
                    "asset_id": 2,
                    "membership_key": "0" * 64,
                },
            ).status_code
            == 409
        )
        chosen = client.patch(best, json={"asset_id": 2, "membership_key": group_key}).json()
        assert (chosen["user_best_id"], chosen["best_id"], chosen["recommended_best_id"]) == (
            2,
            2,
            1,
        )
        assert client.get(f"{base}/bursts/1").json() == chosen

        reject = f"{base}/bursts/1/reject-rest"
        assert (
            client.post(reject, json={"best_id": 1, "membership_key": group_key}).status_code == 409
        )
        result = client.post(reject, json={"best_id": 2, "membership_key": group_key}).json()
        assert result["changed"] == 2
        assert client.post(reject, json={"best_id": 2, "membership_key": group_key}).json() == {
            "changed": 0,
            "undo_token": None,
        }
        flagged = client.get(f"{base}/assets", params={"burst_id": 1, "auto_reject": "true"}).json()
        assert [
            (item["id"], item["auto_reject_reason"], item["rating"], item["rejected"])
            for item in flagged["items"]
        ] == [
            (1, "burst_remaining", 4, False),
            (3, "blur", 0, True),
            (4, "burst_remaining", 0, False),
        ]

        assert client.delete(f"{base}/assets/4/auto-reject").json() == {"changed": 1}
        undo = f"{base}/burst-rejections/{result['undo_token']}"
        assert client.delete(f"{base}/burst-rejections/invalid").status_code == 422
        assert client.delete(f"{base}/burst-rejections/{'0' * 32}").json() == {"changed": 0}
        assert client.delete(undo).json() == {"changed": 1}
        assert client.delete(undo).json() == {"changed": 0}
        assert client.get(f"{base}/assets/3").json()["auto_reject"]
        assert client.delete(f"{base}/auto-reject").json() == {"changed": 2}
        assert client.get(f"{base}/assets", params={"auto_reject": "true"}).json()["items"] == []
        assert client.get(f"{base}/assets/3").json()["rejected"]
        assert client.get(f"{base}/assets/1").json()["rating"] == 4
        assert client.get(f"{base}/assets/2").json()["rating"] == 5
