"""Pinned public fixtures and real CPU models, excluded from normal CI."""

import os
import subprocess
from pathlib import Path
from time import perf_counter
from urllib.request import urlretrieve

import pytest
from PIL import Image

from photocull.config import InsightSettings, MLSettings, Settings, WorkerSettings
from photocull.insights import AESTHETIC_PROMPTS
from photocull.worker.insights.model_cache import face_files, whisper_folder
from photocull.worker.ml.model import FILES, model_file
from photocull.worker.ml.process import InferenceProcess

ASTRONAUT = (
    "https://raw.githubusercontent.com/scikit-image/scikit-image/v0.25.2/skimage/data/astronaut.png"
)
SPEECH = (
    "https://raw.githubusercontent.com/openai/whisper/86098128c0b4f24f0e2aa2994de830614b474227/"
    "tests/jfk.flac"
)


def fixture(tmp_path, name, url):
    if directory := os.getenv("PHOTOCULL_TEST_FIXTURES"):
        path = Path(directory) / name
        if path.is_file():
            return path
    target = tmp_path / name
    urlretrieve(url, target)
    return target


def peak_rss(process):
    status = Path(f"/proc/{process.pid}/status").read_text()
    return (
        int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmHWM:")))
        / 1024
    )


@pytest.mark.phase4_model
def test_real_insight_models_on_cpu(tmp_path):
    settings = Settings(
        state_dir=tmp_path / "state",
        model_dir=Path(os.getenv("PHOTOCULL_MODEL_DIR", ".photocull/models")).resolve(),
        worker=WorkerSettings(memory_mb=3072),
        ml=MLSettings(enabled=True, offline=os.getenv("PHOTOCULL_ML_OFFLINE") == "1"),
        insights=InsightSettings(
            faces=True,
            aesthetic=True,
            video=True,
            transcript=True,
            offline=os.getenv("PHOTOCULL_INSIGHTS_OFFLINE") == "1",
        ),
    )
    config, cache = settings.insights, settings.model_cache
    siglip = [model_file(settings.ml, cache, path) for path in FILES]
    faces = face_files(cache, config.face_revision, config.offline)
    whisper = whisper_folder(
        cache, config.transcript_model, config.transcript_revision, config.offline
    )
    for label, files in (
        ("SigLIP", siglip),
        ("OpenCV faces", faces),
        ("Whisper base", whisper.iterdir()),
    ):
        print(f"{label} model files: {sum(path.stat().st_size for path in files) / 2**20:.2f} MiB")
    preview = tmp_path / "astronaut.jpg"
    with Image.open(fixture(tmp_path, "astronaut.png", ASTRONAUT)) as source:
        source.convert("RGB").save(preview)
    samples = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(fixture(tmp_path, "jfk.flac", SPEECH)),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    runtime = InferenceProcess(settings)

    def pulse():
        pass

    try:
        images = runtime.call("images", ([preview],), pulse)
        assert len(images[0][0]) == 1536
        start = perf_counter()
        weights = runtime.call("aesthetic_head", (AESTHETIC_PROMPTS,), pulse)
        assert len(weights) == 3072
        print(
            f"SigLIP aesthetic head construction {perf_counter() - start:.3f} s, "
            f"peak {peak_rss(runtime.process):.1f} MiB"
        )

        arguments = (preview, config.face_revision, config.offline, config.threads)
        start = perf_counter()
        found = runtime.call("faces", arguments, pulse)
        print(f"YuNet + SFace cold {perf_counter() - start:.3f} s")
        assert found and len(found[0][-1]) == 256
        start = perf_counter()
        for _ in range(12):
            assert runtime.call("faces", arguments, pulse)
        print(
            f"YuNet + SFace warm {(perf_counter() - start) / 12:.3f} s/photo, "
            f"peak {peak_rss(runtime.process):.1f} MiB"
        )

        start = perf_counter()
        text = runtime.call(
            "transcript",
            (
                samples,
                config.transcript_model,
                config.transcript_revision,
                config.offline,
                config.threads,
            ),
            pulse,
        )
        joined = " ".join(segment[-1].lower() for segment in text)
        assert "country" in joined and "ask" in joined, joined
        print(
            f"Whisper base int8 {len(samples) / 32000:.2f} s audio in "
            f"{perf_counter() - start:.3f} s, "
            f"combined child peak {peak_rss(runtime.process):.1f} MiB"
        )
    finally:
        runtime.close()
