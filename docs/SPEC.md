# Photocull — specification (for Codex)

Public rewrite of [Selects](https://github.com/bihanikeshav/selects) v0.1.21 as a cleaner, process-split local photo culler.

- Repo (public, being created in parallel): https://github.com/Kawadian/photocull
- CT 205 push URL (after the write deploy key is registered): `git@github-photocull:Kawadian/photocull.git`
- This is a **new codebase**, not a fork. Use Selects as a feature/reference source only; do not copy its tree wholesale.
- Do not publish to the internet. Do not put secrets, tokens, or webhook URLs in the repo.
- Never delete, move, or overwrite the user's original photos.

## Why rewrite

Selects is a capable local culler (FastAPI + React, per-library SQLite under `<folder>/.selects/`, staged pipeline). On the home install (CT 222, 24 GB RAM) it fell over because **the web `serve` process and the indexer each load the same ML models**. Observed roughly: API/UI ~14.7 GB RSS + swap, indexer ~9 GB RSS + swap, then container OOM / near-standstill I/O wait.

Other production issues we already patched or hit:

1. **Sony α7C II DNG + pyexiv2 / Exiv2 “Sony2” maker notes.** Default Exiv2 log level can turn an invalid/unsupported `Sony2` directory into a hard failure, so date/camera/GPS never load. Fix: mute Exiv2 (`pyexiv2.set_log_level(4)` or equivalent) and still read standard tags (`Exif.Photo.DateTimeOriginal`, make/model, GPS). Do not require maker-note parse success.
2. **`.hif` ignored.** Selects classifies only `.heic` / `.heif` as HEIC. Sony HEIF is often `.HIF`. Treat `.hif` as HEIF.
3. **`edit/` folders must be skipped** at any depth (Lightroom / in-folder edits). Upstream Selects only skips `.selects`, `.git`, `__pycache__`, `node_modules`.
4. **SQLite “database is locked”** under concurrent API + pipeline writes. Use WAL, a generous `busy_timeout`, and a **single writer** (the worker) for analysis; API writes go through a short, serialized path (ratings/jobs only).
5. **Heavy / unbounded pagination** (offset/limit). Prefer cursor pagination from the start.
6. **Filesystem browsing API** (`/api/fs/` style) is a foot-gun on a LAN service. Do not expose a generic file-browser endpoint.

## Goal

Point Photocull at a photo library (Sony α7C II DNG / HIF / ARW, plus JPEGs and videos). It indexes locally, scores and groups, lets the user cull with keyboard and ratings, and writes Lightroom Classic–compatible XMP sidecars. The **UI must stay usable while indexing**. All inference stays on-device.

## Recommended stack (and why)

| Layer | Choice | Why |
|---|---|---|
| API | Python 3.12 + FastAPI + uvicorn | Photo/EXIF/RAW ecosystem is Python (`pyexiv2`, `rawpy`/`libraw`, `pillow-heif`, `onnxruntime`). FastAPI stays small if it **never imports ML**. |
| Worker | Same repo, **separate process / entrypoint** | Only this process may import ONNX / insightface / whisper / etc. Lazy-load models, process in batches, honor memory caps. |
| Queue + jobs | SQLite job tables in the library DB (no Redis required) | Fits a single home CT. Optional Redis later, not for MVP. |
| DB | SQLite 3, WAL, `busy_timeout=5000`+, `synchronous=NORMAL` | One portable file per library. Schema migrations (Alembic or equivalent) from day one. |
| Frontend | React + Vite + TypeScript | Keyboard-first culling UI; API-first, same-origin static build served by the API in production. |
| Reverse proxy | nginx (or Caddy only on LAN, never public) | IP allowlist + optional token injection so the browser need not show `?token=`. |
| Deploy | `docker compose` **and** systemd units | Compose for contributors; systemd for the home CT (Debian, bind mounts). |
| Package | `uv` / `pip` extras: `photocull` (API+CLI) vs `photocull[ml]` (worker) | CI and the API image stay light. |

**Rejected for MVP:** running models inside the API process (Selects’ failure mode); a required Redis/Postgres; a desktop-only shell; GPU as a hard dependency.

## Architecture (non-negotiable)

```
[browser / CLI]
       |  LAN / Tailscale only
[nginx]  allow 192.168.0.0/16, 100.64.0.0/10, loopback
       |  optional token inject; no generic /api/fs/
[photocull-api]   target RSS < 500 MB; NO ML model loads, ever
       |  enqueue jobs, read DB, serve thumbs/previews, write ratings
[SQLite WAL]
       ^
[photocull-worker]  lazy models, batches, memory limits, resumable stages
       |
[/mnt/PhotoLibrary or library roots]  originals: read (XMP sidecars may be written)
```

Rules:

- The API package/image must not depend on `onnxruntime`, `insightface`, `faster-whisper`, `torch`, or similar. Add a CI check (import graph or a test that importing `photocull.api` does not import those modules).
- Worker loads a model only when a job for that stage is actually running; unload or let a memory cap force a restart rather than leaking all models at once.
- Configurable worker limits: max RSS (e.g. env `PHOTOCULL_WORKER_MEMORY_MB`), batch size, parallelism (default 1 on CPU), which stages are enabled, device `cpu` (default) or `cuda` (optional).
- Jobs are resumable: each file × stage is a row (`pending` / `running` / `done` / `error` / `skipped`) with a content hash. Restart must continue, not start over. Stages are independently re-runnable.
- Thumbs and previews live in a **writable state directory**, not inside the original folder unless the operator explicitly chooses an in-tree state dir.
- CPU-only first. GPU is an optional ONNX ExecutionProvider / CTranslate2 path, documented, not assumed.

## Libraries and filesystem

Support both layouts:

1. **Direct scan (preferred for the home NAS):** read-only (or mostly read-only) root such as `/mnt/PhotoLibrary`, with state at a writable path (e.g. `/var/lib/photocull/libraries/<name>/`: `index.db`, `thumbs/`, `previews/`, models cache elsewhere). Follow directory structure as-is.
2. **Symlink library:** a writable library root whose month/day dirs are symlinks into PhotoLibrary (the CT 222 pattern under `/srv/selects/libraries/<name>/`). Index through the links; do not copy originals.

Walk rules:

- Include: JPEG/PNG/WebP/TIFF (`jpg`, `jpeg`, `png`, `webp`, `tif`, `tiff`), HEIF (`heic`, `heif`, **`hif`**), RAW (**`dng`**, **`arw`**, and other common RAW: `cr2`, `cr3`, `nef`, `raf`, `orf`, `rw2`, `pef`), video (`mp4`, `mov`, `m4v`, `mkv`).
- Skip directories named `edit`, `.git`, `__pycache__`, `node_modules`, and Photocull’s own state dir (e.g. `.photocull`), case-insensitive, at any depth.
- Follow symlinks that stay inside configured roots; do not escape the library root (symlink jail).
- Hash files (SHA-256) and record mtime/size so unchanged files are skipped on re-index.
- Decode previews from embedded JPEG in DNG/ARW when possible; otherwise a bounded-size demosaic. HIF/HEIF via `pillow-heif` (or equivalent). Videos: poster frame + duration via ffmpeg/ffprobe.

EXIF:

- Always set Exiv2/pyexiv2 log level to mute (4) before opening files.
- Prefer `DateTimeOriginal` > `DateTimeDigitized` > `DateTime`; keep make/model and GPS when present.
- A maker-note warning is not a failed read. Record a structured error only when standard tags truly cannot be read.
- Unit-test the log-level + “partial EXIF still returned” behavior (fixture can be a tiny synthetic TIFF/DNG if a real Sony file is not in the repo; do **not** commit the user’s photos).

## Scoring, tags, ratings (phased)

**MVP (phase 1)** — must ship first:

- Walk + EXIF + previews/thumbs as above.
- Light web UI: folder/day browse, filmstrip, 100% zoom, keyboard next/prev, rate 1–5 / reject / unrate, undo.
- Persist ratings in SQLite.
- **XMP sidecar write-back** compatible with Lightroom Classic:
  - Always write a sidecar (do not modify original bytes in MVP; safer for DNG/HIF/ARW and mixed mount permissions). Path: `STEM.xmp` next to `STEM.ext` (LR Classic convention), UTF-8, preserve any existing sidecar tags when updating.
  - Set `xmp:Rating` (0–5). Reject may use rating 1 or a documented flag; pick one and test.
  - Dry-run CLI + UI confirmation. Skip or error clearly if the sidecar directory is not writable (e.g. a month mount without write ACL) without touching the original.
  - Idempotent; Unicode paths must work.
- Job progress API (WebSocket or SSE) so the UI can show stage counts.
- Health endpoints: API RSS, worker heartbeat, job counts.

**Phase 2:**

- The worker adds a resumable `score` stage for still images with previews. It stores normalized Laplacian sharpness, mean luminance, clipped highlights and clipped shadows. Thresholds set reversible auto-reject flags with reasons. Ratings and explicit sidecar export are independent.
- Burst grouping uses EXIF capture times and 64-bit dHash, with configurable time and Hamming-distance thresholds. The catalog stores membership, a score-based recommendation and the user's best selection across regrouping.
- The API supports cursor-paginated score and burst filters, flag dismissal, best selection and atomic burst reject/undo. The review UI shows scores, offers side-by-side burst comparison and retains the Phase 1 shortcuts. See the [configuration and sizing guide](../README.md#scores-and-bursts).

**Phase 3:**

- Image embeddings (SigLIP 2 or a smaller documented default if memory is tight; record the model id in the DB so a change forces re-embed).
- Similar-photo grouping; natural-language / tag search.
- Object / open-vocab tags (start with embedding zero-shot; RAM++ or equivalent is optional and must stay in the worker).

**Phase 4 (after the above is solid):**

- Aesthetic quality model, face grouping, day/place stories, video highlight / dead-clip detection, optional local transcripts.
- Taste-learning from keep/reject history.
- Watch folder, keeper zip/copy export, trip recap HTML.

Do not block MVP on phase 3–4 models.

## Access and deployment

- Bind the API to `127.0.0.1` (configurable). nginx listens on `:80` (and maybe `:8000`) and allows only `192.168.0.0/16`, `100.64.0.0/10`, and loopback.
- Auth: IP allowlist alone **or** a shared token automatically injected by the proxy (cookie or header) so LAN/Tailscale users do not paste `?token=` into the URL. Document both. Never commit the token.
- Not published on the public wildcard / Caddy edge. DNS (e.g. `photocull.dian.0am.jp` or reuse `selects.dian.0am.jp` later) is an operator concern, not this repo’s default.
- `docker-compose.yml`: `api`, `worker`, `proxy`; bind-mount library + state + model cache. Resource limits on the worker.
- `deploy/systemd/`: `photocull-api.service`, `photocull-worker.service`, example nginx site. Worker should be restartable independently without dropping the UI.
- Config via env / a single YAML or `.env` (library roots, state dir, bind host, allowlist, token file path, worker memory, device).

Home deploy will be handled by a separate Cursor agent on the files-worker (CT 219). Keep compose + systemd examples accurate and boring.

## Tests and CI

- `pytest` for walk rules (`.hif`, skip `edit/`, symlink jail), EXIF log-level behavior, XMP sidecar write/update (temp dirs, Unicode names), job resume (interrupt mid-stage, restart, no double work), API does not import ML, SQLite writer concurrency (API rating + worker job update must not raise `database is locked`).
- `ruff check` (and format if you add it).
- Frontend: `lint` + `build` (strict TS).
- Optional Playwright smoke: API + UI against a tiny fixture library, **no ML weights**.
- GitHub Actions: lint + unit tests + frontend build on `ubuntu-latest` for every PR/push to `main`. Do not download multi-GB models in CI. Add ffmpeg only if video tests need it.

## Repo layout (suggested)

```
photocull/            # Python package
  api/                # FastAPI app, routes, no ML imports
  worker/             # job runner, stages, lazy model loaders
  db/                 # models + migrations
  media/              # walk, exif, decode, xmp
  cli.py
frontend/
tests/
deploy/docker/        # Dockerfile.api, Dockerfile.worker, nginx
deploy/systemd/
docker-compose.yml
.github/workflows/ci.yml
README.md
```

## Implementation order for Codex

1. Repo skeleton, packaging, lint, empty CI, README.
2. DB schema + migrations + SQLite helpers (WAL, busy timeout, one-writer notes).
3. Walker / EXIF / preview decode + tests (including `.hif`, `edit/`, Sony2/log-level policy).
4. Job queue + worker loop (index + preview stages only) + resume tests.
5. API: libraries, assets, thumbs, ratings, progress, health.
6. XMP sidecar write-back + tests.
7. Minimal React UI (browse, rate, progress).
8. nginx / compose / systemd examples.
9. Stop. Do not start SigLIP/RAM++/faces until phase 1 is green.

Keep commits small and documented. Public repo: no user photos, no tokens, no internal host passwords. Referring to `/mnt/PhotoLibrary` in docs/examples is fine.
