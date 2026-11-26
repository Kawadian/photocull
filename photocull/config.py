from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Stage = Literal["exif", "preview"]
LibraryName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]


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
    stages: tuple[Stage, ...] = ("exif", "preview")
    device: Literal["cpu", "cuda"] = "cpu"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    libraries: dict[LibraryName, Library] = Field(default_factory=dict)
    state_dir: Path = Path(".photocull")
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8000, ge=1, le=65535)
    token_file: Path | None = None
    static_dir: Path = Path("frontend/dist")
    worker: WorkerSettings = Field(default_factory=WorkerSettings)

    @model_validator(mode="after")
    def validate_state(self) -> Settings:
        state = self.state_dir.resolve()
        for name, library in self.libraries.items():
            if any(root.resolve().is_relative_to(state) for root in library.roots):
                raise ValueError(f"library {name} has a media root inside the state directory")
        return self

    def library_state(self, name: str) -> Path:
        return self.state_dir / "libraries" / name


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

    _paths(values, ("state_dir", "token_file", "static_dir"), config.parent)
    for library in values.get("libraries", {}).values():
        _paths(library, ("root",), config.parent)
        if "allowed_roots" in library:
            library["allowed_roots"] = [
                (config.parent / Path(root).expanduser()).absolute()
                for root in library["allowed_roots"]
            ]

    env_values = {
        field: env[key]
        for field in ("state_dir", "token_file", "static_dir", "bind_host", "bind_port")
        if (key := f"PHOTOCULL_{field.upper()}") in env and env[key] is not None
    }
    _paths(env_values, ("state_dir", "token_file", "static_dir"), cwd)
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

    return Settings.model_validate(values)
