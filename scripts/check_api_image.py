"""Run inside the API runtime image with python - on standard input."""

import sys
from importlib.util import find_spec
from pathlib import Path

from photocull.api import create_app
from photocull.config import InsightSettings, MLSettings, Settings

FORBIDDEN = (
    "onnxruntime",
    "cv2",
    "faster_whisper",
    "ctranslate2",
    "watchdog",
    "av",
    "torch",
    "torchvision",
    "open_clip",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    "usearch",
    "numkong",
    "faiss",
    "hnswlib",
    "numpy",
    "PIL",
    "pillow_heif",
    "pyexiv2",
    "rawpy",
)
installed = [name for name in FORBIDDEN if find_spec(name) is not None]
assert not installed, f"Unexpected packages in the API image: {installed}"

app = create_app(
    Settings(
        ml=MLSettings(enabled=True),
        insights=InsightSettings(
            aesthetic=True, taste=True, faces=True, video=True, transcript=True
        ),
    )
)
assert any(route.path.endswith("/search") for route in app.routes)
assert not any(name.startswith("photocull.worker") for name in sys.modules)
assert set(FORBIDDEN).isdisjoint(name.split(".")[0] for name in sys.modules)
assert Path("frontend/dist/index.html").is_file()
