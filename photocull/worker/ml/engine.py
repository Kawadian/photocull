from __future__ import annotations

from pathlib import Path

import numpy as np

from photocull.config import MLSettings
from photocull.db import connect
from photocull.worker.ml.grouping import SimilarityGroups
from photocull.worker.ml.process import MAX_INFERENCE_BATCH
from photocull.worker.ml.vectors import STORAGE_DTYPE, blob_vector, exact_search, normalized


class Engine:
    def __init__(self, settings: MLSettings, cache: Path, factory):
        self.settings = settings
        self.key = settings.model_key
        self.tag_key = settings.tag_key
        self.model = factory(settings, cache)
        self.tag_vectors = None
        self.tag_work = None
        self.tag_names = tuple(sorted(settings.vocabulary))
        self.groups: SimilarityGroups | None = None
        self.cache = cache
        self.face_model = None
        self.face_key = None
        self.whisper = None
        self.whisper_key = None

    def aesthetic_head(self, prompts: tuple[str, ...]) -> bytes:
        vectors = self.text_vectors(list(prompts))
        weights = vectors[:2].mean(axis=0) - vectors[2:].mean(axis=0)
        if not np.all(np.isfinite(weights)):
            raise ValueError("Invalid aesthetic head")
        return weights.astype("<f4").tobytes()

    def faces(self, path: Path, revision: str, offline: bool, threads: int) -> list:
        from photocull.worker.insights.face_model import FaceModel

        key = (revision, offline, threads)
        if self.face_key != key:
            self.face_model = FaceModel(self.cache, *key)
            self.face_key = key
        return self.face_model.detect(path)

    def transcript(self, data: bytes, model_id: str, revision: str, offline: bool, threads: int):
        from faster_whisper import WhisperModel

        from photocull.worker.insights.models import whisper_folder

        key = model_id, revision, offline, threads
        if self.whisper_key != key:
            self.whisper = None
            directory = whisper_folder(self.cache, model_id, revision, offline)
            self.whisper = WhisperModel(
                str(directory),
                device="cpu",
                compute_type="int8",
                cpu_threads=threads,
                num_workers=1,
            )
            self.whisper_key = key
        samples = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
        if not len(samples) or np.max(np.abs(samples)) < 0.005:
            return []
        segments, _ = self.whisper.transcribe(
            samples, beam_size=1, condition_on_previous_text=False, vad_filter=False
        )
        return [
            (float(item.start), float(item.end), item.text.strip())
            for item in segments
            if item.text.strip()
        ]

    def images(self, paths: list[Path]) -> list[tuple[bytes | None, str | None]]:
        vectors, errors = self.model.image_vectors(paths)
        output = []
        for vector, error in zip(vectors, errors, strict=True):
            if error:
                output.append((None, error))
                continue
            try:
                vector = normalized(vector)
                if vector.ndim != 1:
                    raise ValueError("Unexpected image embedding shape")
                output.append((vector.astype(STORAGE_DTYPE).tobytes(), None))
            except ValueError as error:
                output.append((None, str(error)))
        return output

    def text_vectors(self, texts: list[str]) -> np.ndarray:
        try:
            vectors = normalized(self.model.text_vectors(texts))
            if vectors.ndim != 2 or len(vectors) != len(texts):
                raise ValueError("Unexpected text embedding shape")
            return vectors
        finally:
            if not self.settings.keep_text_encoder and self.tag_work is None:
                self.model.release_text()

    def _prepare_tags(self):
        vocab = self.settings.vocabulary
        labels = [label for tag in self.tag_names for label in vocab[tag]]
        prompts = [
            f"「{label}」の写真"
            if any("\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff" for char in label)
            else f"a photo of {label}"
            for label in labels
        ]
        batches = []
        size = min(MAX_INFERENCE_BATCH, self.settings.batch_size)
        try:
            for start in range(0, len(prompts), size):
                batch = prompts[start : start + size]
                vectors = normalized(self.model.text_vectors(batch))
                if vectors.ndim != 2 or len(vectors) != len(batch):
                    raise ValueError("Unexpected tag embedding shape")
                batches.append(vectors)
                if start + size < len(prompts):
                    yield
        finally:
            if not self.settings.keep_text_encoder:
                self.model.release_text()
        matrix = np.concatenate(batches)
        offset = 0
        means = []
        for tag in self.tag_names:
            end = offset + len(vocab[tag])
            means.append(matrix[offset:end].mean(axis=0))
            offset = end
        self.tag_vectors = normalized(means)

    def tags(self, blobs: list[bytes]) -> list[list[tuple[str, float]]] | None:
        if self.tag_vectors is None:
            if self.tag_work is None:
                self.tag_work = self._prepare_tags()
            try:
                next(self.tag_work)
                # Service queued searches between text batches.
                return None
            except StopIteration:
                self.tag_work = None
            except BaseException:
                self.tag_work = None
                raise
        images = np.stack([blob_vector(blob) for blob in blobs])
        scores = np.clip(np.einsum("ij,kj->ik", images, self.tag_vectors), -1, 1)
        order = np.argsort(-scores, axis=1, kind="stable")[:, : self.settings.tag_top_k]
        return [
            [(self.tag_names[i], float(row[i])) for i in indices]
            for row, indices in zip(scores, order, strict=True)
        ]

    def search(self, path: Path, request: dict) -> list[tuple[int, str, float]]:
        asset_id = request["asset_id"]
        vector = self.text_vectors([request["query"]])[0] if asset_id is None else None
        with connect(path) as db:
            db.execute("BEGIN")
            if asset_id is not None:
                row = db.execute(
                    """SELECT vector FROM asset_embeddings
                       WHERE asset_id = ? AND model_key = ? AND content_hash = ?""",
                    (asset_id, self.key, request["content_hash"]),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        "The source photo changed. Try again from its current preview."
                    )
                vector = blob_vector(row[0])
            return exact_search(
                db,
                self.key,
                vector,
                request["limit_results"],
                exclude=asset_id,
                minimum=self.settings.similar_threshold if asset_id is not None else -1,
                tag=request["tag"],
                tag_key=self.tag_key,
            )

    def group_step(self, path: Path) -> bool:
        if self.groups is not None and self.groups.path != path:
            with connect(self.groups.path) as active:
                state = active.execute(
                    "SELECT generation, grouped_generation, group_error FROM ml_state"
                ).fetchone()
            if state["generation"] != state["grouped_generation"] and not state["group_error"]:
                return False
        if self.groups is None or self.groups.path != path:
            self.groups = SimilarityGroups(path, self.key, self.settings.similar_threshold)
        try:
            with connect(path) as db:
                return self.groups.step(db)
        except BaseException:
            # The durable assignment transaction may have rolled back.
            self.groups = None
            raise
