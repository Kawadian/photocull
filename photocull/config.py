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
DEFAULT_FACE_REVISION = "48b10fa505aeb51fbd127fc60a5c660040e823b7"
DEFAULT_TRANSCRIPT_MODEL = "Systran/faster-whisper-base"
DEFAULT_TRANSCRIPT_REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
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
    scan_parallelism: int = Field(default=1, ge=1, le=8)
    scan_batch_size: int = Field(default=64, ge=1, le=512)
    hash_read_mib: int = Field(default=4, ge=1, le=16)
    scan_overlap: bool = False
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
    aesthetic_weight: Ratio = 0.25


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


class InsightSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    aesthetic: bool = False
    taste: bool = False
    faces: bool = False
    story: bool = False
    video: bool = False
    transcript: bool = False
    watch: bool = False
    export: bool = False
    report: bool = False
    offline: bool = False
    threads: int = Field(default=2, ge=1, le=8)
    face_revision: str = Field(default=DEFAULT_FACE_REVISION, pattern=r"^[a-f0-9]{40}$")
    face_threshold: Ratio = 0.45
    taste_min_per_class: int = Field(default=8, ge=2, le=1000)
    story_gap_minutes: int = Field(default=90, ge=1, le=1440)
    story_distance_km: float = Field(default=15, gt=0, le=1000)
    story_geonames: Path | None = None
    transcript_model: str = Field(
        default=DEFAULT_TRANSCRIPT_MODEL,
        pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$",
    )
    transcript_revision: str = Field(default=DEFAULT_TRANSCRIPT_REVISION, pattern=r"^[a-f0-9]{40}$")
    watch_mode: Literal["auto", "poll"] = "auto"
    watch_interval: float = Field(default=60, ge=1, le=86400)
    watch_debounce: float = Field(default=3, ge=0.1, le=300)
    export_roots: dict[LibraryName, Path] = Field(default_factory=dict)


class ArchiveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    write_roots: tuple[Path, ...] = ()
    export_roots: dict[LibraryName, Path] = Field(default_factory=dict)
    trash_roots: dict[LibraryName, Path] = Field(default_factory=dict)
    retention_days: int = Field(default=0, ge=0, le=36500)
    parallelism: int = Field(default=1, ge=1, le=4)
    threads: int = Field(default=2, ge=1, le=8)
    memory_mb: int = Field(default=1536, ge=256, le=16384)
    timeout_seconds: int = Field(default=86400, ge=5, le=604800)


class DerivativeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    hot_dir: Path | None = None
    large_dir: Path | None = None
    hot_mib: int = Field(default=1536, ge=16)
    large_mib: int = Field(default=4096, ge=16)
    avif_quality: int = Field(default=58, ge=1, le=100)
    webp_quality: int = Field(default=80, ge=1, le=100)
    jpeg_quality: int = Field(default=86, ge=1, le=100)
    prewarm: int = Field(default=64, ge=0, le=2048)
    original_download: bool = False
    original_streaming: bool = False
    accel_redirect: bool = False


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strip_location: bool = True

    def includes_location(self, override: bool | None) -> bool:
        return not self.strip_location if override is None else override


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
    insights: InsightSettings = Field(default_factory=InsightSettings)
    archive: ArchiveSettings = Field(default_factory=ArchiveSettings)
    derivatives: DerivativeSettings = Field(default_factory=DerivativeSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)

    @model_validator(mode="after")
    def validate_state(self) -> Settings:
        state = self.state_dir.resolve()
        for name, library in self.libraries.items():
            if any(root.resolve().is_relative_to(state) for root in library.roots):
                raise ValueError(f"library {name} has a media root inside the state directory")
        for name, destination in self.insights.export_roots.items():
            target = destination.resolve()
            for area in (
                state,
                *(root.resolve() for lib in self.libraries.values() for root in lib.roots),
            ):
                if target.is_relative_to(area) or area.is_relative_to(target):
                    raise ValueError(f"export root {name} overlaps a media or state directory")
        media = tuple(root.resolve() for lib in self.libraries.values() for root in lib.roots)
        for root in self.archive.write_roots:
            if not root.is_absolute() or root == Path("/") or root != root.resolve():
                raise ValueError("Archive write roots must be canonical absolute directories")
            if root.is_relative_to(state) or state.is_relative_to(root):
                raise ValueError("Archive write roots must be outside state")
        for name, root in self.archive.export_roots.items():
            if (
                not root.is_absolute()
                or root != root.resolve()
                or any(
                    root.is_relative_to(area) or area.is_relative_to(root)
                    for area in (state, *media)
                )
            ):
                raise ValueError(
                    f"Archive export root {name} overlaps media/state or is not canonical"
                )
        for name, root in self.archive.trash_roots.items():
            if (
                not root.is_absolute()
                or root != root.resolve()
                or not any(
                    root != area and root.is_relative_to(area) for area in self.archive.write_roots
                )
            ):
                raise ValueError(f"Archive trash root {name} must be inside an explicit write root")
            if any(root == area or area.is_relative_to(root) for area in media) or (
                any(root.is_relative_to(area) for area in media)
                and ".photocull-trash" not in root.parts
            ):
                raise ValueError("Archive trash inside media must use a .photocull-trash directory")
            if any(
                root.is_relative_to(area) or area.is_relative_to(root)
                for area in self.archive.export_roots.values()
            ):
                raise ValueError(f"Archive trash and export roots overlap for {name}")
        if self.derivatives.enabled:
            self._validate_derivative_roots(state, media)
        return self

    def _validate_derivative_roots(self, state: Path, media: tuple[Path, ...]) -> None:
        hot, large = (self.derivative_root(tier).resolve() for tier in ("hot", "large"))
        exports = (*self.insights.export_roots.values(), *self.archive.export_roots.values())
        areas = (*media, *(path.resolve() for path in exports), *self.archive.trash_roots.values())
        for cache in (hot, large):
            if state.is_relative_to(cache) or any(
                cache.is_relative_to(root) or root.is_relative_to(cache) for root in areas
            ):
                raise ValueError("Derivative cache must be outside media and export directories")
        if hot.is_relative_to(large) or large.is_relative_to(hot):
            raise ValueError("Derivative cache tiers must be separate directories")

    def library_state(self, name: str) -> Path:
        return self.state_dir / "libraries" / name

    def derivative_root(self, tier: Literal["hot", "large"]) -> Path:
        root = self.derivatives.hot_dir if tier == "hot" else self.derivatives.large_dir
        return root or self.state_dir / "derivatives" / tier

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

    insights = values.setdefault("insights", {})
    _paths(insights, ("story_geonames",), config.parent)
    if places := env.get("PHOTOCULL_INSIGHTS_STORY_GEONAMES"):
        insights["story_geonames"] = (cwd / Path(places).expanduser()).absolute()
    if roots := insights.get("export_roots"):
        _paths(roots, tuple(roots), config.parent)
    archive = values.setdefault("archive", {})
    derivatives = values.setdefault("derivatives", {})
    _paths(derivatives, ("hot_dir", "large_dir"), config.parent)
    for field in ("hot_dir", "large_dir"):
        if setting := env.get(f"PHOTOCULL_DERIVATIVES_{field.upper()}"):
            derivatives[field] = (cwd / Path(setting).expanduser()).absolute()
    for mapping in ("export_roots", "trash_roots"):
        if roots := archive.get(mapping):
            _paths(roots, tuple(roots), config.parent)
    if (roots := env.get("PHOTOCULL_WRITE_ROOTS")) is not None:
        archive["write_roots"] = [
            Path(root).expanduser() for root in roots.split(os.pathsep) if root
        ]
    elif "write_roots" in archive:
        archive["write_roots"] = [
            (config.parent / Path(root).expanduser()).absolute() for root in archive["write_roots"]
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
    for field in (
        "memory_mb",
        "batch_size",
        "parallelism",
        "scan_parallelism",
        "scan_batch_size",
        "hash_read_mib",
        "scan_overlap",
        "device",
    ):
        if (setting := env.get(f"PHOTOCULL_WORKER_{field.upper()}")) is not None:
            worker[field] = setting
    if (stages := env.get("PHOTOCULL_WORKER_STAGES")) is not None:
        worker["stages"] = [stage.strip() for stage in stages.split(",") if stage.strip()]

    for section, model in (
        ("scoring", ScoringSettings),
        ("burst", BurstSettings),
        ("ml", MLSettings),
        ("insights", InsightSettings),
        ("archive", ArchiveSettings),
        ("derivatives", DerivativeSettings),
        ("browser", BrowserSettings),
    ):
        settings = values.setdefault(section, {})
        for field in model.model_fields.keys() - {
            "tag_vocabulary",
            "story_geonames",
            "export_roots",
            "trash_roots",
            "write_roots",
            "hot_dir",
            "large_dir",
        }:
            if (value := env.get(f"PHOTOCULL_{section.upper()}_{field.upper()}")) is not None:
                settings[field] = value

    return Settings.model_validate(values)
