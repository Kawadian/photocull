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


def test_interrupted_save_retains_the_committed_history(tmp_path, monkeypatch):
    store, headers, *_ = deployment(tmp_path)
    atomic = store._atomic

    def fail_active(path, content):
        if path == store.active:
            raise OSError("simulated failure")
        atomic(path, content)

    with TestClient(create_app(settings_store=store), headers=headers) as client:
        saved = change(client, {"worker.batch_size": 12}).json()
        revision = saved["revision"]
        with monkeypatch.context() as patch:
            patch.setattr(store, "_atomic", fail_active)
            assert change(client, {"worker.batch_size": 16}, revision).status_code == 503

        current = client.get(URL).json()
        assert current["revision"] == revision
        assert current["history"] == saved["history"]
        assert current["fields"]["worker.batch_size"]["value"] == 12
        retried = change(client, {"worker.batch_size": 16}, revision).json()
        assert len(retried["history"]) == 2
        assert retried["fields"]["worker.batch_size"]["value"] == 16


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


def test_restart_request_status_and_restart_of_api(tmp_path, monkeypatch):
    import os

    from photocull import restart

    monkeypatch.setattr(restart, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(restart, "validate_wrapper", lambda _: None)
    store, headers, *_ = deployment(
        tmp_path,
        env={
            "PHOTOCULL_SETTINGS_RESTART_SERVICES": "api,worker,egress-proxy",
            "PHOTOCULL_SETTINGS_EGRESS_MANAGED": "1",
        },
    )
    directory = store.directory / "restart"
    requests = directory / "requests"
    results = directory / "results"
    calls = []
    monkeypatch.setattr(restart.subprocess, "run", lambda args, **_: calls.append(args))
    with TestClient(create_app(settings_store=store), headers=headers) as client:
        saved = change(client, {"worker.batch_size": 12, "network.egress_allow": []}).json()
        assert saved["restart"]["automatic"] is False
        assert saved["restart"]["pending_fields"] == ["network.egress_allow", "worker.batch_size"]
        requests.mkdir(mode=0o700, parents=True)
        results.mkdir(mode=0o755)
        request = {"revision": saved["revision"]}
        assert client.get(f"{URL}/restart").json()["automatic"] is True
        assert client.post(f"{URL}/restart", json={"revision": "base"}).status_code == 409
        assert (
            client.post(f"{URL}/restart", json=request | {"services": ["arbitrary"]}).status_code
            == 422
        )
        assert (
            client.post(
                f"{URL}/restart", headers={"Origin": "https://wrong.test"}, json=request
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"{URL}/restart", headers={"Sec-Fetch-Site": "cross-site"}, json=request
            ).status_code
            == 403
        )
        assert not (requests / restart.REQUEST).exists()
        reply = client.post(f"{URL}/restart", json=request)
        assert reply.status_code == 202
        queued = reply.json()["result"]
        assert queued["status"] == "queued"
        details = json.loads((requests / restart.REQUEST).read_text())
        assert details["id"] == queued["request"]["id"]
        assert details["services"] == ["worker", "egress-proxy", "api"]
        assert details["requested_at"] > 0
        assert (requests / restart.REQUEST).stat().st_mode & 0o777 == 0o600
        assert client.post(f"{URL}/restart", json=request).status_code == 409
        assert change(client, {"worker.batch_size": 16}, saved["revision"]).status_code == 409

    wrapper = Path("/usr/local/bin/fixture-wrapper")
    restart.run_host(directory, wrapper, restart.SERVICES)
    assert calls == [[str(wrapper), "restart", name] for name in restart.SERVICES]
    assert not (requests / restart.REQUEST).exists()

    with TestClient(create_app(settings_store=store), headers=headers) as restarted:
        done = restarted.get(f"{URL}/restart").json()
        assert not done["busy"] and not done["services"]
        assert done["result"]["status"] == "succeeded"
        assert done["result"]["finished_at"] >= details["requested_at"]
        assert done["result"]["request"]["id"] == details["id"]
        assert done["result"]["restarted"] == details["services"]

    # A duplicate request left after a crash must not invoke the wrapper again.
    (requests / restart.REQUEST).write_text(json.dumps(details))
    restart.run_host(directory, wrapper, restart.SERVICES)
    assert len(calls) == 3


def test_host_handoff_rejects_unlisted_stale_and_symlink_requests(tmp_path, monkeypatch):
    import os
    from time import time
    from uuid import uuid4

    from photocull import restart

    monkeypatch.setattr(restart, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(restart, "validate_wrapper", lambda _: None)
    directory = tmp_path / "restart"
    requests = directory / "requests"
    requests.mkdir(mode=0o700, parents=True)
    results = directory / "results"
    results.mkdir(mode=0o755)
    wrapper = Path("/usr/local/bin/fixture-wrapper")
    pending = requests / restart.REQUEST
    base = {
        "id": uuid4().hex,
        "revision": "base",
        "requested_at": int(time()),
        "services": ["worker"],
        "egress": None,
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("The host must not run this request")

    monkeypatch.setattr(restart.subprocess, "run", forbidden)
    for args in (
        {"services": ["--privileged"]},
        {"services": ["worker", "worker"]},
        {"command": ["arbitrary"]},
        {"requested_at": 1},
        {"services": ["api"]},
    ):
        pending.write_text(json.dumps(base | args | {"id": uuid4().hex}))
        restart.run_host(directory, wrapper, ("worker",))
        assert not pending.exists()

    outside = tmp_path / "untrusted.json"
    outside.write_text(json.dumps(base))
    pending.symlink_to(outside)
    restart.run_host(directory, wrapper, ("worker",))
    assert not pending.is_symlink() and outside.exists()

    os.mkfifo(pending)
    restart.run_host(directory, wrapper, ("worker",))
    assert not pending.exists()


def test_live_vlm_api_and_restart_only_endpoint(tmp_path):
    store, headers, *_ = deployment(tmp_path)
    with TestClient(create_app(settings_store=store), headers=headers) as client:
        settings = change(
            client,
            {
                "vlm.openai.max_requests_per_day": 90,
                "vlm.openai.max_requests_per_run": 40,
                "vlm.openai.concurrency": 5,
                "vlm.openai.timeout_seconds": 10,
                "vlm.openai.requests_per_minute": 60,
                "vlm.on_failure": "local",
                "vlm.flags": "openai",
            },
        ).json()
        assert not settings["restart"]["services"]
        assert settings["fields"]["vlm.openai.concurrency"]["running"] == 5
        assert settings["fields"]["vlm.on_failure"]["restart"] == "live"
        providers = "/api/libraries/photos/providers"
        assert client.get(providers).json()["selection"]["flags"] == "openai"

        settings = change(
            client,
            {
                "vlm.tags": "openai",
                "vlm.flags": "local",
                "vlm.openai.base_url": TRUSTED,
                "vlm.openai.model": "next",
            },
            settings["revision"],
        ).json()
        assert settings["fields"]["vlm.tags"]["pending"] is True
        assert settings["fields"]["vlm.flags"]["restart"] == "live"
        assert settings["fields"]["vlm.openai.model"]["restart"] == "api-worker"
        assert client.get(providers).json()["selection"] == {"tags": "local", "flags": "local"}
