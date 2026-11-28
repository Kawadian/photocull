from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from secrets import token_urlsafe
from threading import Barrier

from fastapi.testclient import TestClient

from photocull.api import TOKEN_HEADER, create_app
from photocull.settings_store import FIELDS, SettingsStore
from photocull.vlm import OpenAIProvider

URL = "/api/settings"
TRUSTED = "https://vlm.example.test/v1"


def deployment(tmp_path, *, token=True, env=None):
    library = tmp_path / "photos"
    library.mkdir()
    scratch = tmp_path / "volumes"
    scratch.mkdir()
    token_file = tmp_path / "admin"
    key = token_urlsafe(24)
    token_file.write_text(key)
    config = tmp_path / "photocull.yaml"
    config.write_text(
        "# Editable operator YAML\nlibraries:\n  photos:\n    root: photos\nstate_dir: state\n"
        + ("token_file: admin\n" if token else "")
    )
    store = SettingsStore(
        {
            "libraries": {"photos": {"root": "photos"}},
            "state_dir": "state",
            "server": {"allowed_hosts": ["testserver"]},
            **({"token_file": "admin"} if token else {}),
        },
        config,
        tmp_path,
        {
            "PHOTOCULL_SETTINGS_ROOTS": str(scratch),
            "PHOTOCULL_SETTINGS_ENDPOINTS": TRUSTED,
            **(env or {}),
        },
    )
    return store, {TOKEN_HEADER: key}, library, scratch


def change(client, fields, revision="base", preview=False):
    payload = {"revision": revision, "changes": fields}
    if preview:
        return client.post(f"{URL}/preview", json=payload)
    return client.put(URL, json=payload)


def test_schema_admin_host_and_origin(tmp_path):
    store, headers, *_ = deployment(tmp_path)
    with TestClient(create_app(settings_store=store)) as client:
        assert client.get(URL).status_code == 401
        assert client.post(f"{URL}/paths", json={"field": "libraries"}).status_code == 401
        assert client.get(URL, headers=headers | {"Host": "untrusted.test"}).status_code == 421
        client.headers.update(headers)
        result = client.get(URL)
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["revision"] == "base"
        assert result.json()["api_key_configured"] is False
        assert result.json()["fields"].keys() == FIELDS.keys()
        assert (
            client.put(URL, headers={"Origin": "https://untrusted.test"}, json={}).status_code
            == 403
        )
        assert (
            client.post(
                f"{URL}/paths",
                headers={"Sec-Fetch-Site": "cross-site"},
                json={"field": "libraries"},
            ).status_code
            == 403
        )
        assert client.put(URL, data={"changes": "{}"}).status_code == 415

    other = tmp_path / "unauthenticated"
    other.mkdir()
    no_admin, *_ = deployment(other, token=False)
    with TestClient(create_app(settings_store=no_admin)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get(URL).status_code == 403


def test_schema_and_japanese_field_metadata_match():
    frontend = Path(__file__).resolve().parents[1] / "frontend/src/settingsFields.ts"
    keys = re.findall(r"\['([a-z_.]+)',", frontend.read_text())
    assert sorted(keys) == sorted(FIELDS)


def test_validation_save_history_and_restore(tmp_path):
    store, headers, *_ = deployment(tmp_path)
    original = store.config.read_bytes()
    with TestClient(create_app(settings_store=store), headers=headers) as client:
        assert change(client, {"worker.scan_parallelism": 9}).status_code == 422
        assert (
            change(client, {"scoring.min_exposure": 0.9, "scoring.max_exposure": 0.1}).status_code
            == 422
        )
        assert change(client, {"unexpected.key": "invalid"}).status_code == 422
        assert change(client, {"network.egress_allow": ["*.example.test"]}).status_code == 422
        response = change(client, {"browser.strip_location": False}, preview=True)
        assert response.json()["changes"] == [
            {"key": "browser.strip_location", "before": True, "after": False}
        ]
        assert not store.active.exists()
        saved = change(client, {"browser.strip_location": False}).json()
        revision = saved["revision"]
        assert saved["fields"]["browser.strip_location"]["value"] is False
        assert saved["fields"]["browser.strip_location"]["running"] is True
        assert saved["fields"]["browser.strip_location"]["pending"] is True
        assert store.read().settings.browser.strip_location is False
        assert store.config.read_bytes() == original
        backup = store.backups / f"{saved['history'][0]}.json"
        assert backup.stat().st_mode & 0o777 == 0o600
        assert json.loads(backup.read_bytes())["overrides"] == {}
        assert change(client, {"browser.strip_location": True}).status_code == 409

        request = {"revision": revision, "backup": backup.stem}
        preview = client.post(f"{URL}/history/preview", json=request).json()
        assert preview["changes"][0]["after"] is True
        restored = client.post(f"{URL}/history/restore", json=request).json()
        assert restored["revision"] != revision
        assert restored["fields"]["browser.strip_location"]["pending"] is False
        assert len(restored["history"]) == 2
        previous = store.previous(store.read(), restored["history"][0])
        assert previous.settings.browser.strip_location is False
        assert (
            client.post(
                f"{URL}/history/restore",
                json={"revision": restored["revision"], "backup": "../active"},
            ).status_code
            == 422
        )


def test_parallel_save_accepts_one_revision(tmp_path):
    store, headers, *_ = deployment(tmp_path)
    gate = Barrier(2)
    with TestClient(create_app(settings_store=store), headers=headers) as client:

        def save(value):
            gate.wait(timeout=10)
            return change(client, {"worker.batch_size": value}).status_code

        with ThreadPoolExecutor(max_workers=2) as threads:
            assert sorted(threads.map(save, [12, 16])) == [200, 409]
    assert len(store.history(store.read())) == 1


def test_paths_roots_permissions_and_symlinks(tmp_path):
    store, headers, library, scratch = deployment(tmp_path)
    inner = library / "album"
    inner.mkdir()
    file = inner / "places.txt"
    file.write_text("test contents should never be returned")
    (library / "internal").symlink_to(inner, target_is_directory=True)
    (library / "external").symlink_to(scratch, target_is_directory=True)
    (inner / "device").symlink_to("/dev/null")
    # An independently approved mount must not make a link from a media mount traversable.
    with TestClient(create_app(settings_store=store), headers=headers) as client:
        browse = f"{URL}/paths"
        check = f"{browse}/check"
        paths = client.post(browse, json={"field": "libraries"}).json()["roots"]
        assert str(library) in paths and str(scratch) in paths
        items = client.post(browse, json={"field": "libraries", "path": str(library)}).json()[
            "entries"
        ]
        assert [item["name"] for item in items] == ["album", "internal"]
        assert client.post(
            check, json={"field": "libraries", "path": str(library / "internal")}
        ).json() == {"path": str(inner)}
        for raw in (
            str(library / "../volumes"),
            str(library / "%2e%2e"),
            str(library / "%252e%252e"),
            "/etc",
            str(library / "external"),
            str(inner / "device"),
            str(tmp_path / "admin"),
        ):
            assert (
                client.post(
                    check, json={"field": "insights.story_geonames", "path": raw}
                ).status_code
                == 422
            )
        items = client.post(
            browse, json={"field": "insights.story_geonames", "path": str(inner)}
        ).json()["entries"]
        assert [item["name"] for item in items] == [file.name]
        assert (
            client.post(
                check, json={"field": "insights.story_geonames", "path": str(file)}
            ).status_code
            == 200
        )
        assert client.post(check, json={"field": "libraries", "path": str(file)}).status_code == 422
        assert (
            client.post(check, json={"field": "model_dir", "path": str(scratch)}).json()[
                "free_bytes"
            ]
            >= 0
        )
        assert change(client, {"model_dir": str(inner)}).status_code == 422
        assert change(client, {"state_dir": str(inner)}).status_code == 422
        assert change(client, {"model_dir": str(scratch / "missing")}).status_code == 422
        assert change(client, {"insights.story_geonames": str(file)}).status_code == 200
        assert file.read_text() == "test contents should never be returned"


def test_secret_redaction_and_environment_precedence(tmp_path, monkeypatch):
    key = token_urlsafe(24)
    password = token_urlsafe(18)
    monkeypatch.setenv("PHOTOCULL_VLM_API_KEY", key)
    store, headers, *_ = deployment(
        tmp_path,
        env={
            "PHOTOCULL_VLM_BASE_URL": f"https://user:{password}@example.test/v1",
            "PHOTOCULL_VLM_MODEL": "env-model",
            "PHOTOCULL_EGRESS_ALLOW": "worker.example.test",
        },
    )
    with TestClient(create_app(settings_store=store), headers=headers) as client:
        result = client.get(URL)
        view = result.json()
        assert view["api_key_configured"] is True
        assert view["fields"]["vlm.openai.base_url"]["value"] is None
        assert key not in result.text and password not in result.text
        assert view["fields"]["vlm.openai.model"]["source"] == "PHOTOCULL_VLM_MODEL"
        assert view["fields"]["network.egress_allow"]["value"] == ["worker.example.test"]
        assert change(client, {"vlm.openai.model": "replacement"}).status_code == 422
        response = change(client, {"bind_host": key})
        assert response.status_code == 422
        assert key not in response.text
        response = change(
            client, {"ml.tag_vocabulary": {"tag": [f"https://id:{password}@example.test"]}}
        )
        assert response.status_code == 422
        assert password not in response.text
        saved = change(client, {"browser.strip_location": False}).json()
        assert saved["fields"]["vlm.openai.model"]["value"] == "env-model"
        for path in [store.active, *store.backups.glob("*.json")]:
            assert key.encode() not in path.read_bytes()
            assert password.encode() not in path.read_bytes()
            assert b"env-model" not in path.read_bytes()


def test_models_and_connection_use_approved_targets(tmp_path, monkeypatch):
    store, headers, *_ = deployment(tmp_path)
    key = token_urlsafe(24)
    monkeypatch.setenv("PHOTOCULL_VLM_API_KEY", key)
    calls = []

    def http(provider, target, method, body=None):
        calls.append((target.geturl(), method))
        if method == "GET":
            return json.dumps(
                {
                    "data": [
                        {
                            "id": "fixture/vision",
                            "architecture": {"input_modalities": ["text", "image"]},
                            "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
                        },
                        {"id": key, "pricing": {"image": key}},
                    ]
                }
            ).encode()
        assert body["model"] == "fixture/vision"
        return b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'

    monkeypatch.setattr(OpenAIProvider, "_http", http)
    monkeypatch.setattr(OpenAIProvider, "_reserve", lambda *_: None)
    with TestClient(create_app(settings_store=store), headers=headers) as client:
        assert (
            change(client, {"vlm.openai.base_url": "https://other.example.test/v1"}).status_code
            == 422
        )
        assert (
            change(
                client, {"vlm.openai.base_url": "https://user:pass@vlm.example.test/v1"}
            ).status_code
            == 422
        )
        assert (
            client.post(f"{URL}/models", json={"base_url": "http://127.0.0.1:80"}).status_code
            == 422
        )
        assert not calls
        inputs = {"base_url": TRUSTED, "model": "fixture/vision"}
        result = client.post(f"{URL}/models", json=inputs)
        assert key not in result.text
        assert result.json() == {
            "items": [
                {
                    "id": "fixture/vision",
                    "vision": True,
                    "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
                }
            ]
        }
        assert client.post(f"{URL}/test", json=inputs).json() == {"ok": True}
        assert calls == [
            (f"{TRUSTED}/models", "GET"),
            (f"{TRUSTED}/chat/completions", "POST"),
        ]
        preview = change(client, {"vlm.openai.base_url": TRUSTED}, preview=True).json()
        assert any("egress" in warning for warning in preview["warnings"])
        saved = change(
            client,
            {
                "vlm.openai.base_url": TRUSTED,
                "vlm.openai.model": "fixture/vision",
                "network.egress_allow": ["VLM.EXAMPLE.TEST."],
            },
        ).json()
        assert saved["fields"]["vlm.openai.base_url"]["value"] == TRUSTED
        assert saved["fields"]["network.egress_allow"]["value"] == ["vlm.example.test"]
