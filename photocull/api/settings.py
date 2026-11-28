from __future__ import annotations

import json
from contextlib import nullcontext
from hashlib import sha256
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from photocull.config import OpenAISettings, Settings, VLMSettings
from photocull.live_vlm import LIVE_VLM_FIELDS, PROVIDER_FIELDS, LiveVLM
from photocull.restart import SERVICES, RestartBridge, RestartError
from photocull.settings_paths import PATH_FIELDS, PathBrowser
from photocull.settings_store import (
    DEFAULTS,
    EGRESS,
    FIELDS,
    LIMIT,
    SettingsConflict,
    SettingsError,
    SettingsStore,
    Snapshot,
    egress_list,
    safe_url,
    target_approved,
    values_for,
)
from photocull.vlm import OpenAIProvider, ProviderError

Revision = Annotated[str, Field(max_length=50)]
PathString = Annotated[str, Field(max_length=4096)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


class Change(Body):
    revision: Revision
    changes: dict[str, object] = Field(max_length=len(FIELDS))


class Apply(Body):
    revision: Revision


class Restore(Body):
    revision: Revision
    backup: Revision


class Browse(Body):
    field: str = Field(max_length=80)
    path: PathString = ""
    after: str = Field(default="", max_length=255)


class Inspect(Body):
    field: str = Field(max_length=80)
    path: PathString


class Probe(Body):
    base_url: str = Field(max_length=2048)
    model: str = Field(default="", max_length=256)


async def body(request: Request, model):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON で送信してください。")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > LIMIT:
            raise HTTPException(413, "設定のサイズ上限を超えています。")
    try:
        return model.model_validate(json.loads(content))
    except (ValidationError, ValueError, RecursionError):
        raise HTTPException(422, "リクエストの形式を確認してください。") from None


def _schema(key: str) -> dict:
    raw = FIELDS[key]
    shape = next((item for item in raw.get("anyOf", []) if item.get("type") != "null"), raw)
    return {
        name: shape[name]
        for name in (
            "type",
            "enum",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minLength",
            "maxLength",
            "pattern",
        )
        if name in shape
    } | {"nullable": "anyOf" in raw, "path": PATH_FIELDS.get(key)}


def settings_router(
    store: SettingsStore, running: Settings, provider: OpenAIProvider, live_vlm: LiveVLM
) -> APIRouter:
    router = APIRouter(prefix="/api/settings")
    paths = PathBrowser(
        running,
        store.baseline.settings,
        store.env.get("PHOTOCULL_SETTINGS_ROOTS") or "",
        store.directory,
    )
    running_values = values_for(running)
    environment = store.env
    isolated = environment.get("PHOTOCULL_WORKER_PROXY_MODE") == "egress"
    network_mode = environment.get("PHOTOCULL_WORKER_NETWORK_MODE") or "none"
    if network_mode not in {"none", "host", "bridge"}:
        network_mode = "custom"
    managed_egress = environment.get("PHOTOCULL_SETTINGS_EGRESS_MANAGED") == "1"
    bridge = RestartBridge(
        store.directory / "restart", environment.get("PHOTOCULL_SETTINGS_RESTART_SERVICES") or ""
    )

    def egress_digest(snapshot: Snapshot) -> str:
        return sha256(json.dumps(sorted(snapshot.egress)).encode()).hexdigest()

    def is_live(key: str, snapshot: Snapshot, effective: VLMSettings) -> bool:
        if key not in LIVE_VLM_FIELDS:
            return False
        if key in PROVIDER_FIELDS and not live_vlm.same_endpoint(snapshot.settings.vlm):
            kind = key.removeprefix("vlm.")
            return getattr(snapshot.settings.vlm, kind) == getattr(effective, kind)
        return True

    def running_value(key: str, effective: VLMSettings):
        if key.startswith("vlm.openai.") and key in LIVE_VLM_FIELDS:
            return getattr(effective.openai, key.removeprefix("vlm.openai."))
        if key in LIVE_VLM_FIELDS:
            return getattr(effective, key.removeprefix("vlm."))
        return running_values[key]

    def restart_info(snapshot: Snapshot) -> dict:
        try:
            allowed, result, applied_egress, busy = bridge.inspect()
        except (RestartError, OSError, KeyError, TypeError):
            allowed, result, applied_egress, busy = (), None, None, False
        current = snapshot.values()
        effective = live_vlm.read()
        pending = {
            key
            for key in FIELDS
            if key != EGRESS
            and not is_live(key, snapshot, effective)
            and current[key] != running_value(key, effective)
        }
        targets = {"api", "worker"} if pending else set()
        if EGRESS not in snapshot.sources and (
            egress_digest(snapshot) != (applied_egress or egress_digest(store.baseline))
        ):
            pending.add(EGRESS)
            targets.add("egress-proxy")
        if result:
            request = result["request"]
            result = {
                **result,
                "request": {key: value for key, value in request.items() if key != "egress"},
            }
        return {
            "services": [name for name in SERVICES if name in targets],
            "pending_fields": sorted(pending),
            "automatic": bool(targets and targets <= set(allowed))
            and ("egress-proxy" not in targets or managed_egress)
            and (result is None or result["status"] != "expired"),
            "busy": busy,
            "result": result,
        }

    def public(value: object, key: str = ""):
        try:
            store.reject_secret(value)
            if key == "insights.people_min_faces" and isinstance(value, int):
                return str(value)
            if key == "vlm.openai.base_url" and value:
                return safe_url(value)
        except SettingsError:
            return None
        return value

    def warnings(snapshot: Snapshot) -> list[str]:
        notices = []
        url = public(snapshot.values()["vlm.openai.base_url"], "vlm.openai.base_url")
        if isinstance(url, str) and url and urlsplit(url).hostname not in snapshot.egress:
            notices.append("エンドポイントのホストは egress 許可リストに含まれていません。")
        if (
            (snapshot.settings.vlm.tags == "openai" or snapshot.settings.vlm.flags == "openai")
            and not isolated
            and network_mode == "none"
        ):
            notices.append(
                "ワーカーのネットワークが無効です。リモート判定には egress 構成が必要です。"
            )
        if snapshot.settings.archive != running.archive and (
            snapshot.settings.archive.write_roots != running.archive.write_roots
            or snapshot.settings.archive.trash_roots != running.archive.trash_roots
            or snapshot.settings.archive.retention_days != running.archive.retention_days
        ):
            notices.append("アーカイブの書き込み範囲・隔離先・自動削除の条件を確認してください。")
        if snapshot.settings.token_file != running.token_file:
            notices.append(
                "トークンファイルを変更すると、再起動後に管理画面に入れなくなる場合があります。"
            )
        return notices

    def snapshot_payload(snapshot: Snapshot) -> dict:
        values = snapshot.values()
        effective = live_vlm.read()
        restart = restart_info(snapshot)
        return {
            "restart": restart,
            "revision": snapshot.revision,
            "fields": {
                key: _schema(key)
                | {
                    "value": public(values[key], key),
                    "default": public(DEFAULTS[key], key),
                    "running": public(running_value(key, effective), key)
                    if key != EGRESS
                    else None,
                    "source": snapshot.sources.get(key)
                    or ("ui" if key in snapshot.overrides else "yaml"),
                    "restart": (
                        "live"
                        if is_live(key, snapshot, effective)
                        else "egress-proxy"
                        if key == EGRESS
                        else "api-worker"
                    ),
                    "pending": key in restart["pending_fields"],
                }
                for key in FIELDS
            },
            "history": store.history(snapshot),
            "locked_libraries": {
                key.removeprefix("libraries."): value
                for key, value in snapshot.sources.items()
                if key.startswith("libraries.")
            },
            "api_key_configured": bool(store.api_key),
            "network": {
                "worker": "isolated" if isolated else network_mode,
                "proxy": "egress" if isolated else "none",
                "proxy_reads_saved_settings": managed_egress,
            },
            "warnings": warnings(snapshot),
        }

    def compare(before: Snapshot, after: Snapshot) -> dict:
        old = before.values()
        return {
            "changes": [
                {"key": key, "before": public(old[key], key), "after": public(value, key)}
                for key, value in after.values().items()
                if old[key] != value
                or (
                    key not in after.sources
                    and before.overrides.get(key, ...) != after.overrides.get(key, ...)
                )
            ],
            "warnings": warnings(after),
        }

    def check_revision(actual: Snapshot, expected: str) -> None:
        if actual.revision != expected:
            raise SettingsConflict(
                "設定が更新されました。画面を再読み込みしてから編集してください。"
            )

    def candidate(before: Snapshot, proposed: dict) -> Snapshot:
        changes = dict(proposed)
        store.reject_secret(changes)
        for key in changes:
            if key in before.sources:
                raise SettingsError("環境変数で上書き中の項目は変更できません。")
        for field in ("state_dir", "static_dir"):
            if changes.get(field) == DEFAULTS[field]:
                changes[field] = str(store.cwd / DEFAULTS[field])
        changes = before.overrides | changes
        if changes.get("vlm.openai.base_url"):
            changes["vlm.openai.base_url"] = safe_url(changes["vlm.openai.base_url"])
        if EGRESS in changes:
            changes[EGRESS] = egress_list(changes[EGRESS])
        if "libraries" in proposed and isinstance(changes["libraries"], dict):
            libraries = changes["libraries"]
            for path in before.sources:
                if path.startswith("libraries."):
                    name = path.removeprefix("libraries.")
                    if name in libraries and before.values()["libraries"][name] != libraries[name]:
                        raise SettingsError("環境変数で指定されたライブラリは変更できません。")
                    changes["libraries"] = {
                        key: val for key, val in libraries.items() if key != name
                    }
        result = store.evaluate(before.revision, changes)
        # Store validated types, retaining explicitly supplied (not resolved)
        # paths until their lexical form and mount permissions have been checked.
        checked_paths = paths.validate(before, result, proposed.keys())

        def canonicalize(value, checked):
            if isinstance(value, str):
                return checked.get(value, value)
            if isinstance(value, dict):
                return {key: canonicalize(item, checked) for key, item in value.items()}
            if isinstance(value, list):
                return [canonicalize(item, checked) for item in value]
            return value

        if checked_paths:
            changes.update(
                {
                    key: canonicalize(changes[key], checked)
                    for key, checked in checked_paths.items()
                    if key in changes
                }
            )
            result = store.evaluate(before.revision, changes)
        normalized = result.values()
        for key in changes.keys() - result.sources.keys():
            if key == "libraries":
                changes[key] = {
                    name: lib
                    for name, lib in normalized[key].items()
                    if f"libraries.{name}" not in result.sources
                }
            else:
                changes[key] = normalized[key]
        return store.evaluate(before.revision, changes)

    def guarded(function, *args):
        try:
            return function(*args)
        except SettingsConflict as error:
            raise HTTPException(409, str(error)) from None
        except SettingsError as error:
            raise HTTPException(422, str(error)) from None
        except FileNotFoundError:
            raise HTTPException(404, "設定の履歴またはパスが見つかりません。") from None
        except (OSError, ValueError, RecursionError):
            raise HTTPException(503, "設定ストレージを確認してください。") from None

    def ensure_idle(snapshot: Snapshot) -> None:
        if restart_info(snapshot)["busy"]:
            raise SettingsConflict("再起動の結果が出るまで設定の変更をお待ちください。")

    def apply(value: Apply):
        with store.transaction() as current:
            check_revision(current, value.revision)
            restart = restart_info(current)
            if restart["busy"]:
                raise SettingsConflict("再起動はすでに要求されています。")
            if not restart["automatic"]:
                raise SettingsConflict("対象サービスをホストから再起動してください。")
            targets = tuple(restart["services"])
            digest = egress_digest(current) if "egress-proxy" in targets else None
            try:
                bridge.submit(current.revision, targets, digest)
            except (RestartError, OSError):
                raise HTTPException(503, "ホストの再起動受付を確認してください。") from None
            return restart_info(current)

    def update(value: Change, save: bool):
        with store.transaction() if save else nullcontext(store.read()) as current:
            check_revision(current, value.revision)
            after = candidate(current, value.changes)
            if save:
                ensure_idle(current)
                store.commit(current, after)
                return snapshot_payload(store.read())
            return compare(current, after)

    def restore_revision(value: Restore, save: bool):
        with store.transaction() if save else nullcontext(store.read()) as current:
            check_revision(current, value.revision)
            after = store.previous(current, value.backup)
            paths.validate(current, after)
            if save:
                ensure_idle(current)
                store.commit(current, after)
                return snapshot_payload(store.read())
            return compare(current, after)

    def remote(value: Probe):
        current = store.read()
        saved = current.settings.vlm.openai
        url = safe_url(value.base_url)
        store.reject_secret({"url": url, "model": value.model})
        if not target_approved(url, environment, store.trusted_endpoint):
            raise SettingsError(
                "エンドポイントを PHOTOCULL_SETTINGS_ENDPOINTS で許可してください。"
            )
        if "vlm.openai.base_url" in current.sources and url != public(
            saved.base_url.get_secret_value(), "vlm.openai.base_url"
        ):
            raise SettingsError("環境変数で指定された URL を使用してください。")
        options = OpenAISettings(
            **live_vlm.read().openai.model_dump(), base_url=url, model=value.model or "models"
        )
        return provider.with_config(options)

    @router.get("")
    def read():
        return guarded(lambda: snapshot_payload(store.read()))

    @router.get("/restart")
    def restart_status():
        return guarded(lambda: restart_info(store.read()))

    @router.post("/restart", status_code=202)
    async def queue_restart(request: Request):
        return await run_in_threadpool(guarded, apply, await body(request, Apply))

    @router.post("/preview")
    async def preview(request: Request):
        return await run_in_threadpool(guarded, update, await body(request, Change), False)

    @router.put("")
    async def save(request: Request):
        return await run_in_threadpool(guarded, update, await body(request, Change), True)

    @router.post("/history/preview")
    async def preview_restore(request: Request):
        return await run_in_threadpool(
            guarded, restore_revision, await body(request, Restore), False
        )

    @router.post("/history/restore")
    async def restore(request: Request):
        return await run_in_threadpool(
            guarded, restore_revision, await body(request, Restore), True
        )

    def browse_paths(value: Browse):
        return paths.list(value.field, value.path, value.after)

    def inspect_path(value: Inspect):
        return paths.inspect(value.field, value.path)

    @router.post("/paths")
    async def browse(request: Request):
        result = await run_in_threadpool(guarded, browse_paths, await body(request, Browse))
        return public(result) or {"roots": [], "entries": [], "crumbs": []}

    @router.post("/paths/check")
    async def check(request: Request):
        result = await run_in_threadpool(guarded, inspect_path, await body(request, Inspect))
        if public(result) is None:
            raise HTTPException(422, "指定されたパスは使用できません。")
        return result

    def probe(value: Probe, models: bool):
        provider = guarded(remote, value)
        if not models:
            if not value.model.strip():
                raise HTTPException(422, "モデルを入力してください。")
            return {"ok": provider.test_connection()["ok"]}
        try:
            return {"items": [item for item in provider.models() if public(item) is not None]}
        except ProviderError:
            return JSONResponse(
                {"detail": "モデル一覧を取得できません。URL とアクセス制限を確認してください。"},
                status_code=502,
            )

    @router.post("/models")
    async def models(request: Request):
        return await run_in_threadpool(probe, await body(request, Probe), True)

    @router.post("/test")
    async def test(request: Request):
        return await run_in_threadpool(probe, await body(request, Probe), False)

    return router
