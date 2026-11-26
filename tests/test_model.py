import fcntl
import os
import resource
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from time import perf_counter
from urllib.request import urlretrieve

import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import Library, MLSettings, Settings, WorkerSettings
from photocull.db import connect
from photocull.db.jobs import request_scan
from photocull.media.cache import cache_path
from photocull.worker import LibraryWorker
from photocull.worker.ml import LibraryML
from photocull.worker.ml.model import FILES, model_file
from photocull.worker.ml.process import InferenceProcess
from tests.worker_harness import append_cached_previews, start_worker, wait_for

FIXTURE_NAMES = ("astronaut.png", "chelsea.png", "coffee.png", "rocket.jpg")
FIXTURE_BASE = "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.25.2/skimage/data/"


@pytest.fixture
def model_settings(tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    for name in FIXTURE_NAMES:
        if fixture_dir := os.getenv("PHOTOCULL_TEST_FIXTURES"):
            (root / name).write_bytes((Path(fixture_dir) / name).read_bytes())
        else:
            urlretrieve(FIXTURE_BASE + name, root / name)
    return Settings(
        libraries={"sample": Library(root=root)},
        state_dir=tmp_path / "state",
        model_dir=Path(os.getenv("PHOTOCULL_MODEL_DIR", ".photocull/models")).resolve(),
        worker=WorkerSettings(memory_mb=3072),
        ml=MLSettings(enabled=True, offline=os.getenv("PHOTOCULL_ML_OFFLINE") == "1"),
    )


@pytest.mark.model
def test_real_siglip_search_in_english_and_japanese(model_settings):
    start = perf_counter()
    settings = model_settings
    prepare = perf_counter()
    size = sum(model_file(settings.ml, settings.model_cache, file).stat().st_size for file in FILES)
    print(f"cache preparation {perf_counter() - prepare:.2f} s, {size / (1024**2):.1f} MiB")
    inference = InferenceProcess(settings)
    state = settings.library_state("sample")
    base = "/api/libraries/sample"
    state.mkdir(parents=True)
    try:
        with (
            (state / "worker.lock").open("a") as lock,
            TestClient(create_app(settings)) as client,
            connect(state / "index.db") as db,
            ThreadPoolExecutor(1) as pool,
        ):
            fcntl.flock(lock, fcntl.LOCK_EX)
            ml = LibraryML(db, state, inference, lambda: None)
            worker = LibraryWorker(
                db, settings.libraries["sample"], state, settings, lambda: None, ml=ml
            )
            worker.configure()
            request_scan(db)
            for _ in range(80):
                if not worker.step(pool, worker.beat):
                    break
            assert db.execute("SELECT count(*) FROM asset_embeddings").fetchone()[0] == 4, (
                client.get(f"{base}/jobs").json()
            )
            assert db.execute("SELECT count(*) FROM asset_tags").fetchone()[0] == 12

            for query, expected in (
                ("a photo of a cat", "chelsea.png"),
                ("猫の写真", "chelsea.png"),
                ("a cup of coffee", "coffee.png"),
                ("コーヒーの入ったカップ", "coffee.png"),
            ):
                started = perf_counter()
                queued = client.post(f"{base}/search", json={"query": query})
                assert queued.status_code == 202, queued.text
                worker.step(pool, worker.beat)
                match = client.get(f"{base}/search/{queued.json()['id']}").json()
                assert match["status"] == "done", match
                top = match["items"][0]
                print(
                    f"{query} -> {top['path']} ({top['similarity']:.3f}), "
                    f"{perf_counter() - started:.3f} s"
                )
                assert top["path"] == expected

            previews = [
                cache_path(state, "previews", row["id"], row["content_hash"])
                for row in db.execute("SELECT id, content_hash FROM assets ORDER BY id")
            ]
            warm = perf_counter()
            for _ in range(8):
                assert all(blob for blob, _ in inference.call("images", (previews,), worker.beat))
            print(f"warm batch=4, 32 previews: {(perf_counter() - warm) / 32:.3f} s/photo")
    finally:
        inference.close()
        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
        print(f"peak child RSS {peak:.1f} MiB, test elapsed {perf_counter() - start:.2f} s")


@pytest.mark.model
@pytest.mark.parametrize("resident", [True, False], ids=["resident", "on-demand"])
def test_real_search_round_trip_under_indexing_load(model_settings, resident):
    settings = model_settings.model_copy(
        update={"ml": model_settings.ml.model_copy(update={"keep_text_encoder": resident})}
    )
    base = "/api/libraries/sample"
    state = settings.library_state("sample")
    with TestClient(create_app(settings)) as client, start_worker(settings):
        wait_for(lambda: client.get(f"{base}/jobs").json()["worker_online"])
        client.post(f"{base}/scans").raise_for_status()
        wait_for(
            lambda: client.get(f"{base}/jobs").json()["stages"].get("embedding") == {"done": 4}
        )

        def tags_ready():
            assets = client.get(f"{base}/assets").json()["items"]
            return assets if len(assets) == 4 and all(asset["tags"] for asset in assets) else None

        wait_for(tags_ready)

        def request_result():
            started = perf_counter()
            response = client.post(f"{base}/search", json={"query": "猫の写真"})
            assert response.status_code == 202, response.text
            url = f"{base}/search/{response.json()['id']}"

            def completed():
                reply = client.get(url)
                assert reply.status_code == 200, reply.text
                result = reply.json()
                assert result["status"] != "error", result
                return result if result["status"] == "done" else None

            matches = wait_for(completed, interval=0.05)
            assert matches["items"][0]["path"] == "chelsea.png"
            return perf_counter() - started

        # Warm the API and encoder path once. With on-demand text mode each
        # subsequent query opens and releases the tokenizer and text session.
        request_result()
        idle = [request_result() for _ in range(8)]
        append_cached_previews(state, 48)
        busy = []
        for _ in range(8):
            wait_for(
                lambda: client.get(f"{base}/jobs").json()["stages"]["embedding"].get("running"),
                interval=0.01,
            )
            busy.append(request_result())
        for label, samples in (("idle", idle), ("embedding", busy)):
            print(
                f"resident={resident} {label}: p50 {median(samples) * 1000:.0f} ms, "
                f"p95 {max(samples) * 1000:.0f} ms (n={len(samples)})"
            )
            assert max(samples) < 4, samples
