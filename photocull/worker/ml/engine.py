from __future__ import annotations

from pathlib import Path

import numpy as np

from photocull.config import MLSettings
from photocull.db import connect
from photocull.worker.ml.grouping import rebuild
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
        self.group_work = None

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
        size = self.settings.batch_size
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
        with connect(path) as db:
            if asset_id is None:
                vector = self.text_vectors([request["query"]])[0]
            else:
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
            db.execute("BEGIN")
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

    def group_step(self, path: Path, generation: int) -> bool:
        if self.group_work is not None:
            active, revision, _ = self.group_work
            if path != active:
                with connect(active) as db:
                    state = db.execute("SELECT generation, image_error FROM ml_state").fetchone()
                if state["generation"] == revision and not state["image_error"]:
                    return False
                self.drop_group()
            elif generation != revision:
                self.drop_group()
        if self.group_work is None:
            self.group_work = path, generation, self._group(path, generation)
        try:
            next(self.group_work[2])
        except StopIteration:
            self.drop_group()
        except BaseException:
            self.drop_group()
            raise
        return True

    def _group(self, path: Path, generation: int):
        with connect(path) as db:
            yield from rebuild(db, self.key, generation, self.settings.similar_threshold)

    def drop_group(self) -> None:
        if self.group_work is not None:
            self.group_work[2].close()
            self.group_work = None
