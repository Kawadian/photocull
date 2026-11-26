from __future__ import annotations

import fcntl
import logging
import os
from hashlib import sha256
from pathlib import Path
from shutil import copyfileobj
from urllib.request import urlopen

from photocull.config import DEFAULT_MODEL, DEFAULT_REVISION, MLSettings

LOG = logging.getLogger(__name__)
VISION = "onnx/vision_model_quantized.onnx"
TEXT = "onnx/text_model_quantized.onnx"
TOKENIZER = "tokenizer.json"
FILES = {
    VISION: (94553333, "5f2b401c1a4fc095702a5d45348e17ad46c4f87064085365b43c6e8eaa5c0070"),
    TEXT: (283438275, "3a0603d3a00c05a80a6ded4743c16aaac7b1e62cdcc7e362e7ce418659b96400"),
    TOKENIZER: (34363039, "cb9140fae3ac5122c972d37adf83e1248471a38147ad76f8215c8872c6fd8322"),
}


class ModelUnavailable(RuntimeError):
    pass


def model_file(settings: MLSettings, cache: Path, name: str) -> Path:
    root = cache / settings.model_id / settings.model_revision
    target = root / name
    verified_default = (settings.model_id, settings.model_revision) == (
        DEFAULT_MODEL,
        DEFAULT_REVISION,
    )
    size, checksum = FILES[name] if verified_default else (None, None)

    def cached() -> bool:
        try:
            length = target.stat().st_size
            return length > 0 and (size is None or size == length)
        except FileNotFoundError:
            return False

    try:
        if cached():
            return target
        if settings.offline:
            raise ModelUnavailable(f"Offline cache is missing {target}. Run photocull fetch-model.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with (root / ".download.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if cached():
                return target
            url = f"https://huggingface.co/{settings.model_id}/resolve/{settings.model_revision}/{name}"
            temporary = target.with_suffix(".part")
            try:
                LOG.info("Downloading model file %s", name)
                with urlopen(url, timeout=20) as response, temporary.open("wb") as output:
                    copyfileobj(response, output, length=1024 * 1024)
                if size is not None:
                    with temporary.open("rb") as handle:
                        digest = sha256()
                        while block := handle.read(1024 * 1024):
                            digest.update(block)
                    if temporary.stat().st_size != size or digest.hexdigest() != checksum:
                        raise ModelUnavailable(f"Checksum mismatch for {name}")
                elif temporary.stat().st_size == 0:
                    raise ModelUnavailable(f"Empty model file {name}")
                temporary.replace(target)
                return target
            finally:
                temporary.unlink(missing_ok=True)
    except OSError as error:
        # The file and pinned revision are enough to diagnose fetch failures. Avoid
        # including redirect URLs, which can contain signed parameters, in SQLite.
        raise ModelUnavailable(
            f"Could not cache {name} for {settings.model_id} at {settings.model_revision} "
            f"({type(error).__name__}). Check network and cache permissions or run "
            "photocull fetch-model."
        ) from error


class Siglip:
    def __init__(self, settings: MLSettings, cache: Path):
        self.settings = settings
        self.cache = cache
        self.vision = None
        self.text = None
        self.tokenizer = None

    def _session(self, name: str, input_name: str, input_type: str):
        path = model_file(self.settings, self.cache, name)
        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = self.settings.threads
            options.inter_op_num_threads = 1
            options.enable_cpu_mem_arena = False
            options.enable_mem_pattern = False
            session = ort.InferenceSession(
                str(path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            inputs = session.get_inputs()
            outputs = {output.name for output in session.get_outputs()}
            if [(value.name, value.type) for value in inputs] != [(input_name, input_type)]:
                raise ValueError("Unexpected ONNX input signature")
            if "pooler_output" not in outputs:
                raise ValueError("Missing ONNX pooler_output")
        except Exception as error:
            raise ModelUnavailable(f"Could not load {name}: {error}") from error
        return session

    def image_vectors(self, paths: list[Path]) -> tuple[list, list[str | None]]:
        import numpy as np
        from PIL import Image

        size = self.settings.image_size
        batch = np.empty((len(paths), 3, size, size), dtype=np.float32)
        positions = []
        vectors: list = [None] * len(paths)
        errors: list[str | None] = [None] * len(paths)
        for position, path in enumerate(paths):
            try:
                with Image.open(path) as preview:
                    preview.draft("RGB", (size, size))
                    with preview.resize((size, size), Image.Resampling.BILINEAR).convert(
                        "RGB"
                    ) as image:
                        batch[len(positions)] = np.asarray(image).transpose(2, 0, 1)
                positions.append(position)
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                errors[position] = f"Could not read the cached preview: {error}"
        if positions:
            if self.vision is None:
                self.vision = self._session(VISION, "pixel_values", "tensor(float)")
            pixels = batch[: len(positions)]
            pixels *= 2 / 255
            pixels -= 1
            try:
                result = self.vision.run(["pooler_output"], {"pixel_values": pixels})[0]
                if result.ndim != 2 or len(result) != len(positions):
                    raise ValueError("Unexpected image embedding shape. Check ml.image_size.")
            except Exception as error:
                raise ModelUnavailable(f"Image encoder failed: {error}") from error
            for position, vector in zip(positions, result, strict=True):
                vectors[position] = vector
        return vectors, errors

    def text_vectors(self, texts: list[str]):
        import numpy as np

        try:
            if self.tokenizer is None:
                from tokenizers import Tokenizer

                self.tokenizer = Tokenizer.from_file(
                    str(model_file(self.settings, self.cache, TOKENIZER))
                )
                self.tokenizer.enable_truncation(max_length=64)
            if self.text is None:
                self.text = self._session(TEXT, "input_ids", "tensor(int64)")
            formatted = [" ".join(text.lower().split()) for text in texts]
            tokens = self.tokenizer.encode_batch(formatted)
            ids = np.array([token.ids for token in tokens], dtype=np.int64)
            if ids.shape != (len(texts), 64):
                raise ValueError("Tokenizer must provide 64 padded input IDs per prompt")
            return self.text.run(["pooler_output"], {"input_ids": ids})[0]
        except ModelUnavailable:
            raise
        except Exception as error:
            raise ModelUnavailable(f"Text encoder failed: {error}") from error

    def release_text(self) -> None:
        self.text = None
        self.tokenizer = None


def set_native_threads() -> None:
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
