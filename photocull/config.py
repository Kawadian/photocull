from __future__ import annotations

import os
from hashlib import sha256
from json import dumps
from pathlib import Path
from typing import Annotated, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MediaStage = Literal["exif", "preview", "score"]
Stage = Literal["exif", "preview", "score", "embedding"]
STAGES: tuple[MediaStage, ...] = ("exif", "preview", "score")
LibraryName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]
Ratio = Annotated[float, Field(ge=0, le=1)]
TagLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
TagLabels = Annotated[tuple[TagLabel, ...], Field(min_length=1, max_length=4)]
DEFAULT_MODEL = "onnx-community/siglip2-base-patch16-224-ONNX"
DEFAULT_REVISION = "ba1f3b0843f24bc5417d38e19c37b287d719b2f4"
DEFAULT_TAGS = {
    "cat": ("Cat", "猫"),
    "dog": ("Dog", "犬"),
    "bird": ("Bird", "鳥"),
    "people": ("People", "人物"),
    "food": ("Food", "料理"),
    "flowers": ("Flowers", "花"),
    "mountains": ("Mountains", "山"),
    "ocean": ("Ocean", "海"),
    "snow": ("Snow", "雪"),
    "sunset": ("Sunset", "夕焼け"),
    "architecture": ("Architecture", "建築"),
    "street": ("Street scene", "街並み"),
}


def _fingerprint(value: object) -> str:
    return sha256(dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Library(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    allowed_roots: tuple[Path, ...] = ()

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.root, *self.allowed_roots)


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_mb: int = Field(default=1536, ge=128)
    batch_size: int = Field(default=8, ge=1, le=256)
    parallelism: int = Field(default=1, ge=1, le=16)
    stages: tuple[MediaStage, ...] = STAGES
    device: Literal["cpu", "cuda"] = "cpu"


class ScoringSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_sharpness: Ratio = 0.02
    min_exposure: Ratio = 0.08
    max_exposure: Ratio = 0.92
    max_clipped_highlights: Ratio = 0.25
    max_clipped_shadows: Ratio = 0.40

    @model_validator(mode="after")
    def validate_exposure(self) -> ScoringSettings:
        if self.min_exposure > self.max_exposure:
            raise ValueError("min_exposure must be at most max_exposure")
        return self


class BurstSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window_seconds: float = Field(default=2, gt=0, allow_inf_nan=False)
    max_hamming_distance: int = Field(default=8, ge=0, le=64)
    min_cosine: Ratio = 0.88


class MLSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    search_enabled: bool = True
    tags_enabled: bool = True
    model_id: str = Field(
        default=DEFAULT_MODEL,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$",
        max_length=160,
    )
    model_revision: str = Field(default=DEFAULT_REVISION, pattern=r"^[a-f0-9]{40}$")
    image_size: int = Field(default=224, ge=224, le=256, multiple_of=32)
    offline: bool = False
    batch_size: int = Field(default=4, ge=1, le=16)
    threads: int = Field(default=2, ge=1, le=8)
    keep_text_encoder: bool = True
    similar_threshold: Ratio = 0.90
    tag_top_k: int = Field(default=3, ge=1, le=12)
    tag_vocabulary: dict[LibraryName, TagLabels] = Field(default_factory=dict, max_length=128)

    @property
    def preprocessing(self) -> dict:
        return {
            "version": 1,
            "source": "oriented JPEG preview",
            "resize": [self.image_size, self.image_size],
            "resample": "bilinear",
            "jpeg_draft": True,
            "color": "RGB",
            "mean": 0.5,
            "std": 0.5,
            "text_length": 64,
            "text_cleaning": "lowercase and collapse whitespace",
            "storage": "L2-normalized float16 little-endian",
            "format": "siglip2-onnx-int8-split",
        }

    @property
    def model_key(self) -> str:
        return _fingerprint((self.model_id, self.model_revision, self.preprocessing))

    @property
    def vocabulary(self) -> dict[str, tuple[str, ...]]:
        return DEFAULT_TAGS | self.tag_vocabulary

    @property
    def tag_key(self) -> str:
        return _fingerprint((self.model_key, self.vocabulary, self.tag_top_k, 1))


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    libraries: dict[LibraryName, Library] = Field(default_factory=dict)
    state_dir: Path = Path(".photocull")
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8000, ge=1, le=65535)
    token_file: Path | None = None
    static_dir: Path = Path("frontend/dist")
    model_dir: Path | None = None
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    burst: BurstSettings = Field(default_factory=BurstSettings)
    ml: MLSettings = Field(default_factory=MLSettings)

    @model_validator(mode="after")
    def validate_state(self) -> Settings:
        state = self.state_dir.resolve()
        for name, library in self.libraries.items():
            if any(root.resolve().is_relative_to(state) for root in library.roots):
                raise ValueError(f"library {name} has a media root inside the state directory")
        return self

    def library_state(self, name: str) -> Path:
        return self.state_dir / "libraries" / name

    @property
    def model_cache(self) -> Path:
        return self.model_dir or self.state_dir / "models"


def _paths(values: dict, keys: tuple[str, ...], base: Path) -> None:
    for key in keys:
        if values.get(key) is not None:
            values[key] = (base / Path(values[key]).expanduser()).absolute()


def load_settings() -> Settings:
    cwd = Path.cwd()
    env = {**dotenv_values(cwd / ".env"), **os.environ}
    config = Path(env.get("PHOTOCULL_CONFIG") or "photocull.yaml").expanduser().absolute()
    values: dict = {}
    if config.exists() or env.get("PHOTOCULL_CONFIG"):
        with config.open(encoding="utf-8") as handle:
            values = yaml.safe_load(handle) or {}
        if not isinstance(values, dict):
            raise ValueError("Photocull configuration must be a YAML mapping")

    _paths(values, ("state_dir", "token_file", "static_dir", "model_dir"), config.parent)
    for library in values.get("libraries", {}).values():
        _paths(library, ("root",), config.parent)
        if "allowed_roots" in library:
            library["allowed_roots"] = [
                (config.parent / Path(root).expanduser()).absolute()
                for root in library["allowed_roots"]
            ]

    env_values = {
        field: env[key]
        for field in (
            "state_dir",
            "token_file",
            "static_dir",
            "model_dir",
            "bind_host",
            "bind_port",
        )
        if (key := f"PHOTOCULL_{field.upper()}") in env and env[key] is not None
    }
    if env_values.get("token_file") == "":
        env_values["token_file"] = None
    _paths(env_values, ("state_dir", "token_file", "static_dir", "model_dir"), cwd)
    values.update(env_values)

    if root := env.get("PHOTOCULL_LIBRARY_ROOT"):
        name = env.get("PHOTOCULL_LIBRARY_NAME") or "photos"
        allowed = env.get("PHOTOCULL_ALLOWED_ROOTS") or ""
        values.setdefault("libraries", {})[name] = {
            "root": (cwd / Path(root).expanduser()).absolute(),
            "allowed_roots": [
                (cwd / Path(path).expanduser()).absolute()
                for path in allowed.split(os.pathsep)
                if path
            ],
        }

    worker = values.setdefault("worker", {})
    for field in ("memory_mb", "batch_size", "parallelism", "device"):
        if (setting := env.get(f"PHOTOCULL_WORKER_{field.upper()}")) is not None:
            worker[field] = setting
    if (stages := env.get("PHOTOCULL_WORKER_STAGES")) is not None:
        worker["stages"] = [stage.strip() for stage in stages.split(",") if stage.strip()]

    for section, model in (
        ("scoring", ScoringSettings),
        ("burst", BurstSettings),
        ("ml", MLSettings),
    ):
        settings = values.setdefault(section, {})
        for field in model.model_fields.keys() - {"tag_vocabulary"}:
            if (value := env.get(f"PHOTOCULL_{section.upper()}_{field.upper()}")) is not None:
                settings[field] = value

    return Settings.model_validate(values)
