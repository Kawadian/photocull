import os
from time import perf_counter, sleep

import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import Library, MLSettings, Settings, WorkerSettings
from photocull.db import connect
from photocull.worker.ml.process import InferenceInterrupted, InferenceProcess
from tests.fake_embedding import FakeEmbedding
from tests.worker_harness import append_cached_previews, photo, start_worker, wait_for

BASE = "/api/libraries/photos"


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    photo(root / "a.jpg", "red", "01 00:00:01")
    return Settings(
        libraries={"photos": Library(root=root)},
        state_dir=tmp_path / "state",
        worker=WorkerSettings(memory_mb=3072),
        ml=MLSettings(enabled=True, tags_enabled=False, batch_size=16),
    )


class InstrumentedEmbedding(FakeEmbedding):
    def __init__(self, settings, cache):
        self.cache = cache
        self.cache.mkdir(exist_ok=True, parents=True)

    def image_vectors(self, paths):
        signal_file = self.cache / "fail-image"
        if signal_file.exists():
            failure = signal_file.read_text()
            signal_file.unlink()
            if failure == "oom":
                raise MemoryError
            if failure == "hang":
                sleep(20)
            else:
                os._exit(46)
        if (self.cache / "slow-images").exists():
            assert len(paths) <= 4
            with (self.cache / "trace").open("a") as file:
                file.write("image\n")
            sleep(0.25)
        return super().image_vectors(paths)

    def text_vectors(self, texts):
        kill = self.cache / "kill-text"
        if kill.exists():
            kill.unlink()
            os._exit(47)
        with (self.cache / "trace").open("a") as file:
            file.write("text\n")
        return super().text_vectors(texts)


def search(client):
    response = client.post(f"{BASE}/search", json={"query": "猫"})
    assert response.status_code == 202, response.text
    return f"{BASE}/search/{response.json()['id']}"


def ready(client):
    return client.get(f"{BASE}/jobs").json()["worker_online"]


def start_scan(client):
    wait_for(lambda: ready(client))
    client.post(f"{BASE}/scans").raise_for_status()


def completed(client, url):
    response = client.get(url)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] != "error", result
    return result if result["status"] == "done" else None


def test_priority_and_stale_heartbeat_after_worker_is_killed(config):
    state = config.library_state("photos")
    with (
        TestClient(create_app(config)) as client,
        start_worker(
            config,
            model_factory=InstrumentedEmbedding,
        ) as worker,
    ):
        start_scan(client)
        wait_for(
            lambda: client.get(f"{BASE}/jobs").json()["stages"].get("embedding") == {"done": 1}
        )
        (config.model_cache / "slow-images").touch()
        append_cached_previews(state, 20)
        trace = config.model_cache / "trace"
        wait_for(lambda: trace.exists() and trace.read_text().startswith("image"), interval=0.01)
        started = perf_counter()
        url = search(client)
        result = wait_for(lambda: completed(client, url), interval=0.01)
        assert perf_counter() - started < 2
        assert result["items"][0]["path"] == "a.jpg"
        assert trace.read_text().splitlines()[:2] == ["image", "text"]

        pending = search(client)
        worker.kill()
        worker.join(2)
        assert not worker.is_alive()
        with connect(state / "index.db") as db:
            assert db.execute("SELECT heartbeat FROM worker_status").fetchone()
        assert not ready(client)
        assert "offline" in client.get(pending).json()["detail"]
        assert client.post(f"{BASE}/search", json={"query": "cat"}).status_code == 503
        assert client.get(url).json()["status"] == "done"


@pytest.mark.parametrize("failure", ["oom", "exit"])
def test_image_oom_and_child_exit_retry_and_preserve_other_work(config, failure):
    config.model_cache.mkdir(parents=True)
    (config.model_cache / "fail-image").write_text(failure)
    state = config.library_state("photos")
    with (
        TestClient(create_app(config)) as client,
        start_worker(config, model_factory=InstrumentedEmbedding),
    ):
        start_scan(client)
        wait_for(
            lambda: client.get(f"{BASE}/jobs").json()["stages"].get("embedding") == {"done": 1}
        )
        with connect(state / "index.db") as db:
            assert (
                db.execute("SELECT attempts FROM jobs WHERE stage = 'embedding'").fetchone()[0] == 2
            )
        assert client.get(f"{BASE}/assets/1/media/thumb").status_code == 200
        (config.model_cache / "kill-text").touch()
        url = search(client)
        result = wait_for(lambda: completed(client, url))
        assert result["items"][0]["path"] == "a.jpg"
        with connect(state / "index.db") as db:
            assert db.execute("SELECT attempts FROM search_requests").fetchone()[0] == 2


def test_timeout_reaps_child_and_next_call_can_start(config):
    inference = InferenceProcess(config, InstrumentedEmbedding)
    paths = list(config.libraries["photos"].root.iterdir())
    try:
        assert inference.call("images", (paths,), lambda: None)
        (config.model_cache / "fail-image").write_text("hang")
        child = inference.process.pid
        started = perf_counter()
        with pytest.raises(InferenceInterrupted, match="timed out"):
            inference.call("images", (paths,), lambda: None, timeout=0.2)
        assert perf_counter() - started < 1.5
        assert inference.process is None
        wait_for(lambda: not inference.cooling_down)
        assert inference.call("images", (paths,), lambda: None)
        assert inference.process.pid != child
    finally:
        inference.close()
