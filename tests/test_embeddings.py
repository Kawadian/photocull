import fcntl
import sys
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from importlib.util import find_spec

import pytest
from fastapi.testclient import TestClient

from photocull.api import create_app
from photocull.config import Library, MLSettings, Settings, WorkerSettings
from photocull.db import connect, initialize
from photocull.db.jobs import claim_jobs, recover, request_scan, retry_job
from photocull.db.search import MAX_PENDING, REQUEST_TTL
from photocull.media.cache import cache_path
from photocull.worker import LibraryWorker
from photocull.worker.ml import LibraryML
from photocull.worker.ml.process import InferenceProcess
from tests.fake_embedding import FakeEmbedding
from tests.worker_harness import photo

BASE = "/api/libraries/photos"


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    photo(root / "a.jpg", "red", "01 00:00:01")
    photo(root / "b.jpg", "blue", "01 00:00:01")
    photo(root / "c.jpg", "red", "05 00:00:01")
    photo(root / "d.jpg", "red", "01 00:00:02", gradient=True)
    config = Settings(
        libraries={"photos": Library(root=root)},
        state_dir=tmp_path / "state",
        worker=WorkerSettings(memory_mb=3072, batch_size=4),
        ml=MLSettings(enabled=True, batch_size=3, tag_top_k=1),
    )
    state = config.library_state("photos")
    state.mkdir(parents=True)
    with (state / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield config


def make_worker(db, settings, inference):
    library = settings.libraries["photos"]
    state = settings.library_state("photos")
    worker = LibraryWorker(
        db, library, state, settings, lambda: None, ml=LibraryML(db, state, inference, lambda: None)
    )
    recover(db)
    worker.configure()
    worker.beat()
    return worker


def drain(worker, pool):
    for _ in range(100):
        if not worker.step(pool, worker.beat):
            return
    raise AssertionError("Worker queue failed to reach idle")


def fetch_search(client, worker, pool, endpoint, body):
    response = client.post(f"{BASE}/{endpoint}", json=body)
    assert response.status_code == 202, response.text
    result_url = f"{BASE}/search/{response.json()['id']}"
    assert client.get(result_url).json()["status"] == "pending"
    worker.step(pool, worker.beat)
    result = client.get(result_url).json()
    assert result["status"] == "done", result
    return result["items"], result_url


def test_embedding_stages_groups_tags_and_search(settings):
    settings = settings.model_copy(
        update={"ml": settings.ml.model_copy(update={"similar_threshold": 1.0})}
    )
    before = {
        path: sha256(path.read_bytes()).hexdigest()
        for path in settings.libraries["photos"].root.iterdir()
    }
    state = settings.library_state("photos")
    inference = InferenceProcess(settings, FakeEmbedding)
    try:
        with (
            TestClient(create_app(settings)) as client,
            connect(state / "index.db") as db,
            ThreadPoolExecutor(1) as pool,
        ):
            worker = make_worker(db, settings, inference)
            assert inference.process is None
            request_scan(db)
            drain(worker, pool)
            jobs = client.get(f"{BASE}/jobs").json()
            assert jobs["stages"]["embedding"] == {"done": 4}
            assert not jobs["ml"]["grouping"]
            model = db.execute("SELECT * FROM embedding_models").fetchone()
            assert model["model_id"] == settings.ml.model_id
            assert model["revision"] == settings.ml.model_revision
            assert '"resize": [224, 224]' in model["preprocessing"]
            assert (
                db.execute("SELECT DISTINCT length(vector) FROM asset_embeddings").fetchone()[0]
                == 6
            )
            assert client.get(f"{BASE}/similar-groups").json()["items"] == [
                {"id": 1, "path": "a.jpg", "member_count": 3}
            ]
            burst = client.get(f"{BASE}/bursts").json()["items"]
            assert [item["member_count"] for item in burst] == [2]
            assert [
                item["path"]
                for item in client.get(
                    f"{BASE}/assets", params={"burst_id": burst[0]["id"]}
                ).json()["items"]
            ] == ["a.jpg", "d.jpg"]
            groups = client.get(f"{BASE}/assets", params={"similar_group_id": 1, "limit": 1}).json()
            assert groups["next_cursor"] == 1 and groups["items"][0]["path"] == "a.jpg"
            assert groups["items"][0]["tags"] == [{"tag": "cat", "label": "Cat / 猫", "score": 1.0}]
            dog = client.get(f"{BASE}/assets", params={"tag": "dog"}).json()["items"]
            assert [item["path"] for item in dog] == ["b.jpg"]

            for query in ("a cat", "猫の写真"):
                items, _ = fetch_search(
                    client, worker, pool, "search", {"query": query, "limit": 2}
                )
                assert [item["path"] for item in items] == ["a.jpg", "c.jpg"]
                assert [item["similarity"] for item in items] == pytest.approx([1.0, 1.0])
            items, stale = fetch_search(client, worker, pool, "assets/1/similar", {"tag": "cat"})
            assert [item["path"] for item in items] == ["c.jpg", "d.jpg"]
            items, _ = fetch_search(client, worker, pool, "search", {"query": "犬", "tag": "dog"})
            assert [item["path"] for item in items] == ["b.jpg"]
            assert client.get(f"{BASE}/assets", params={"tag": "unknown"}).status_code == 422
            assert client.post(f"{BASE}/search", json={"query": "  "}).status_code == 422

            assert {path: sha256(path.read_bytes()).hexdigest() for path in before} == before
            photo(settings.libraries["photos"].root / "c.jpg", "blue", "05 00:00:01")
            request_scan(db)
            drain(worker, pool)
            assert [item["path"] for item in client.get(stale).json()["items"]] == ["d.jpg"]
            assert client.get(f"{BASE}/assets/3").json()["tags"][0]["tag"] == "dog"
    finally:
        inference.close()


def test_model_change_resume_and_vocabulary_only_refresh(settings):
    state = settings.library_state("photos")
    initialize_client = TestClient(create_app(settings))
    with initialize_client, connect(state / "index.db") as db, ThreadPoolExecutor(1) as pool:
        config = settings
        for model_id, image_size in (
            (settings.ml.model_id, 224),
            ("test/changed-model", 224),
            ("test/changed-model", 256),
        ):
            config = config.model_copy(
                update={
                    "ml": config.ml.model_copy(
                        update={"model_id": model_id, "image_size": image_size}
                    )
                }
            )
            inference = InferenceProcess(config, FakeEmbedding)
            try:
                worker = make_worker(db, config, inference)
                if not db.execute("SELECT 1 FROM assets LIMIT 1").fetchone():
                    request_scan(db)
                else:
                    assert (
                        db.execute(
                            "SELECT count(*) FROM jobs WHERE stage = 'embedding' "
                            "AND status = 'pending' AND model_key = ?",
                            (config.ml.model_key,),
                        ).fetchone()[0]
                        == 4
                    )
                    assert len(claim_jobs(db, 2, embedding=True)) == 2
                    recover(db)
                drain(worker, pool)
                assert {
                    row[0] for row in db.execute("SELECT DISTINCT model_key FROM asset_embeddings")
                } == {config.ml.model_key}
            finally:
                inference.close()

        assert db.execute("SELECT count(*) FROM embedding_models").fetchone()[0] == 3
        config = config.model_copy(
            update={
                "ml": config.ml.model_copy(update={"tag_vocabulary": {"cat": ("Kitten", "猫")}})
            }
        )
        inference = InferenceProcess(config, FakeEmbedding)
        try:
            worker = make_worker(db, config, inference)
            assert (
                db.execute("SELECT sum(attempts) FROM jobs WHERE stage = 'embedding'").fetchone()[0]
                == 6
            )
            drain(worker, pool)
            assert (
                db.execute(
                    "SELECT count(*) FROM asset_embeddings WHERE tag_key = ?", (config.ml.tag_key,)
                ).fetchone()[0]
                == 4
            )
            assert (
                db.execute("SELECT sum(attempts) FROM jobs WHERE stage = 'embedding'").fetchone()[0]
                == 6
            )
        finally:
            inference.close()


@pytest.mark.parametrize("failure", ["offline", "memory", "dependency"])
def test_ml_failure_preserves_media_work(settings, monkeypatch, failure):
    if failure == "memory":
        settings = settings.model_copy(update={"worker": WorkerSettings(memory_mb=1536)})
    if failure == "dependency":
        monkeypatch.setattr(
            "photocull.worker.ml.process.find_spec",
            lambda name: None if name == "onnxruntime" else find_spec(name),
        )
    settings = settings.model_copy(update={"ml": settings.ml.model_copy(update={"offline": True})})
    inference = InferenceProcess(settings)
    match = {
        "offline": "Offline cache is missing",
        "memory": "worker budget",
        "dependency": "Install photocull[ml]",
    }[failure]
    try:
        with (
            TestClient(create_app(settings)) as client,
            connect(settings.library_state("photos") / "index.db") as db,
            ThreadPoolExecutor(1) as pool,
        ):
            worker = make_worker(db, settings, inference)
            request_scan(db)
            drain(worker, pool)
            jobs = client.get(f"{BASE}/jobs").json()
            assert jobs["stages"]["embedding"] == {"error": 4}
            assert jobs["stages"]["preview"] == {"done": 4}
            assert match in jobs["ml"]["image_error"]
            assert client.get(f"{BASE}/assets/1/media/thumb").status_code == 200
            assert client.post(f"{BASE}/search", json={"query": "猫"}).status_code == 503
            if failure != "offline":
                assert inference.process is None
    finally:
        inference.close()


def test_disabled_features_queue_limit_and_expiry(settings):
    state = settings.library_state("photos")
    inference = InferenceProcess(settings, FakeEmbedding)
    try:
        with TestClient(create_app(settings)) as client, connect(state / "index.db") as db:
            worker = make_worker(db, settings, inference)
            with ThreadPoolExecutor(1) as pool:
                request_scan(db)
                drain(worker, pool)
            for _ in range(MAX_PENDING):
                queued = client.post(f"{BASE}/search", json={"query": "猫"})
                assert queued.status_code == 202
            assert client.post(f"{BASE}/search", json={"query": "猫"}).status_code == 429
            db.execute("UPDATE search_requests SET created_at = created_at - ?", (REQUEST_TTL,))
            assert client.get(f"{BASE}/search/{queued.json()['id']}").status_code == 404
            assert client.post(f"{BASE}/search", json={"query": "猫"}).status_code == 202
            db.execute("UPDATE worker_status SET heartbeat = 0")
            assert client.post(f"{BASE}/search", json={"query": "cat"}).status_code == 503
    finally:
        inference.close()

    settings = settings.model_copy(update={"ml": MLSettings(enabled=False)})
    with TestClient(create_app(settings)) as client, connect(state / "index.db") as db:
        inference = InferenceProcess(settings, FakeEmbedding)
        make_worker(db, settings, inference)
        assert inference.process is None
        assert "embedding" not in client.get(f"{BASE}/jobs").json()["stages"]
        assert client.get(f"{BASE}/tags").json()["items"] == []
        assert client.post(f"{BASE}/search", json={"query": "猫"}).status_code == 409
        assert client.get(f"{BASE}/assets", params={"tag": "cat"}).status_code == 409
        assert client.get(f"{BASE}/similar-groups").status_code == 409
        assert "tags" not in client.get(f"{BASE}/assets/1").json()


def test_embedding_waits_for_preview_and_can_be_retried(settings):
    config = settings.model_copy(
        update={"worker": WorkerSettings(memory_mb=3072, stages=("exif",))}
    )
    state = settings.library_state("photos")
    with (
        TestClient(create_app(config)),
        connect(state / "index.db") as db,
        ThreadPoolExecutor(1) as pool,
    ):
        inference = InferenceProcess(config, FakeEmbedding)
        worker = make_worker(db, config, inference)
        request_scan(db)
        drain(worker, pool)
        assert inference.process is None
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE stage = 'embedding' AND status = 'skipped'"
            ).fetchone()[0]
            == 4
        )
        inference = InferenceProcess(settings)
        try:
            worker = make_worker(db, settings, inference)
            # Exercise the real adapter with missing cached previews without loading weights.
            row = db.execute("SELECT content_hash FROM assets WHERE id = 1").fetchone()
            preview = cache_path(state, "previews", 1, row[0])
            db.execute("UPDATE jobs SET status = 'done' WHERE stage = 'preview' AND asset_id = 1")
            db.execute(
                "UPDATE jobs SET status = 'pending' WHERE stage = 'embedding' AND asset_id = 1"
            )
            assert not preview.exists()
            assert worker.ml.process_images()
            assert (
                "cached preview"
                in db.execute(
                    "SELECT error FROM jobs WHERE stage = 'embedding' AND asset_id = 1"
                ).fetchone()[0]
            )
            assert retry_job(db, 1, "embedding")["status"] == "pending"
        finally:
            inference.close()


class AllocationProbe(FakeEmbedding):
    def image_vectors(self, paths):
        import errno
        import mmap
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        assert 1536 * 1024**2 <= soft == hard < (3072 - 512) * 1024**2
        with pytest.raises(OSError, match="Cannot allocate memory") as failure:
            mmap.mmap(-1, soft)
        assert failure.value.errno == errno.ENOMEM
        return super().image_vectors(paths)


def test_inference_allocation_cap_is_applied_in_the_child(settings):
    inference = InferenceProcess(settings, AllocationProbe)
    try:
        paths = list(settings.libraries["photos"].root.glob("*.jpg"))
        assert all(blob for blob, error in inference.call("images", (paths,), lambda: None))
    finally:
        inference.close()


class OnnxImportProbe(FakeEmbedding):
    def image_vectors(self, paths):
        loaded = tuple(sys.modules)
        if "usearch.index" in loaded:
            assert "onnxruntime" in loaded, "usearch loaded before ONNX Runtime"
            assert loaded.index("onnxruntime") < loaded.index("usearch.index")
        import onnxruntime

        assert "CPUExecutionProvider" in onnxruntime.get_available_providers()
        return super().image_vectors(paths)


@pytest.mark.parametrize("group_first", [False, True], ids=["images-first", "groups-first"])
def test_ml_child_loads_onnxruntime_before_usearch(settings, group_first):
    if any(find_spec(name) is None for name in ("onnxruntime", "usearch")):
        pytest.skip("Requires photocull[ml]")
    path = settings.library_state("photos") / "index.db"
    initialize(path)
    with connect(path) as db:
        db.execute("UPDATE ml_state SET generation = 1, grouped_generation = 0")
    inference = InferenceProcess(settings, OnnxImportProbe)

    def pulse():
        pass

    try:
        if group_first:
            assert inference.call("group_step", (path,), pulse)
        paths = [settings.libraries["photos"].root / "a.jpg"]
        [(embedding, error)] = inference.call("images", (paths,), pulse)
        assert embedding and error is None
        if not group_first:
            assert inference.call("group_step", (path,), pulse)
    finally:
        inference.close()


def test_search_and_tag_flags_are_independent(settings):
    state = settings.library_state("photos")
    for search_enabled, tags_enabled in ((False, True), (True, False)):
        config = settings.model_copy(
            update={
                "ml": settings.ml.model_copy(
                    update={
                        "search_enabled": search_enabled,
                        "tags_enabled": tags_enabled,
                    }
                )
            }
        )
        inference = InferenceProcess(config, FakeEmbedding)
        try:
            with (
                TestClient(create_app(config)) as client,
                connect(state / "index.db") as db,
                ThreadPoolExecutor(1) as pool,
            ):
                worker = make_worker(db, config, inference)
                request_scan(db)
                drain(worker, pool)
                assert bool(client.get(f"{BASE}/tags").json()["items"]) == tags_enabled
                assert client.get(f"{BASE}/assets", params={"tag": "cat"}).status_code == (
                    200 if tags_enabled else 409
                )
                assert client.post(f"{BASE}/search", json={"query": "猫"}).status_code == (
                    202 if search_enabled else 409
                )
                similar = client.post(f"{BASE}/assets/1/similar", json={})
                assert similar.status_code == 202
        finally:
            inference.close()


def test_model_files_download_once_and_publish_only_complete_files(tmp_path, monkeypatch):
    from io import BytesIO

    from photocull.worker.ml import model

    payload = b"cached model data"
    monkeypatch.setitem(model.FILES, model.VISION, (len(payload), sha256(payload).hexdigest()))
    opened = []

    def fetch(url, timeout):
        opened.append((url, timeout))
        return BytesIO(payload if len(opened) == 1 else b"invalid")

    monkeypatch.setattr(model, "urlopen", fetch)
    settings = MLSettings()
    with ThreadPoolExecutor(2) as pool:
        files = list(
            pool.map(lambda _: model.model_file(settings, tmp_path, model.VISION), range(2))
        )
    assert files[0] == files[1]
    assert files[0].read_bytes() == payload
    assert len(opened) == 1
    assert settings.model_revision in opened[0][0]

    offline = settings.model_copy(update={"offline": True})
    assert model.model_file(offline, tmp_path, model.VISION) == files[0]
    files[0].unlink()
    with pytest.raises(model.ModelUnavailable, match="Offline cache"):
        model.model_file(offline, tmp_path, model.VISION)
    with pytest.raises(model.ModelUnavailable, match="Checksum mismatch"):
        model.model_file(settings, tmp_path, model.VISION)
    assert not files[0].exists()
    assert not files[0].with_suffix(".part").exists()


def test_restart_replays_live_search_once_then_deadline_and_offline_are_reported(settings):
    from photocull.db import search

    state = settings.library_state("photos")
    inference = InferenceProcess(settings, FakeEmbedding)
    try:
        with (
            TestClient(create_app(settings)) as client,
            connect(state / "index.db") as db,
            ThreadPoolExecutor(1) as pool,
        ):
            worker = make_worker(db, settings, inference)
            request_scan(db)
            drain(worker, pool)

            def submit():
                response = client.post(f"{BASE}/search", json={"query": "猫"})
                assert response.status_code == 202, response.text
                return f"{BASE}/search/{response.json()['id']}"

            url = submit()
            assert search.claim(db)["attempts"] == 1
            worker = make_worker(db, settings, inference)
            assert client.get(url).json()["status"] == "pending"
            worker.step(pool, worker.beat)
            assert client.get(url).json()["items"][0]["path"] == "a.jpg"

            twice = submit()
            assert search.claim(db)["attempts"] == 1
            make_worker(db, settings, inference)
            assert search.claim(db)["attempts"] == 2
            make_worker(db, settings, inference)
            assert client.get(twice).json()["status"] == "error"

            expired = submit()
            db.execute(
                "UPDATE search_requests SET created_at = created_at - ? WHERE id = ?",
                (search.SEARCH_TIMEOUT + 1, expired.rsplit("/", 1)[-1]),
            )
            assert client.get(expired).status_code == 504
            assert search.claim(db) is None
            pending = submit()
            db.execute("DELETE FROM worker_status")
            assert client.get(pending).status_code == 503
            assert client.get(url).json()["status"] == "done"
    finally:
        inference.close()


def test_concurrent_queue_capacity_and_cancelling_running_search(settings):
    from collections import Counter

    from photocull.db import search

    state = settings.library_state("photos")
    inference = InferenceProcess(settings, FakeEmbedding)
    with TestClient(create_app(settings)) as client, connect(state / "index.db") as db:
        make_worker(db, settings, inference)
        with ThreadPoolExecutor(8) as parallel:
            responses = list(
                parallel.map(
                    lambda index: client.post(f"{BASE}/search", json={"query": f"猫 {index}"}),
                    range(search.MAX_PENDING + 8),
                )
            )
        assert Counter(response.status_code for response in responses) == {
            202: search.MAX_PENDING,
            429: 8,
        }
        first = search.claim(db)
        url = f"{BASE}/search/{first['id']}"
        assert client.delete(url).status_code == 204
        search.finish(db, first, [])
        assert client.get(url).status_code == 404
        assert client.post(f"{BASE}/search", json={"query": "another"}).status_code == 202


def test_incremental_similarity_preserves_groups_and_recovers_missing_checkpoint(settings):
    root = settings.libraries["photos"].root
    state = settings.library_state("photos")
    inference = InferenceProcess(settings, FakeEmbedding)
    try:
        with (
            TestClient(create_app(settings)) as client,
            connect(state / "index.db") as db,
            ThreadPoolExecutor(1) as pool,
        ):
            worker = make_worker(db, settings, inference)
            request_scan(db)
            drain(worker, pool)
            revision = db.execute("SELECT anchor_revision FROM ml_state").fetchone()[0]
            checkpoint = next(state.glob("*.usearch"))
            photo(root / "e.jpg", "blue", "06 00:00:01")
            photo(root / "f.jpg", "red", "07 00:00:01")
            request_scan(db)
            drain(worker, pool)
            assert revision == db.execute("SELECT anchor_revision FROM ml_state").fetchone()[0]
            assert checkpoint.is_file()
            assert [
                item["member_count"]
                for item in client.get(f"{BASE}/similar-groups").json()["items"]
            ] == [4, 2]

            (root / "a.jpg").unlink()
            request_scan(db)
            drain(worker, pool)
            assert [
                tuple(row)
                for row in db.execute("SELECT id, member_count FROM similar_groups ORDER BY id")
            ] == [(2, 2), (3, 3)]
            assert not db.execute("SELECT 1 FROM similar_dirty").fetchone()
            inference.close()
            checkpoint = next(state.glob("*.usearch"))
            checkpoint.write_bytes(b"corrupt")

            worker = make_worker(db, settings, inference)
            photo(root / "g.jpg", "blue", "08 00:00:01")
            request_scan(db)
            drain(worker, pool)
            assert [
                tuple(row)
                for row in db.execute("SELECT id, member_count FROM similar_groups ORDER BY id")
            ] == [(2, 3), (3, 3)]
    finally:
        inference.close()
