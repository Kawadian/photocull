from __future__ import annotations

import base64
import io
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photocull.api import create_app
from photocull.config import (
    Library,
    MLSettings,
    OpenAISettings,
    Settings,
    VLMSettings,
    load_settings,
)
from photocull.db import connect, write
from photocull.vlm import OpenAIProvider, ProviderError, endpoint
from photocull.worker import run

BASE = "/api/libraries/photos"
QUALITY = {
    "sharpness": 0.13,
    "exposure": 0.6,
    "clipped_highlights": 0.02,
    "clipped_shadows": 0.01,
    "reason": "blur",
}
TAGS = [{"tag": "cat", "score": 0.9}]


@dataclass
class Request:
    path: str
    body: dict
    authorization: str | None

    @property
    def content(self):
        return self.body["messages"][0]["content"]

    @property
    def prompt(self):
        return self.content[0]["text"]

    @property
    def image(self):
        uri = self.content[1]["image_url"]["url"]
        return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))


def classify(request: Request) -> tuple[int, str]:
    if '{"ok":true}' in request.prompt:
        return 200, '{"ok":true}'
    result = {}
    if "`tags`:" in request.prompt:
        result["tags"] = TAGS
    if "`flags`:" in request.prompt:
        result["flags"] = QUALITY
    return 200, json.dumps(result)


@dataclass
class Mock:
    base_url: str
    calls: list[Request]
    reply: Callable[[Request], tuple[int, str]] = classify

    def config(self, **overrides) -> OpenAISettings:
        return OpenAISettings.model_validate(
            {"base_url": self.base_url, "model": "test-vision", "requests_per_minute": 600}
            | overrides
        )


@pytest.fixture
def compatible():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request = Request(self.path, body, self.headers.get("Authorization"))
            service.calls.append(request)
            status, content = service.reply(request)
            data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            if status == 307:
                self.send_header("Location", "/untrusted")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        service = Mock(f"http://127.0.0.1:{server.server_port}/gateway/v1/", [])
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield service
        finally:
            server.shutdown()
            thread.join(timeout=2)


def write_jpeg(path):
    tags = Image.Exif()
    tags[306] = "2024:01:02 03:04:05"
    tags[34853] = {1: "N", 2: (2.0, 3.0, 4.0), 3: "E", 4: (5.0, 6.0, 7.0)}
    with Image.new("RGB", (1310, 970), "#333333") as image:
        image.save(path, quality=90, exif=tags, comment=b"private", icc_profile=b"private")


def test_normalize_compatible_endpoint(compatible):
    root = compatible.base_url.removesuffix("/gateway/v1/")
    for prefix in ("", "/", "/v1/", "/v1/chat/completions/", "/gateway/v1"):
        expected = "/gateway/v1/chat/completions" if "gateway" in prefix else "/v1/chat/completions"
        assert endpoint(compatible.config(base_url=root + prefix)).path == expected
    assert endpoint(compatible.config(base_url=root + "/chat/completions/")).path == (
        "/chat/completions"
    )
    for prefix in ("?key=private", "#private", "http://user:private@"):
        with pytest.raises(ProviderError, match="^invalid_configuration$"):
            endpoint(compatible.config(base_url=prefix + root))


def test_cache_privacy_and_attempt_budgets(tmp_path, compatible, monkeypatch):
    preview = tmp_path / "preview.jpg"
    write_jpeg(preview)
    with Image.open(preview) as source:
        assert source.getexif().get_ifd(34853)
        assert "icc_profile" in source.info
    key = "fixture-only"
    monkeypatch.setenv("PHOTOCULL_VLM_API_KEY", key)
    settings = compatible.config(max_requests_per_run=1, max_requests_per_day=2)
    remote = OpenAIProvider(tmp_path, settings)
    other_process = OpenAIProvider(tmp_path, settings)
    ml = MLSettings()
    with ThreadPoolExecutor(3) as pool:
        jobs = [
            pool.submit(client.analyze, "same", preview, ("tags", "flags"), ml)
            for client in (remote, remote, remote)
        ]
        outcomes = [job.result() for job in jobs]
    assert all(value.model_dump() == {"tags": TAGS, "flags": QUALITY} for value in outcomes)
    assert len(compatible.calls) == 1
    call = compatible.calls[0]
    assert call.path == "/gateway/v1/chat/completions"
    assert call.authorization == f"Bearer {key}"
    with call.image as sent:
        assert sent.format == "JPEG" and max(sent.size) == 768
        assert not sent.getexif()
        assert set(sent.info).isdisjoint({"exif", "icc_profile", "comment", "xmp"})
    assert preview.name not in json.dumps(call.body)
    assert remote.analyze("same", preview, ("flags",), ml).flags is not None
    with pytest.raises(ProviderError, match="budget_exhausted"):
        remote.analyze("other", preview, ("tags",), ml)
    assert other_process.analyze("other", preview, ("tags",), ml).tags
    restarted = OpenAIProvider(tmp_path, settings)
    with pytest.raises(ProviderError, match="budget_exhausted"):
        restarted.analyze("third", preview, ("tags",), ml)
    assert len(compatible.calls) == 2
    assert restarted.analyze("same", preview, ("tags",), ml).tags
    assert len(compatible.calls) == 2


def test_backoff_invalid_response_and_redirect(tmp_path, compatible):
    preview = tmp_path / "preview.jpg"
    write_jpeg(preview)
    settings = compatible.config(max_requests_per_run=8)
    remote = OpenAIProvider(tmp_path, settings)
    sequence = iter([429, 503, 200])

    def transient(request):
        status = next(sequence)
        return classify(request) if status == 200 else (status, "private service response")

    compatible.reply = transient
    started = time.monotonic()
    assert remote.analyze("first", preview, ("flags",), MLSettings()).flags
    assert time.monotonic() - started >= 0.75
    assert len(compatible.calls) == 3

    for raw in (
        '{"tags":[{"tag":"unknown","score":0.9}]}',
        '{"tags":[{"tag":"cat","score":true}]}',
        '{"tags":[],"tags":[]}',
        '{"tags":[],"flags":null}',
    ):
        compatible.reply = lambda _, raw=raw: (200, raw)
        with pytest.raises(ProviderError, match="^invalid_response$"):
            remote.analyze("second", preview, ("tags",), MLSettings())
    compatible.reply = lambda _: (307, "private service response")
    with pytest.raises(ProviderError, match="^request_rejected$"):
        remote.analyze("second", preview, ("tags",), MLSettings())
    assert len(compatible.calls) == 8
    assert {call.path for call in compatible.calls} == {"/gateway/v1/chat/completions"}


def test_environment_key_and_configuration_are_private(tmp_path, compatible, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PHOTOCULL_VLM_API_KEY", raising=False)
    (tmp_path / ".env").write_text("PHOTOCULL_VLM_API_KEY=fixture-dotenv\n")
    (tmp_path / "photocull.yaml").write_text("vlm:\n  flags: openai\n")
    monkeypatch.setenv("PHOTOCULL_VLM_BASE_URL", compatible.base_url)
    monkeypatch.setenv("PHOTOCULL_VLM_MODEL", "test-vision")
    settings = load_settings()
    assert settings.vlm.flags == "openai" and settings.vlm.tags == "local"
    assert compatible.base_url not in repr(settings)
    assert compatible.base_url not in settings.model_dump_json()
    remote = OpenAIProvider(tmp_path, settings.vlm.openai)
    assert remote.test_connection() == {"ok": True, "endpoint": "http://***"}
    assert compatible.calls[0].authorization is None
    with compatible.calls[0].image as sent:
        sent.verify()

    (tmp_path / "photocull.yaml").write_text("vlm:\n  openai:\n    api_key: fixture-yaml\n")
    with pytest.raises(ValueError, match="^Invalid VLM settings in Photocull configuration$"):
        load_settings()


def until(action):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        result = action()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for the worker")


def test_provider_workflow_and_manual_edits(tmp_path, compatible, monkeypatch):
    monkeypatch.delenv("PHOTOCULL_VLM_API_KEY", raising=False)
    root = tmp_path / "test-photos"
    root.mkdir()
    photo = root / "test.jpg"
    write_jpeg(photo)
    before = (photo.read_bytes(), photo.stat().st_mtime_ns)
    settings = Settings(
        state_dir=tmp_path / "state",
        libraries={"photos": Library(root=root)},
        vlm=VLMSettings(openai=compatible.config(), flags="openai"),
    )
    with TestClient(create_app(settings)) as client, ThreadPoolExecutor(1) as pool:
        stop = Event()
        worker = pool.submit(run, settings, stop)
        try:
            assert client.get(f"{BASE}/providers").json()["selection"] == {
                "tags": "local",
                "flags": "openai",
            }
            invalid = client.put(
                f"{BASE}/providers", json={"tags": "openai", "key": "fixture-only"}
            )
            assert invalid.status_code == 422 and "fixture-only" not in invalid.text
            client.post(f"{BASE}/scans").raise_for_status()
            asset_url = f"{BASE}/assets/1"

            def openai_flags():
                asset = client.get(asset_url).json()
                return asset if asset.get("flag_provider") == "openai" else None

            asset = until(openai_flags)
            assert asset["auto_reject_reason"] == "blur" and asset["sharpness"] == 0.13
            assert client.get(
                f"{BASE}/assets", params={"auto_reject": True, "min_sharpness": 0.1}
            ).json()["items"]
            assert client.get(
                "/api/browse/timeline", params={"flag": "automatic", "min_sharpness": 0.1}
            ).json()["items"]
            assert asset.get("tag_provider") is None
            assert "`tags`" not in compatible.calls[0].prompt
            client.patch(
                f"{asset_url}/rating", json={"rating": 5, "expected_revision": 0}
            ).raise_for_status()
            client.patch(
                "/api/browse/photos/assets/1/labels",
                json={"add_tags": ["Manual"], "favorite": True},
            ).raise_for_status()
            assert client.delete(f"{asset_url}/auto-reject").json()["changed"] == 1
            client.put(
                f"{BASE}/providers", json={"tags": "openai", "flags": "local"}
            ).raise_for_status()
            reanalysis = f"{BASE}/providers/reanalyze"
            assert (
                client.post(reanalysis, json={"kind": "tags", "end": "2024-01-01"}).json()["queued"]
                == 0
            )
            assert (
                client.post(reanalysis, json={"kind": "flags", "scope": "local"}).json()["queued"]
                == 0
            )
            assert (
                client.post(reanalysis, json={"kind": "flags", "scope": "openai"}).json()["queued"]
                == 1
            )
            assert (
                client.post(
                    reanalysis, json={"kind": "tags", "scope": "unjudged", "start": "2024-01-02"}
                ).json()["queued"]
                == 1
            )

            def finish():
                photo = client.get(asset_url, params={"details": "browser"}).json()
                return photo if photo.get("tag_provider") == "openai" else None

            result = until(finish)
            assert result["flag_provider"] == "local"
            assert result["rating"] == 5 and result["favorite"]
            assert result["user_tags"] == ["Manual"]
            assert not result["auto_reject"]
            assert [tag["tag"] for tag in result["tags"]] == ["cat"]
            assert "`flags`" not in compatible.calls[1].prompt
            assert client.get(f"{BASE}/assets", params={"tag": "cat"}).json()["items"]
            assert client.get("/api/browse/timeline", params={"tag": "auto:cat"}).json()["items"]
            assert client.post(f"{BASE}/providers/test").json() == {
                "ok": True,
                "endpoint": "http://***",
            }
            assert client.delete(f"{BASE}/providers").status_code == 204
            assert client.get(f"{BASE}/providers").json()["selection"]["tags"] == "local"

            with connect(settings.library_state("photos") / "index.db") as db, write(db):
                db.execute("UPDATE assets SET content_hash = 'changed' WHERE id = 1")
                assert not db.execute("SELECT 1 FROM decision_tag_results").fetchone()
                assert not db.execute("SELECT 1 FROM decision_tags").fetchone()
        finally:
            stop.set()
    assert worker.result() == 0
    assert before == (photo.read_bytes(), photo.stat().st_mtime_ns)


@pytest.mark.parametrize(("policy", "provider"), [("unjudged", "unjudged"), ("local", "local")])
def test_budget_outcomes(tmp_path, compatible, policy, provider):
    root = tmp_path / "test-photos"
    root.mkdir()
    write_jpeg(root / "test.jpg")
    settings = Settings(
        state_dir=tmp_path / "state",
        libraries={"photos": Library(root=root)},
        vlm=VLMSettings(
            openai=compatible.config(max_requests_per_run=0),
            flags="openai",
            on_failure=policy,
        ),
    )
    with TestClient(create_app(settings)) as client, ThreadPoolExecutor(1) as pool:
        stop = Event()
        worker = pool.submit(run, settings, stop)
        try:
            scan = client.post(f"{BASE}/scans").json()
            path = settings.library_state("photos") / "index.db"

            def decision():
                with connect(path) as db:
                    return db.execute(
                        """SELECT 1 FROM assets
                           WHERE NOT EXISTS (SELECT 1 FROM decision_jobs)
                           AND EXISTS (
                               SELECT 1 FROM scan_requests WHERE id = ? AND status = 'done'
                           )
                           AND (vlm_flag_provider IS NOT NULL OR EXISTS (
                               SELECT 1 FROM asset_scores WHERE asset_id = assets.id
                           ))""",
                        (scan["id"],),
                    ).fetchone()

            assert until(decision)
            asset = client.get(f"{BASE}/assets/1").json()
            assert asset["flag_provider"] == provider
            assert (asset["sharpness"] is None) == (provider == "unjudged")
            assert not compatible.calls
        finally:
            stop.set()
    assert worker.result() == 0
