# Photocull

Photocull is a self-hosted photo and video culler for a LAN or Tailscale network. Browse by folder, capture day or burst, review previews with a filmstrip and keyboard, and export ratings as XMP sidecars. Optional CPU ML adds Japanese and English photo search, similar-photo groups and automatic tags. The FastAPI service and SQLite indexing worker run independently, so you can browse and rate while the worker scans or restarts.

Open **Photo browser** for a cross-library capture timeline with a justified photo grid, month navigation and shareable photo previews. The grid supports touch, keyboard navigation and three densities. Choose English or Japanese in the browser header. [Photo browser guide](docs/BROWSER.md)

The viewer supports fullscreen, touch zoom and pan, slideshow, EXIF information, face boxes and video highlights. The worker prepares responsive AVIF, WebP and JPEG images and two fast-start video proxies. Optional original downloads use the existing authentication. [Viewer guide](docs/BROWSER.md#viewer) · [Cache configuration](docs/BROWSER.md#cache-configuration-and-migration)

## Run locally

Use Linux, Python 3.12, [uv](https://docs.astral.sh/uv/) and Node.js 24. Install `ffmpeg` and `ffprobe` for video posters and durations (`apt install ffmpeg` on Debian or Ubuntu).

```sh
uv sync --locked --extra worker
npm --prefix frontend ci
npm --prefix frontend run build
cp .env.example .env
```

Set `PHOTOCULL_LIBRARY_ROOT` in `.env` to your photo directory. In two terminals from the repo root, run:

```sh
.venv/bin/photocull-api
```

```sh
.venv/bin/photocull-worker
```

Open `http://127.0.0.1:8000` and choose **Scan library**. You can also queue a scan with `.venv/bin/photocull scan photos` and check it with `.venv/bin/photocull status photos`. The API serves `frontend/dist` at the same origin. For frontend development, `npm --prefix frontend run dev` starts Vite on loopback and proxies `/api` to port 8000.

The base package is the API and CLI (`uv sync --locked --no-dev` or `pip install .`). The worker extra installs media decoders, Pillow and NumPy. Scoring runs on the CPU without model weights. The optional `ml` extra includes the worker extra plus CPU inference and neighbor search.

## Configure libraries

Environment variables override `.env`, which overrides YAML. Photocull loads `./photocull.yaml` by default, or a file set by `PHOTOCULL_CONFIG`. Relative paths in YAML are resolved against that file's directory. Relative environment paths are resolved from the process working directory. Use the same config and state directory for both processes.

- **Direct scan** uses a root such as `/mnt/PhotoLibrary` with a separate writable state directory.
- **Symlink library** uses a view such as `/srv/photocull-view` whose directories link into explicitly configured `allowed_roots`. The walker verifies resolved paths. Symlink targets outside those roots and directory cycles are skipped.
- The walker skips `edit`, `.git`, `__pycache__`, `node_modules`, `.photocull`, and the configured state directory at any depth. Archive trash and private staging folders are also excluded. The named exclusions are case-insensitive.

The [YAML example](deploy/photocull.example.yaml) shows both layouts, and [.env.example](.env.example) shows the environment settings. Use `PHOTOCULL_LIBRARY_ROOT` and `PHOTOCULL_LIBRARY_NAME` to configure a single library by environment, with extra jail roots in `PHOTOCULL_ALLOWED_ROOTS` separated by `:` on Linux. YAML supports multiple libraries.

| Environment variable | Default or purpose |
| --- | --- |
| `PHOTOCULL_STATE_DIR` | `.photocull`; contains per-library DBs and generated media |
| `PHOTOCULL_BIND_HOST`, `PHOTOCULL_BIND_PORT` | `127.0.0.1`, `8000` |
| `PHOTOCULL_STATIC_DIR` | `frontend/dist` |
| `PHOTOCULL_TOKEN_FILE` | Optional path to a shared printable ASCII token |
| `PHOTOCULL_WORKER_MEMORY_MB` | `1536` MiB worker budget, `6144` MiB recommended with ML on a 16 GiB host |
| `PHOTOCULL_WORKER_BATCH_SIZE`, `PHOTOCULL_WORKER_PARALLELISM` | `8` media jobs per library turn and `1` concurrent EXIF, preview or scoring job |
| `PHOTOCULL_WORKER_SCAN_PARALLELISM`, `PHOTOCULL_WORKER_SCAN_BATCH_SIZE` | `1` concurrent library scan and up to `64` files or errors per scan transaction |
| `PHOTOCULL_WORKER_HASH_READ_MIB`, `PHOTOCULL_WORKER_SCAN_OVERLAP` | `4` MiB reusable read buffer per scan and `false` to run scans ahead of media jobs |
| `PHOTOCULL_WORKER_STAGES`, `PHOTOCULL_WORKER_DEVICE` | `exif,preview,score` and `cpu` |

Photocull scans DNG, ARW and common RAW formats, HEIF/HEIC/HIF, AVIF, JPEG, PNG, WebP, TIFF, and MP4/MOV/M4V/MKV. RAW previews prefer an embedded JPEG and fall back to a half-size demosaic. Videos display a poster and duration. Capture day uses the best available EXIF timestamp and falls back to the file modification day in UTC. A **100%** view displays decoded pixels at their natural size. If an image exceeds the fit preview limit, **100%** opens a native-size center crop.

For unreadable-file reports and tuning initial imports from HDDs, see [Library indexing](docs/INGEST.md).

## Archive and storage

The **Archive** view plans lossless RAW/DNG conversion, HEVC/AV1 video reduction, JPEG/HEIC/AVIF conversion, selected-file copy, and batch rename. It also shows duplicate groups and storage by year, folder, format and camera. Jobs default to separate exports. One confirmation covers the full previewed set, with paginated details and worker-batched execution. Write roots must pass the worker filesystem self-test before replacement, rename or deletion. Saved conversion rules can notify you of ready previews. The CLI uses the same preview and confirmation contract. [Archive guide](docs/ARCHIVE.md)

## Review and write XMP

| Key | Action |
| --- | --- |
| `←` / `→` | Previous / next asset, including across pages |
| `1`–`5` | Set star rating |
| `X` / `0` | Reject / clear rating and reject flag |
| `U` or `Ctrl/⌘+Z` | Undo the most recent rating change in this library view |
| `Z` | Toggle fit and 100% |
| `[` / `]` | Previous / next burst, including across pages |
| `B` | Set the current photo as best in its burst |
| `R` | Open the burst reject confirmation |
| `Shift+U` | Undo the most recent burst rejection in this library view |
| `?` | Open keyboard shortcuts |

Ratings are saved in SQLite. **Write XMP…** shows the destination and rating before making any change. You can also preview or write selected asset IDs from the CLI:

```sh
.venv/bin/photocull xmp photos 42 43 --dry-run
.venv/bin/photocull xmp photos 42 43 --write
```

Photocull writes `STEM.xmp` beside the resolved original and preserves other XMP tags. The original media is opened read-only. A reject stays a flag in Photocull and exports as `xmp:Rating=1`. Clearing a rating exports `0`. Export requires directory write permission and refuses ambiguous destinations such as `IMG_0001.ARW` and `IMG_0001.JPG` in the same folder.

For a proprietary RAW file such as ARW, select it in Lightroom Classic and use **Metadata → Read Metadata From File**. Lightroom stores metadata inside formats such as DNG, JPEG and TIFF, so its sidecar import behavior depends on the format. Photocull uses a sidecar for every format. [Adobe metadata basics](https://helpx.adobe.com/lightroom-classic/desktop/organize-photos-in-lightroom-classic/metadata-basics-actions.html) · [Adobe advanced metadata actions](https://helpx.adobe.com/lightroom-classic/desktop/organize-photos-in-lightroom-classic/advanced-metadata-actions.html)

## Scores and bursts

- **Scores** shows normalized sharpness, average luminance and the share of pixels near black or white. Use **Score filters** to set a sharpness minimum, choose an exposure range or find flagged assets.
- **Clear auto-reject** dismisses one flag. **Clear all auto-rejects…** dismisses current flags across the library. Dismissals persist when a photo is rescored or thresholds change. Ratings and XMP export use the manual review controls.
- **Bursts** lists groups of similar photos taken close together. Filters find bursts with at least one matching member. Open a burst to see all its members, use **Compare with best** for a side-by-side view and **Set as best** to choose a different keeper. **Reject rest…** confirms and flags the others in one action. **Undo burst reject** restores only the flags changed by that action.

The worker scores still-image previews using grayscale luminance and Laplacian variance on a resampled image of up to 512 pixels per side. This includes RAW previews. The inspector shows queued, running, skipped and failed scoring states. Videos have poster previews and duration. A burst member must be within the capture-time window and pass the similarity threshold against its burst's first photo. When both photos have current embeddings, the worker compares cosine similarity. Otherwise it compares 64-bit dHash by Hamming distance. Burst membership requires an EXIF capture time. Capture subseconds are used when available in the indexed metadata. The best recommendation balances sharpness (60%), distance from mid-gray exposure (25%) and unclipped pixels (15%). A manually selected best follows its photo when groups are recalculated.

| Worker environment variable | YAML key | Default |
| --- | --- | --- |
| `PHOTOCULL_SCORING_MIN_SHARPNESS` | `scoring.min_sharpness` | `0.02` |
| `PHOTOCULL_SCORING_MIN_EXPOSURE` | `scoring.min_exposure` | `0.08` |
| `PHOTOCULL_SCORING_MAX_EXPOSURE` | `scoring.max_exposure` | `0.92` |
| `PHOTOCULL_SCORING_MAX_CLIPPED_HIGHLIGHTS` | `scoring.max_clipped_highlights` | `0.25` |
| `PHOTOCULL_SCORING_MAX_CLIPPED_SHADOWS` | `scoring.max_clipped_shadows` | `0.40` |
| `PHOTOCULL_BURST_WINDOW_SECONDS` | `burst.window_seconds` | `2` |
| `PHOTOCULL_BURST_MAX_HAMMING_DISTANCE` | `burst.max_hamming_distance` | `8` |
| `PHOTOCULL_BURST_MIN_COSINE` | `burst.min_cosine` | `0.88` |

Scoring thresholds are inclusive bounds from 0 to 1. Set minimums to 0 and maximums to 1 to disable automatic flag suggestions. Restart the worker after changing settings. With Compose, edit the mounted YAML. The worker re-evaluates stored scores against new thresholds and refreshes groups after pending indexing work completes.

Upgrading from Phase 1 schedules one score job per active still image through `0004_scores_and_bursts.sql`. Ensure `score` is in the worker's stage list if you previously set it explicitly. Scoring reads cached previews and adds one compact score row per photo plus membership rows for grouped photos. Allow CPU time and preview reads for the initial backfill. A 512 × 512 float analysis plane occupies 1 MiB, with additional temporary arrays and a decoded preview per worker task. The default worker parallelism of 1 and 1536 MiB restart threshold also cover media decoding. The API uses the existing 500 MiB deployment budget and queries SQLite for results.

The asset and burst lists use integer cursors and accept `min_sharpness`, `min_exposure`, `max_exposure` and `auto_reject`. Pass `burst_id` to the asset list to page through a burst. The OpenAPI schema lists the burst selection, reject, undo and flag-clearing endpoints.

### Catalog performance

`0005_incremental_bursts.sql` adds a durable queue of affected capture times. The worker walks the capture-time index and compares hashes only against anchors in the active window. Dense windows use a hash-band index for distance thresholds up to 12, with exact Hamming comparisons on the resulting candidates. Edits, additions and removals rebuild the affected ranges. A capture gap larger than the window limits how far a change can propagate. Initial indexing, grouping setting changes and batches with at least 2,048 changed capture times use a full pass.

The benchmark inserts synthetic SQLite rows with five photos per burst, then measures requests through FastAPI with 100 results per page and 40 samples per case. The figures below use a Ryzen 5 5500U and SQLite 3.54.1 with the default grouping settings. They cover catalog and grouping work on one process. Preview generation, network latency and simultaneous clients add their own costs.

| Assets | Highest browse p95 across nine cases | Full grouping | One-photo incremental update | SQLite catalog |
| ---: | ---: | ---: | ---: | ---: |
| 50,000 | 16.5 ms | 0.80 s | 1.07 ms | 40.7 MiB |
| 100,000 | 29.0 ms | 2.59 s | 2.36 ms | 82.3 MiB |

SQLite uses the active asset ID, folder/day and burst membership indexes for keyset pagination. Selective filters can still visit the remaining IDs to find enough matches. Keep the database, WAL and SQLite temporary files on local writable storage. Generated previews and thumbnails are stored separately and require additional space.

```sh
uv run --locked python scripts/benchmark_catalog.py --assets 50000
uv run --locked python scripts/benchmark_catalog.py --assets 100000
```

The script prints p50, p95 and `EXPLAIN QUERY PLAN` for each case, including filters with zero matches. It exits with an error if any p95 reaches 200 ms.

## SQLite and service health

Keep state on a local writable Linux filesystem so both processes can share SQLite WAL, its `-wal` and `-shm` files, and the worker lock. Each library has its own index DB and generated thumbnails, previews and detail crops. Schema changes migrate automatically at startup. The worker holds an exclusive lock per library for indexing. It commits in batches and does filesystem I/O outside DB transactions. API writes use short `BEGIN IMMEDIATE` transactions. Connections use WAL, a 10-second busy timeout and `synchronous=NORMAL`.

A scan computes SHA-256 for new and changed media and reuses hashes for files with unchanged source identity, size and timestamps. Jobs track each asset and stage. The worker requeues in-flight jobs on restart and preserves completed work. After three interrupted runs of a stage, the worker marks that job as failed and processes the rest of the queue. A stage can be rerun with `POST /api/libraries/{library}/assets/{asset_id}/jobs/{stage}`. The OpenAPI schema is at `/openapi.json`.

`GET /api/health` reports API RSS, worker availability and job counts. The API stays healthy while the worker is offline. `GET /api/libraries/{library}/events` streams job and catalog updates via SSE. The worker exits for a supervisor restart if its peak RSS reaches the configured threshold after a batch. Optional ML runs with a separate address-space limit inside that budget. Compose and systemd also apply a hard memory limit to the service.

## Photo search, similarity and tags

1. Install `uv sync --locked --extra ml` in the worker environment. The Docker worker image includes this extra.
2. Set `ml.enabled: true` in the shared configuration and raise the worker memory budget. Use `worker.memory_mb: 6144` on a 16 GiB host and allow up to 8 GiB in the service or container.
3. Run `photocull fetch-model` as the worker user to preload the 393 MiB model cache. With outbound network access, the worker can also populate the cache on first use.
4. Restart the API and worker. Existing still photos with previews are queued automatically. New photos get embeddings after preview generation.

Press **/** to focus **Search photos**, **Enter** to submit and **Esc** to return to review. Search with a phrase such as `雪の山` or `snowy mountains`. Use **Find similar photos** in the inspector to search from the current photo, or browse **Similar** groups. Pick a **Photo tag** or click a tag in the inspector to filter the catalog. The ML guide covers all settings, offline deployment steps and measured resource use. [ML guide](docs/ML.md)

## Local insights and keeper exports

The optional worker adds aesthetic scores and learned recommendations,
local face grouping, day and place stories, and video highlights and dead-clip
flags. It can also watch media folders, copy keepers or create ZIP64 archives,
and generate standalone HTML trip reports. Video transcription runs locally
and is independently configurable. The setup guide covers feature settings,
model licenses, download sizes and CPU-only measurements. For folder copies
under Compose, set the destination in shared YAML and use the worker-only
[example override](deploy/compose.insights.example.yml).
[Phase 4 setup and operation](docs/INSIGHTS.md)

## Access and deployment examples

Keep the API on loopback and expose the nginx example only on your LAN or Tailscale. The [nginx site](deploy/systemd/nginx-photocull.conf) allows `192.168.0.0/16`, `100.64.0.0/10` and loopback, and denies other peers. The allowlist is configured in the nginx site. Use the real client connection to nginx, and keep WAN port forwarding disabled.

### Docker Compose

The [Compose example](docker-compose.yml) uses Linux Docker Engine host networking for nginx and the loopback API, which keeps the connecting client's IP visible to nginx. The worker shares state through the mounted directory and can be restarted independently.

1. Set `PHOTOCULL_LIBRARY_DIR` in `.env`. The host path is mounted as `/mnt/PhotoLibrary`. For a symlink layout, supply your own YAML with `PHOTOCULL_CONFIG_FILE` and a Compose override that mounts the view and all allowed target roots at the paths used by the symlinks. The worker mounts the media read-only.
2. Set `PHOTOCULL_UID` and `PHOTOCULL_GID` to a user and group that can access the media and write the state and cache directories. For sidecar export, give the API user write permission on the media directories.
3. As that user, create the default state and cache directories and start the services.

```sh
mkdir -p .photocull/state .photocull/models
docker compose config --quiet
docker compose up --build -d
docker compose restart worker
```

Open port 80 on the host from an allowed client. Port 8000 is the API upstream on loopback.

### systemd

The example [API unit](deploy/systemd/photocull-api.service) and [worker unit](deploy/systemd/photocull-worker.service) expect a `photocull` service user, checkout and built frontend under `/opt/photocull`, and a Python 3.12 environment at `/opt/photocull/.venv`. Install the worker extra and FFmpeg there. Install the [example YAML](deploy/photocull.example.yaml) as `/etc/photocull/photocull.yaml`. The units accept optional overrides from `/etc/photocull/photocull.env`.

Give the service user read access to the media. For XMP export, grant directory write access and set the API unit's `ReadWritePaths` to the canonical roots where sidecars are allowed. systemd creates `/var/lib/photocull` for state. If the library requires a separate mount, add the mount dependency in a unit drop-in.

Install both units in `/etc/systemd/system/` and enable them with `systemctl enable --now photocull-api photocull-worker` after `systemctl daemon-reload`. Install the nginx site and enable it as the only default server on port 80. Run `nginx -t` before reloading nginx. Restart the indexer with `systemctl restart photocull-worker`.

### Optional proxy token

For IP-only access, run nginx with the example as supplied. To add a shared token, generate a random value locally and store it in a file readable by the API service. Point `token_file` or `PHOTOCULL_TOKEN_FILE` at that file. Set the matching value in a private nginx snippet at `/etc/nginx/photocull.d/token.conf`:

```nginx
set $photocull_token "REPLACE_WITH_THE_GENERATED_VALUE";
```

Nginx supplies `X-Photocull-Token` upstream, so the browser uses ordinary same-origin URLs. With Compose, add a read-only bind mount for the API token file and a read-only bind mount for the proxy snippet directory at `/etc/nginx/photocull.d`. Keep both files outside version control. Restart the API and reload or restart nginx after enabling or rotating the token.

## Checks

```sh
uv sync --locked --extra ml
uv run --locked --extra ml ruff check
uv run --locked --extra ml ruff format --check
uv run --locked --extra ml pytest
npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend run build
uv run --locked --extra ml python scripts/benchmark_catalog.py --assets 2000 --requests 8 --ml
(cd frontend && npx playwright install --with-deps --only-shell chromium && npm run test:e2e)
```

GitHub Actions runs these checks for pull requests and pushes to `main`, including API tests with the base package. The ML tests and Playwright use a deterministic encoder. Playwright covers keyboard review, both search languages, tags, similarity, cancellation, worker errors and the 375 px layout. CI also builds both Docker images and checks the API image for ML packages. The opt-in real-model check is documented separately. [CI workflow](.github/workflows/ci.yml) · [ML guide](docs/ML.md#validation) · [Specification](docs/SPEC.md)

## 日本語

Photocull は、LAN または Tailscale 内で写真・動画を選別するためのセルフホスト型ツールです。フォルダー別・撮影日別に閲覧でき、インデックス作成中もブラウザーで選別できます。

1. Python 3.12、uv、Node.js 24、動画用の FFmpeg を用意します。
2. 上の **Run locally** の手順でインストールし、`.env` の `PHOTOCULL_LIBRARY_ROOT` に写真フォルダーを設定します。
3. `photocull-api` と `photocull-worker` を別々に起動し、`http://127.0.0.1:8000` で **Scan library** を実行します。

左右の矢印キーで移動、`1`〜`5` で評価、`X` で除外、`0` で評価を解除、`U` で直前の評価操作を取り消せます。**Write XMP…** で出力先と評価を確認してから、元ファイルの隣に `STEM.xmp` を書き込みます。元ファイルは変更しません。除外は Photocull 内ではフラグとして保持し、XMP には星 1 として出力します。

**Bursts** では `[` と `]` でバースト間を移動し、`B` でベストを選択、`R` で一括除外の確認、`Shift+U` で一括除外を取り消せます。`?` でショートカット一覧を開けます。自動除外フラグは [スコアとバーストの設定](#scores-and-bursts) から調整できます。

Docker Compose と systemd の設定例は [deploy](deploy/) を参照してください。nginx の許可リストで LAN・Tailscale・ループバックからの接続に制限します。

写真検索を使う場合は worker に `photocull[ml]` を導入し、共通設定の `ml.enabled` を有効にしてください。**Search photos** は「猫の写真」のような日本語や英語で検索できます。**Similar** から類似グループを、**Photo tag** からタグを選べます。 [設定・必要メモリ・オフライン導入](docs/ML.md)
