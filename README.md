# Photocull

Photocull is a self-hosted photo and video culler for a LAN or Tailscale network. Browse by folder or capture day, review previews with a filmstrip and keyboard, and export ratings as XMP sidecars. The FastAPI service and SQLite indexing worker run independently, so you can browse and rate while the worker scans or restarts.

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

The base package is the API and CLI (`uv sync --locked --no-dev` or `pip install .`). The worker installs media decoders with `pip install '.[worker]'` or the corresponding uv extra. The reserved `ml` extra is empty in Phase 1. No model weights are needed.

## Configure libraries

Environment variables override `.env`, which overrides YAML. Photocull loads `./photocull.yaml` by default, or a file set by `PHOTOCULL_CONFIG`. Relative paths in YAML are resolved against that file's directory. Relative environment paths are resolved from the process working directory. Use the same config and state directory for both processes.

- **Direct scan** uses a root such as `/mnt/PhotoLibrary` with a separate writable state directory.
- **Symlink library** uses a view such as `/srv/photocull-view` whose directories link into explicitly configured `allowed_roots`. The walker verifies resolved paths. Symlink targets outside those roots and directory cycles are skipped.
- The walker skips `edit`, `.git`, `__pycache__`, `node_modules`, `.photocull`, and the configured state directory at any depth. The named exclusions are case-insensitive.

The [YAML example](deploy/photocull.example.yaml) shows both layouts, and [.env.example](.env.example) shows the environment settings. Use `PHOTOCULL_LIBRARY_ROOT` and `PHOTOCULL_LIBRARY_NAME` to configure a single library by environment, with extra jail roots in `PHOTOCULL_ALLOWED_ROOTS` separated by `:` on Linux. YAML supports multiple libraries.

| Environment variable | Default or purpose |
| --- | --- |
| `PHOTOCULL_STATE_DIR` | `.photocull`; contains per-library DBs and generated media |
| `PHOTOCULL_BIND_HOST`, `PHOTOCULL_BIND_PORT` | `127.0.0.1`, `8000` |
| `PHOTOCULL_STATIC_DIR` | `frontend/dist` |
| `PHOTOCULL_TOKEN_FILE` | Optional path to a shared printable ASCII token |
| `PHOTOCULL_WORKER_MEMORY_MB` | `1536` MiB peak worker RSS threshold |
| `PHOTOCULL_WORKER_BATCH_SIZE`, `PHOTOCULL_WORKER_PARALLELISM` | `8` assets per scan transaction and `1` decoding task at a time |
| `PHOTOCULL_WORKER_STAGES`, `PHOTOCULL_WORKER_DEVICE` | `exif,preview`, `cpu`; Phase 1 decoding uses CPU |

Photocull scans DNG, ARW and common RAW formats, HEIF/HEIC/HIF, JPEG, PNG, WebP, TIFF, and MP4/MOV/M4V/MKV. RAW previews prefer an embedded JPEG and fall back to a half-size demosaic. Videos display a poster and duration. Capture day uses the best available EXIF timestamp and falls back to the file modification day in UTC. A **100%** view displays decoded pixels at their natural size. If an image exceeds the fit preview limit, **100%** opens a native-size center crop.

## Review and write XMP

| Key | Action |
| --- | --- |
| `←` / `→` | Previous / next asset, including across pages |
| `1`–`5` | Set star rating |
| `X` / `0` | Reject / clear rating and reject flag |
| `U` or `Ctrl/⌘+Z` | Undo the most recent rating change in this library view |
| `Z` | Toggle fit and 100% |

Ratings are saved in SQLite. **Write XMP…** shows the destination and rating before making any change. You can also preview or write selected asset IDs from the CLI:

```sh
.venv/bin/photocull xmp photos 42 43 --dry-run
.venv/bin/photocull xmp photos 42 43 --write
```

Photocull writes `STEM.xmp` beside the resolved original and preserves other XMP tags. The original media is opened read-only. A reject stays a flag in Photocull and exports as `xmp:Rating=1`. Clearing a rating exports `0`. Export requires directory write permission and refuses ambiguous destinations such as `IMG_0001.ARW` and `IMG_0001.JPG` in the same folder.

For a proprietary RAW file such as ARW, select it in Lightroom Classic and use **Metadata → Read Metadata From File**. Lightroom stores metadata inside formats such as DNG, JPEG and TIFF, so its sidecar import behavior depends on the format. Photocull uses a sidecar for every format. [Adobe metadata basics](https://helpx.adobe.com/lightroom-classic/desktop/organize-photos-in-lightroom-classic/metadata-basics-actions.html) · [Adobe advanced metadata actions](https://helpx.adobe.com/lightroom-classic/desktop/organize-photos-in-lightroom-classic/advanced-metadata-actions.html)

## SQLite and service health

Keep state on a local writable Linux filesystem so both processes can share SQLite WAL, its `-wal` and `-shm` files, and the worker lock. Each library has its own index DB and generated thumbnails, previews and detail crops. Schema changes migrate automatically at startup. The worker holds an exclusive lock per library for indexing. It commits in batches and does filesystem I/O outside DB transactions. API writes for rating changes and queue requests use short `BEGIN IMMEDIATE` transactions. Connections use WAL, a 10-second busy timeout and `synchronous=NORMAL`.

A scan computes SHA-256 for new and changed media and reuses hashes for files with unchanged source identity, size and timestamps. Jobs track each asset and stage. The worker requeues in-flight jobs on restart and preserves completed work. After three interrupted runs of a stage, the worker marks that job as failed and processes the rest of the queue. A stage can be rerun with `POST /api/libraries/{library}/assets/{asset_id}/jobs/{stage}`. The OpenAPI schema is at `/openapi.json`.

`GET /api/health` reports API RSS, worker last heartbeat and job counts. The API stays healthy while the worker is offline. `GET /api/libraries/{library}/events` streams job and catalog updates via SSE. The default worker exits for a supervisor restart if its peak RSS exceeds the configured threshold after a batch. Compose and systemd also apply a hard memory limit.

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
uv sync --locked --extra worker
uv run --locked --extra worker ruff check
uv run --locked --extra worker ruff format --check
uv run --locked --extra worker pytest
npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend run build
```

[GitHub Actions](.github/workflows/ci.yml) runs these checks for pull requests and pushes to `main`, including API tests in an environment with the base package and no worker decoders. The [specification](docs/SPEC.md) describes later phases.

## 日本語

Photocull は、LAN または Tailscale 内で写真・動画を選別するためのセルフホスト型ツールです。フォルダー別・撮影日別に閲覧でき、インデックス作成中もブラウザーで選別できます。

1. Python 3.12、uv、Node.js 24、動画用の FFmpeg を用意します。
2. 上の **Run locally** の手順でインストールし、`.env` の `PHOTOCULL_LIBRARY_ROOT` に写真フォルダーを設定します。
3. `photocull-api` と `photocull-worker` を別々に起動し、`http://127.0.0.1:8000` で **Scan library** を実行します。

左右の矢印キーで移動、`1`〜`5` で評価、`X` で除外、`0` で評価を解除、`U` で直前の評価操作を取り消せます。**Write XMP…** で出力先と評価を確認してから、元ファイルの隣に `STEM.xmp` を書き込みます。元ファイルは変更しません。除外は Photocull 内ではフラグとして保持し、XMP には星 1 として出力します。

Docker Compose と systemd の設定例は [deploy](deploy/) を参照してください。nginx の許可リストで LAN・Tailscale・ループバックからの接続に制限します。
