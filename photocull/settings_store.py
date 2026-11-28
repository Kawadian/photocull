from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from photocull.config import Settings, configuration, resolve_settings

LIMIT = 256 * 1024
REVISION = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{12}")
EGRESS = "network.egress_allow"
DEFAULT_EGRESS = ["openrouter.ai"]
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
BOOTSTRAP_REVISION = "base"


class SettingsError(ValueError):
    pass


class SettingsConflict(SettingsError):
    pass


def fields(model: type[BaseModel], prefix: str = "") -> dict:
    result = {}
    for name, field in model.model_fields.items():
        key = f"{prefix}{name}"
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel):
            result.update(fields(field.annotation, f"{key}."))
        else:
            result[key] = model.model_json_schema(mode="validation")["properties"][name]
    return result


FIELDS = fields(Settings) | {EGRESS: {"type": "array", "items": {"type": "string"}}}


def safe_url(value: str) -> str:
    # Compatible API prefixes may include path components. Reject URL syntax
    # that can carry credentials or change the authority between parsers.
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not re.fullmatch(r"[0-9A-Za-z.:[\]-]+", parsed.netloc)
            or parsed.port == 0
            or not re.fullmatch(r"[0-9A-Za-z._~/\-]*", parsed.path)
            or any(part in {".", ".."} for part in parsed.path.split("/"))
            or "?" in value
            or "#" in value
            or any(not 33 <= ord(char) <= 126 for char in value)
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SettingsError("認証情報を含まない HTTP(S) の URL を入力してください。") from None
    return value.rstrip("/")


def target_approved(value: str, env: dict, baseline: str) -> bool:
    if not value:
        return False
    for trusted in (baseline, *(env.get("PHOTOCULL_SETTINGS_ENDPOINTS") or "").split(",")):
        try:
            if value == safe_url(trusted.strip()):
                return True
        except SettingsError:
            continue
    return False


def egress_list(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise SettingsError("egress の許可リストに DNS ホスト名を入力してください。")
    hosts = []
    for item in value:
        if not isinstance(item, str):
            raise SettingsError("egress の許可リストに DNS ホスト名を入力してください。")
        host = item.strip().lower().removesuffix(".")
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or not any("a" <= char <= "z" for char in labels[-1])
            or not all(DNS_LABEL.fullmatch(label) for label in labels)
        ):
            raise SettingsError("egress の許可リストに DNS ホスト名を入力してください。")
        if host not in hosts:
            hosts.append(host)
    return hosts


def values_for(settings: Settings) -> dict:
    values = settings.model_dump(mode="json")
    values["vlm"]["openai"].update(
        base_url=settings.vlm.openai.base_url.get_secret_value(),
        model=settings.vlm.openai.model,
    )
    result = {}
    for key in FIELDS:
        if key == EGRESS:
            continue
        value = values
        for name in key.split("."):
            value = value[name]
        result[key] = value
    return result


DEFAULTS = values_for(Settings())
# The schema resolves explicit paths at load time, but this is the YAML default.
DEFAULTS.update(state_dir=".photocull", **{EGRESS: DEFAULT_EGRESS})


def insert_overrides(base: dict, changes: dict) -> dict:
    values = deepcopy(base)
    for key, value in changes.items():
        if key == EGRESS:
            continue
        *parents, field = key.split(".")
        section = values
        for name in parents:
            section = section.setdefault(name, {})
        section[field] = value
    return values


@dataclass(frozen=True)
class Snapshot:
    revision: str
    overrides: dict
    settings: Settings
    sources: dict
    egress: list[str]
    backup: str | None = None

    def values(self) -> dict:
        return values_for(self.settings) | {EGRESS: self.egress}


class SettingsStore:
    def __init__(self, raw: dict, config: Path, cwd: Path, env: dict):
        self.raw = raw
        self.config = config
        self.cwd = cwd
        self.env = env
        self.baseline = self.evaluate(BOOTSTRAP_REVISION, {})
        # Deliberately fixed by the underlying YAML/env even if the UI moves state_dir.
        self.directory = self.baseline.settings.state_dir / "settings"
        self.active = self.directory / "active.json"
        self.backups = self.directory / "history"

    @classmethod
    def from_config(cls) -> SettingsStore:
        return cls(*configuration())

    def evaluate(self, revision: str, overrides: dict) -> Snapshot:
        if not isinstance(overrides, dict) or overrides.keys() - FIELDS.keys():
            raise SettingsError("未対応の設定キーが含まれています。")
        self.reject_secret(overrides)
        if EGRESS in overrides:
            egress_list(overrides[EGRESS])
        base_url = overrides.get("vlm.openai.base_url", "")
        if not isinstance(base_url, str):
            raise SettingsError("エンドポイント URL を入力してください。")
        if base_url:
            clean = safe_url(base_url)
            # Keep admin-approved local endpoints possible while protecting API probes
            # and workers from turning an editable URL into arbitrary network access.
            if not self.env.get("PHOTOCULL_VLM_BASE_URL") and not target_approved(
                clean, self.env, self.trusted_endpoint
            ):
                raise SettingsError(
                    "エンドポイントを PHOTOCULL_SETTINGS_ENDPOINTS で許可してください。"
                )
        sources: dict = {}
        try:
            settings = resolve_settings(
                insert_overrides(self.raw, overrides), self.config, self.cwd, self.env, sources
            )
        except ValidationError as error:
            known = []
            for issue in error.errors(
                include_input=False, include_context=False, include_url=False
            ):
                path = ".".join(str(part) for part in issue["loc"] if isinstance(part, str))
                matches = [key for key in FIELDS if path == key or path.startswith(f"{key}.")]
                if matches:
                    known.append(max(matches, key=len))
            suffix = f" ({', '.join(dict.fromkeys(known))})" if known else ""
            raise SettingsError(
                f"設定値の型、範囲、または項目の組み合わせを確認してください。{suffix}"
            ) from None
        except (ValueError, TypeError, AttributeError, OSError, RuntimeError):
            if revision == BOOTSTRAP_REVISION and not overrides:
                raise
            raise SettingsError(
                "設定値の型、範囲、または項目の組み合わせを確認してください。"
            ) from None

        if overrides and any(
            self.directory.is_relative_to(root.resolve())
            or root.resolve().is_relative_to(self.directory)
            for lib in settings.libraries.values()
            for root in lib.roots
        ):
            raise SettingsError("設定の保存先はライブラリの外に置いてください。")

        raw_egress = self.env.get("PHOTOCULL_EGRESS_ALLOW")
        egress = overrides.get(EGRESS, DEFAULT_EGRESS)
        if EGRESS in overrides:
            egress = egress_list(egress)
        if raw_egress and raw_egress.strip():
            sources[EGRESS] = "PHOTOCULL_EGRESS_ALLOW"
            try:
                egress = egress_list(raw_egress.split(","))
            except SettingsError:
                egress = []
        return Snapshot(revision, overrides, settings, sources, egress)

    @property
    def trusted_endpoint(self) -> str:
        if hasattr(self, "baseline"):
            return self.baseline.settings.vlm.openai.base_url.get_secret_value()
        return ""

    def reject_secret(self, value: object) -> None:
        key = self.api_key

        def contains_secret(item: object) -> bool:
            if isinstance(item, str):
                if key and (item == key or (len(key) >= 8 and key in item)):
                    return True
                for url in re.findall(r"(?i)https?://[^\s]+", item):
                    try:
                        parsed = urlsplit(url)
                        if (
                            parsed.username is not None
                            or parsed.password is not None
                            or parsed.query
                        ):
                            return True
                    except ValueError:
                        return True
                return False
            if isinstance(item, list):
                return any(contains_secret(member) for member in item)
            if isinstance(item, dict):
                return any(contains_secret(k) or contains_secret(v) for k, v in item.items())
            return False

        if contains_secret(value):
            raise SettingsError("秘密情報は環境変数で設定してください。")

    @property
    def api_key(self) -> str:
        # The provider reads the process environment rather than dotenv.
        return os.environ.get("PHOTOCULL_VLM_API_KEY", "")

    def _check_storage(self) -> None:
        for path in (self.directory, self.backups):
            if path.is_symlink():
                raise SettingsError("設定の保存先にシンボリックリンクは使用できません。")

    def _record(self, path: Path) -> tuple[str, dict, str | None]:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
            encoded = handle.read(LIMIT + 1)
        if len(encoded) > LIMIT:
            raise SettingsError("設定ファイルのサイズ上限を超えています。")
        try:
            value = json.loads(encoded)
            revision, overrides = value["revision"], value["overrides"]
            backup = value.get("backup")
            if backup is not None and (
                not isinstance(backup, str) or not REVISION.fullmatch(backup)
            ):
                raise ValueError
            if not isinstance(revision, str) or not (
                REVISION.fullmatch(revision) or revision == BOOTSTRAP_REVISION
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, RecursionError):
            raise SettingsError("設定ファイルの形式を確認してください。") from None
        return revision, overrides, backup

    def read(self) -> Snapshot:
        self._check_storage()
        try:
            revision, overrides, backup = self._record(self.active)
        except FileNotFoundError:
            return self.baseline
        return replace(self.evaluate(revision, overrides), backup=backup)

    def history(self, snapshot: Snapshot) -> list[str]:
        # Follow committed versions. A crash before replacing active.json
        # can leave an orphan backup, which must not appear as a revision.
        history = []
        backup = snapshot.backup
        while backup and backup not in history and len(history) < 100:
            path = self.backups / f"{backup}.json"
            try:
                _, _, parent = self._record(path)
            except FileNotFoundError:
                break
            history.append(backup)
            backup = parent
        return history

    def previous(self, current: Snapshot, revision: str) -> Snapshot:
        if revision not in self.history(current):
            raise SettingsError("指定された履歴が見つかりません。")
        version, overrides, backup = self._record(self.backups / f"{revision}.json")
        return replace(self.evaluate(version, overrides), backup=backup)

    @contextmanager
    def transaction(self):
        self._check_storage()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(
            os.open(self.directory / ".lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600),
            "w",
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self.read()

    def commit(self, before: Snapshot, after: Snapshot) -> None:
        if before.overrides == after.overrides:
            return
        self.backups.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        revision = f"{stamp}-{uuid4().hex[:12]}"
        active = self._encode(revision, after.overrides, revision)
        self._atomic(
            self.backups / f"{revision}.json",
            self._encode(before.revision, before.overrides, before.backup),
        )
        self._atomic(self.active, active)

    @staticmethod
    def _encode(revision: str, overrides: dict, backup: str | None) -> bytes:
        record = {"revision": revision, "overrides": overrides, "backup": backup}
        encoded = (json.dumps(record, allow_nan=False) + "\n").encode()
        if len(encoded) > LIMIT:
            raise SettingsError("設定ファイルのサイズ上限を超えています。")
        return encoded

    @staticmethod
    def _atomic(path: Path, encoded: bytes) -> None:
        with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                os.replace(temporary, path)
                fd = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                temporary.unlink(missing_ok=True)
