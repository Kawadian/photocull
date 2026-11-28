from __future__ import annotations

import fcntl
import http.client
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import SplitResult, urlsplit
from urllib.request import proxy_bypass_environment

from pydantic import BaseModel, ConfigDict, Field

from photocull.config import DecisionKind, MLSettings, OpenAISettings
from photocull.db import connect, write

MAX_RESPONSE = 65_536
REQUEST_ATTEMPTS = 3
IMAGE_RECIPE = "rgb-jpeg-768-q82-v1"
PROBE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAa0lEQVR42u3asQ3AIAwEQIjYyMN5Su/E"
    "BlAQKUK5b934ZJffM7PdnKddHgAAAACAfwPGelxVn68YEV4IAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAA4J1suhLrnoILAAAAAAAAHGYC1K8FgqAeACQAAAAASUVORK5CYII="
)
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, result TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, count INTEGER NOT NULL) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS rate (id INTEGER PRIMARY KEY CHECK (id = 1), sent REAL NOT NULL)",
)


class ProviderError(RuntimeError):
    """Safe diagnostic code without request or response data."""


class TemporaryFailure(ProviderError):
    def __init__(self, retry_after: str | None = None):
        super().__init__("temporarily_unavailable")
        self.delay = 0.0
        if retry_after:
            try:
                if retry_after.isdecimal():
                    self.delay = float(retry_after)
                else:
                    then = parsedate_to_datetime(retry_after)
                    self.delay = max(0.0, then.timestamp() - time.time())
            except (ValueError, OverflowError, TypeError):
                pass


class Quality(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    sharpness: Annotated[float, Field(ge=0, le=1)]
    exposure: Annotated[float, Field(ge=0, le=1)]
    clipped_highlights: Annotated[float, Field(ge=0, le=1)]
    clipped_shadows: Annotated[float, Field(ge=0, le=1)]
    reason: (
        Literal["blur", "clipped_highlights", "clipped_shadows", "underexposed", "overexposed"]
        | None
    )


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    tag: str
    score: Annotated[float, Field(ge=0, le=1)]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    tags: Annotated[list[Tag], Field(max_length=12)] | None = None
    flags: Quality | None = None


def _prompt(kinds: tuple[DecisionKind, ...], ml: MLSettings) -> str:
    parts = ["Analyze this photo. Return exactly one JSON object with only the requested keys."]
    if "tags" in kinds:
        vocabulary = json.dumps(ml.vocabulary, ensure_ascii=False, sort_keys=True)
        parts.append(
            f'`tags`: array of up to {ml.tag_top_k} objects {{"tag":"id","score":0.9}}, '
            f"score between 0 and 1. Use distinct IDs only from {vocabulary}. "
            "Include only relevant visible subjects. An empty array is valid."
        )
    if "flags" in kinds:
        parts.append(
            "`flags`: object with numbers sharpness, exposure, clipped_highlights, "
            'clipped_shadows (all 0 to 1), and reason (null or one of "blur", '
            '"clipped_highlights", "clipped_shadows", "underexposed", "overexposed"). '
            "Sharpness is detail clarity, exposure is overall brightness, and clipping values "
            "are estimated fractions. Give a reason only for a severe technical defect. "
            "Allow intentional shallow focus, motion, and high- or low-key photography."
        )
    return "\n".join(parts)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


def _validate(raw: str, kinds: tuple[DecisionKind, ...], ml: MLSettings) -> Decision:
    try:
        result = Decision.model_validate(json.loads(raw, object_pairs_hook=_unique_keys))
        if result.model_fields_set != set(kinds):
            raise ValueError
        if "flags" in kinds and result.flags is None:
            raise ValueError
        if "tags" in kinds:
            tags = result.tags
            if tags is None or len(tags) > ml.tag_top_k:
                raise ValueError
            names = {item.tag for item in tags}
            if len(names) != len(tags) or not names <= ml.vocabulary.keys():
                raise ValueError
    except (ValueError, RecursionError):
        raise ProviderError("invalid_response") from None
    return result


def endpoint(config: OpenAISettings) -> SplitResult:
    try:
        url = config.base_url.get_secret_value().strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not url.isascii()
            or any(char.isspace() or ord(char) < 32 for char in url)
            or not config.model.strip()
        ):
            raise ValueError
        _ = parsed.port
        path = parsed.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            if not re.search(r"/v[0-9]+$", path):
                path += "/v1"
            path += "/chat/completions"
        return parsed._replace(path=path)
    except ValueError:
        raise ProviderError("invalid_configuration") from None


def _connection(target: SplitResult, timeout: float) -> http.client.HTTPConnection:
    host = target.hostname
    assert host is not None
    proxy_url = os.environ.get("HTTPS_PROXY", "")
    if not proxy_url or proxy_bypass_environment(
        target.netloc, {"no": os.environ.get("NO_PROXY", "")}
    ):
        transport = (
            http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        )
        return transport(host, target.port, timeout=timeout)

    try:
        proxy = urlsplit(proxy_url)
        if (
            target.scheme != "https"
            or proxy.scheme != "http"
            or not proxy.hostname
            or proxy.username is not None
            or proxy.password is not None
            or proxy.path not in {"", "/"}
            or proxy.query
            or proxy.fragment
            or proxy.port == 0
            or any(not 33 <= ord(char) <= 126 for char in proxy_url)
        ):
            raise ValueError
        connection = http.client.HTTPSConnection(proxy.hostname, proxy.port or 80, timeout=timeout)
        # CONNECT carries only the authority. HTTPSConnection verifies TLS against the
        # destination inside the tunnel before sending the POST and its credentials.
        connection.set_tunnel(host, target.port if target.port is not None else 443)
        return connection
    except ValueError:
        raise ProviderError("invalid_configuration") from None


class OpenAIProvider:
    def __init__(self, state: Path, config: OpenAISettings, stop: threading.Event | None = None):
        self.config = config
        self.root = state / "vlm"
        self.path = self.root / "requests.db"
        self.stop = stop or threading.Event()
        self._run_count = 0
        self._run_lock = threading.Lock()
        self._initialized = False

    def _initialize(self) -> None:
        with self._run_lock:
            if self._initialized:
                return
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with connect(self.path) as db, write(db):
                for sql in SCHEMA:
                    db.execute(sql)
            self._initialized = True

    def _wait(self, seconds: float) -> None:
        if self.stop.wait(seconds):
            raise ProviderError("interrupted")

    @contextmanager
    def _lock(self, names: tuple[str, ...]) -> Iterator[None]:
        while not self.stop.is_set():
            for name in names:
                with (self.root / name).open("a") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    yield
                    return
            self._wait(0.05)
        raise ProviderError("interrupted")

    def _reserve(self, still_requested: Callable[[], bool] | None) -> None:
        # A transaction reserves an actual HTTP attempt across workers and API probes.
        while not self.stop.is_set() and (still_requested is None or still_requested()):
            with self._run_lock, connect(self.path) as db, write(db):
                now = time.time()
                day = datetime.fromtimestamp(now, UTC).date().isoformat()
                daily = db.execute("SELECT count FROM daily WHERE day = ?", (day,)).fetchone()
                if self._run_count >= self.config.max_requests_per_run or (
                    (daily[0] if daily else 0) >= self.config.max_requests_per_day
                ):
                    raise ProviderError("budget_exhausted")
                last = db.execute("SELECT sent FROM rate WHERE id = 1").fetchone()
                spacing = 60 / self.config.requests_per_minute
                delay = spacing - max(0, now - last[0]) if last else 0
                if delay <= 0:
                    db.execute(
                        "INSERT INTO daily VALUES (?, 1) "
                        "ON CONFLICT(day) DO UPDATE SET count=count+1",
                        (day,),
                    )
                    db.execute("INSERT OR REPLACE INTO rate VALUES (1, ?)", (now,))
                    self._run_count += 1
                    return
            self._wait(delay)
        raise ProviderError("interrupted")

    def _post(self, target: SplitResult, body: dict) -> str:
        key = os.environ.get("PHOTOCULL_VLM_API_KEY", "")
        if any(not 33 <= ord(char) <= 126 for char in key):
            raise ProviderError("invalid_configuration")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        connection = _connection(target, self.config.timeout_seconds)
        # A single HTTP request, with no redirect handling or implicit proxy discovery.
        try:
            connection.request("POST", target.path, json.dumps(body).encode(), headers)
            with connection.getresponse() as response:
                if response.status == 429 or 500 <= response.status <= 599:
                    raise TemporaryFailure(response.getheader("Retry-After"))
                if response.status != 200:
                    raise ProviderError("request_rejected")
                chunks = bytearray()
                deadline = time.monotonic() + self.config.timeout_seconds
                while len(chunks) <= MAX_RESPONSE:
                    if time.monotonic() >= deadline:
                        raise TemporaryFailure()
                    if not (part := response.read1(min(8192, MAX_RESPONSE + 1 - len(chunks)))):
                        break
                    chunks.extend(part)
                if len(chunks) > MAX_RESPONSE:
                    raise ProviderError("invalid_response")
        except (OSError, http.client.HTTPException):
            raise TemporaryFailure() from None
        finally:
            connection.close()
        try:
            message = json.loads(chunks)["choices"][0]["message"]
            if message.get("refusal") or not isinstance(message["content"], str):
                raise ValueError
            # Some compatible servers wrap JSON in Markdown despite the prompt.
            content = message["content"].strip()
            fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
            return fence[1] if fence else content
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
            raise ProviderError("invalid_response") from None

    def _request(
        self,
        target: SplitResult,
        prompt: str,
        image_url: str,
        still_requested: Callable[[], bool] | None = None,
    ) -> str:
        body = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }
        slots = tuple(f"request-{slot}.lock" for slot in range(self.config.concurrency))
        for attempt in range(REQUEST_ATTEMPTS):
            try:
                with self._lock(slots):
                    self._reserve(still_requested)
                    return self._post(target, body)
            except TemporaryFailure as error:
                if attempt == REQUEST_ATTEMPTS - 1 or error.delay > 10:
                    raise ProviderError("temporarily_unavailable") from None
                self._wait(max(error.delay, 0.25 * 2**attempt))
        raise AssertionError("Invalid request attempt count")

    def analyze(
        self,
        digest: str,
        preview: Path,
        kinds: tuple[DecisionKind, ...],
        ml: MLSettings,
        still_requested: Callable[[], bool] | None = None,
    ) -> Decision:
        from photocull.worker.vlm_image import image_data_url

        target = endpoint(self.config)
        # Serialize the same content across processes even when one request asks
        # for both decisions and another only needs one of them.
        identity = [target.geturl(), self.config.model, IMAGE_RECIPE, digest]
        bucket = sha256(digest.encode()).hexdigest()[:2]
        keys = {
            kind: sha256(json.dumps([*identity, _prompt((kind,), ml)]).encode()).hexdigest()
            for kind in kinds
        }
        values = {}
        try:
            self._initialize()
            with self._lock((f"cache-{bucket}.lock",)), connect(self.path) as db:
                for kind, key in keys.items():
                    row = db.execute("SELECT result FROM cache WHERE key = ?", (key,)).fetchone()
                    if row:
                        values[kind] = getattr(_validate(row[0], (kind,), ml), kind)
                missing = tuple(kind for kind in kinds if kind not in values)
                if missing:
                    try:
                        raw = self._request(
                            target,
                            _prompt(missing, ml),
                            image_data_url(preview),
                            still_requested,
                        )
                        result = _validate(raw, missing, ml)
                    except ProviderError:
                        if not values:
                            raise
                    else:
                        with write(db):
                            for kind in missing:
                                values[kind] = getattr(result, kind)
                                partial = Decision(**{kind: values[kind]})
                                db.execute(
                                    "INSERT INTO cache VALUES (?, ?)",
                                    (keys[kind], partial.model_dump_json(exclude_unset=True)),
                                )
                return Decision(**values)
        except (OSError, sqlite3.Error):
            raise ProviderError("storage_unavailable") from None

    def test_connection(self) -> dict[str, str | bool]:
        masked = "***"
        try:
            target = endpoint(self.config)
            masked = f"{target.scheme}://***"
            self._initialize()
            response = self._request(
                target,
                'Return only the JSON object {"ok":true}.',
                f"data:image/png;base64,{PROBE_PNG}",
            )
            value = json.loads(response, object_pairs_hook=_unique_keys)
            success = isinstance(value, dict) and set(value) == {"ok"} and value["ok"] is True
        except (OSError, sqlite3.Error, ProviderError, ValueError, RecursionError):
            success = False
        return {"ok": success, "endpoint": masked}
