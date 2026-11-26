import os
import resource
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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

FIXTURE_NAMES = ("astronaut.png", "chelsea.png", "coffee.png", "rocket.jpg")
FIXTURE_BASE = "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.25.2/skimage/data/"


@pytest.mark.model
def test_real_siglip_search_in_english_and_japanese(tmp_path):
    start = perf_counter()
    root = tmp_path / "fixtures"
    root.mkdir()
    for name in FIXTURE_NAMES:
        if fixture_dir := os.getenv("PHOTOCULL_TEST_FIXTURES"):
            (root / name).write_bytes((Path(fixture_dir) / name).read_bytes())
        else:
            urlretrieve(FIXTURE_BASE + name, root / name)
    settings = Settings(
        libraries={"sample": Library(root=root)},
        state_dir=tmp_path / "state",
        model_dir=Path(os.getenv("PHOTOCULL_MODEL_DIR", ".photocull/models")).resolve(),
        worker=WorkerSettings(memory_mb=3072),
        ml=MLSettings(enabled=True, offline=os.getenv("PHOTOCULL_ML_OFFLINE") == "1"),
    )
    prepare = perf_counter()
    size = sum(model_file(settings.ml, settings.model_cache, file).stat().st_size for file in FILES)
    print(f"cache preparation {perf_counter() - prepare:.2f} s, {size / (1024**2):.1f} MiB")
    inference = InferenceProcess(settings)
    state = settings.library_state("sample")
    base = "/api/libraries/sample"
    try:
        with (
            TestClient(create_app(settings)) as client,
            connect(state / "index.db") as db,
            ThreadPoolExecutor(1) as pool,
        ):
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
