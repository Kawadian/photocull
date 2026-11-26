from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.request import urlopen

from photocull.config import DEFAULT_FACE_REVISION

FACE_FILES = {
    "face_detection_yunet/face_detection_yunet_2023mar.onnx": (
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        232589,
    ),
    "face_recognition_sface/face_recognition_sface_2021dec.onnx": (
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        38696353,
    ),
}


def cache_bytes(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def face_files(cache: Path, revision: str, offline: bool) -> list[Path]:
    def verified(path: Path, expected: str, size: int) -> bool:
        if not path.is_file() or path.stat().st_size != size:
            return False
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == expected

    results = []
    for name, (digest, size) in FACE_FILES.items():
        target = cache / "opencv-faces" / revision / Path(name).name
        if revision != DEFAULT_FACE_REVISION:
            # Custom revisions are manually provisioned, with a new immutable identity.
            if not target.is_file():
                raise ValueError(f"Install the custom face model in {target}")
        elif not verified(target, digest, size):
            if offline:
                raise ValueError(f"Face model missing or corrupt in offline cache: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{revision}/models/{name}"
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
                temp = Path(staged.name)
                try:
                    with urlopen(url, timeout=30) as response:
                        # Accept at most one byte beyond the pinned content length.
                        remaining = size + 1
                        while remaining and (chunk := response.read(min(remaining, 1024 * 1024))):
                            staged.write(chunk)
                            remaining -= len(chunk)
                    staged.flush()
                    if not verified(temp, digest, size):
                        raise ValueError(f"Face model checksum mismatch: {name}")
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        results.append(target)
    return results


def whisper_folder(cache: Path, model: str, revision: str, offline: bool) -> Path:
    from huggingface_hub import snapshot_download

    return Path(
        snapshot_download(
            model,
            revision=revision,
            cache_dir=cache / "whisper",
            local_files_only=offline,
            allow_patterns=[
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.*",
                "preprocessor_config.json",
            ],
        )
    )
