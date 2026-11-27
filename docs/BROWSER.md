# Photo browser

Phase 6 adds a dedicated photo browser to Photocull. The review workflow keeps its existing entry points. The browser serves the same catalogs and media, including when the library is split into one catalog per year. The source tree stays read-only. Ratings, tags, albums and other browser edits live in SQLite. XMP still requires an explicit export.

## Delivery plan

Each row is a reviewable pull request. Stack unfinished branches and retarget after their dependencies merge. Start from `d337a9a`. Keep the browser API and UI in `photocull/api/browse.py`, `photocull/db/browse.py` and `frontend/src/browser/` respectively. Upgrade data in place with the existing migration mechanism.

| PR | Scope | Acceptance |
| --- | --- | --- |
| 6A · Timeline | Cross-library catalog, cursor feed, year/month/day navigation, virtualized justified grid, density and library controls, URL history, Japanese and English browser shell | Equal-time entries paginate without gaps across catalogs. A month jump works without fetching intervening pages. Desktop and 375 px mobile work with keyboard and touch. The existing review view still opens and handles its shortcuts. |
| 6B · Derivatives and delivery | Extend the preview worker, responsive stills and placeholders, video proxies, original downloads, immutable URLs, location policy, two cache tiers, reverse proxy examples | Exercise HEIF, RAW, video posters, metadata stripping, per-operation overrides, orientation, ICC, eviction and recovery. Verify conditional requests, Range, proxy and direct API delivery. Review storage measurements and migration. Inspect the grid with the new media. |
| 6C · Viewer | Fullscreen stills and video, progressive loading, gestures, keyboard, zoom, slideshow, metadata, faces and highlights | Exercise keyboard and touch paths, slow or failed media, source cropping and rotations, and synchronous URL navigation. Inspect screenshots at mobile and desktop sizes. |
| 6D · Search and organization | Filters, cross-library semantic and similar search, tags, people, albums, favorites, ratings, bulk selection, archive handoff, places, days/trips and recently added | Verify link replay, bulk retries and source-safe edits across catalogs, plus mixed-library selection and organization in Playwright. |
| 6E · Offline and release | PWA, adaptive network preferences, browser accessibility and compatibility, performance gates, end-to-end documentation | Verify installation and offline replay with a bounded cache and no client credentials. Confirm the complete profile on mobile and desktop and build the deployment images. |

Before each PR, review the full diff, test edge cases and inspect Playwright screenshots on mobile and desktop. Keep the docs in sync with the shipped behavior. Run Ruff, pytest, frontend lint, build and Playwright, then push and watch CI. Pull requests remain open for user review.

## Using the timeline

Open **Photo browser** in the Photocull header. The default scope includes every configured library. Narrow it with the library selector. Use **Jump to month** or the right-hand scrubber to start at a month and continue into earlier photos. The photos in each row keep their aspect ratios. Use **Grid density** to change how many appear per row. The browser follows the device's color scheme. Choose English or Japanese in its header.

Tab into the grid and use the arrow keys to move between thumbnails. Enter opens a preview. Left and Right step through the loaded photos, and Escape closes the preview. Listings and photo previews have shareable URLs. Use **Refresh timeline** after a scan or to update pending thumbnails. For a video, choose a quality and select **Prepare playback**. **Download original** appears when the server enables it.

## Catalog and navigation

Use a bounded keyset query against each selected library's SQLite catalog, then merge the sorted results in the API. Order newest capture day and time first, followed by descending library name and asset ID for equal timestamps. Within a day, photos without a capture time come after those with one. A cursor carries the sort key and the selected scope. Each catalog keeps an index for the same order so late pages can seek instead of scanning an offset. Month jumps use a date bound with the same feed. Measure the approach with 100,000 synthetic assets across nine libraries before considering a second index.

Use the History API for browser routing and lazy-load each view. Virtualize justified rows against the viewport. Keep one thumbnail in the Tab order and support arrow-key movement among photos. Load extra pages from the scroll boundary. Offer month selection through a menu and the timeline scrubber.

`GET /api/browse/timeline` accepts `library`, `month` (YYYY-MM), `limit` (1–120, default 72) and an opaque `cursor` returned by the prior page. Keep the same library and month when continuing a page. `GET /api/browse/months` returns the occupied months in the selected library or across all libraries. The feed reflects the live catalogs. Indexing can add photos or update capture dates during a browse session, so refresh to see those changes from the top.

## Derivatives and playback

The browser's `picture` elements choose AVIF, WebP or JPEG and provide intrinsic widths to match the layout and device pixel ratio. Grid variants use 256 and 512 px long edges. Fit variants use 1280, 1920 and 2560 px. The native JPEG preserves all decoded pixels and is available on demand. RAW native resolution is the resolution of the embedded JPEG or, when absent, the existing half-size demosaic. Video grid and fit images use the shared poster frame. The timeline includes the BlurHash when a derivative has generated one. The original JPEG URLs remain available to the review workflow and as the browser's fallback while a derivative is pending.

The worker shares the existing decoder. It applies orientation, converts ICC-tagged input to sRGB and embeds a compact sRGB ICC profile. It also handles NCLX Display P3 with the sRGB transfer function. For untagged or malformed profiles it assumes sRGB after decoding to RGB. Wide-gamut source colors outside sRGB are clipped. Grid, fit, native zoom, posters and playback proxies carry no source EXIF, XMP or GPS. Browser and worker use a small BlurHash library on each side. Color profiles are CC0 and pinned. [Compact ICC Profiles](https://github.com/saucecontrol/Compact-ICC-Profiles/tree/bdd84663061bc4ae95ca70decff54f581e27f702)

The H.264/AAC MP4 proxies use fast start and byte ranges. The 480p variant caps video at 850 kb/s and uses 64 kb/s audio. The 720p variant caps video at 2 Mb/s and uses 96 kb/s audio. The worker uses the encoder already used for video review. Progressive MP4 covers the initial playback and seek requirements. HLS would add segment generation, files and player logic, so this release uses progressive MP4. Original downloads are separate authenticated requests with Unicode filenames and byte ranges. Original video streaming and original downloads each have their own setting and are off by default. Bulk export still uses the existing archive and ZIP workflows.

The versioned URL is `/api/libraries/{library}/assets/{id}/media/{variant}/{source_sha256}-{recipe}`. The recipe identifies the encoder settings and implementation revision. Files are private and immutable for one year. A request for a missing file queues work in the catalog and returns `202` with `Retry-After`. The worker deduplicates requests and encodes multiple requested stills from one decode. Explicit requests take priority over warm-up. The worker uses one low-priority execution slot for derivatives, and starts new derivative jobs after scans. A warm-up pass uses recent photos and dates represented in recent cache access, and stops when the hot tier reaches 80% of its limit. Once a proxy exists the API or nginx serves it directly with conditional and range requests.

## Location information in shared and exported media

`browser.strip_location` sets the default for sharing, downloading a rendered copy, keeper exports and archive exports. It defaults to `true` and can be set in YAML or overridden by `PHOTOCULL_BROWSER_STRIP_LOCATION`. Each operation can send `include_location: true` or `false` to override the default. The archive preview binds that choice to the confirmation and job. Older confirmed jobs and saved rules preserve the behavior recorded when they were created.

| Operation | Exclude location | Include location |
| --- | --- | --- |
| Shared link or rendered download | A clean JPEG up to 2560 px or a 720p H.264/AAC MP4 | Same media with source coordinates when available. JPEG uses GPS EXIF and MP4 uses QuickTime location. |
| Original download or streaming | A request expressly excluding location is rejected | The original bytes and original metadata are delivered. Original downloads and original streaming retain their separate, default-off permissions. |
| Keeper ZIP or folder export | Full decoded stills as JPEG and video via the 720p MP4 profile. Sidecars are omitted. | Original media bytes and optional existing XMP sidecars. |
| Archive copy or export rename | Same format behavior as a location-free keeper export. Sidecars are omitted. | Original media bytes and existing sidecar behavior. |
| Archive conversion export | Requested conversion format, with source descriptive metadata and sidecars omitted. DNG retains verified raw samples and omits embedded previews. Video retains audio and moving-picture tracks. | Existing verified metadata-preservation workflow. |

Only the worker reads GPS or encodes files. The delivery API accepts `GET` and `HEAD` on `/api/libraries/{library}/assets/{id}/delivery/{version}?action=share|download&include_location=true|false`. It serves generated data after the usual catalog and authentication checks. Shared links pin the flag and source version and require access to the same Photocull instance. The existing photo-page URL opens the authenticated catalog viewer and its metadata panel. Archive in-place operations keep their original metadata behavior.

The cache keys encode both the source version and the location class. Clean delivery reuses the same verified fit JPEG or playback proxy as ordinary browsing. GPS-bearing JPEG and MP4 use distinct keys and can be requested only through the delivery endpoint. This keeps the browser's normal immutable URLs safe regardless of the configured policy and avoids duplicating clean artifacts. Delivery responses set `Cache-Control: private, no-store` and bypass the immutable nginx locations. The worker's GPS-bearing files are owner-only and count toward the large-tier limit. Run the API and worker under the same UID, as in the deployment examples. The offline cache must exclude delivery, original and export routes. Archive RAW metadata scrubbing retains structural tags and still goes through DNGLab pixel and calibration checks. [ExifTool metadata removal notes](https://exiftool.org/faq.html)

## Cache configuration and migration

| YAML under `derivatives` | Default | Purpose |
| --- | --- | --- |
| `enabled` | `true` | Enables the new worker and serving routes |
| `hot_dir`, `hot_mib` | `<state>/derivatives/hot`, 1536 MiB | Grid variants |
| `large_dir`, `large_mib` | `<state>/derivatives/large`, 4096 MiB | Fit, native and video variants |
| `prewarm` | 64 | Candidate photos per library for the newest captures and the most viewed recent day. Use zero to disable. |
| `avif_quality`, `webp_quality`, `jpeg_quality` | 58, 80, 86 | Image encoder quality |
| `original_download`, `original_streaming` | `false`, `false` | Permit original file download or playback |
| `accel_redirect` | `false` | Hand versioned cache files to nginx via its internal locations |

Use the same paths and encoder settings on API and worker. The `PHOTOCULL_DERIVATIVES_*` environment settings are listed in `.env.example`. Cache roots must be outside media and export directories. With 13 GiB of free SSD space, keep the hot tier on SSD and configure the large tier to a dedicated HDD directory such as `/srv/photocull-cache-hdd`. The two limits apply across all catalogs. The API batches access timestamps every 10 seconds. The worker evicts the least recently accessed files across libraries, and evicted files can be regenerated. Allow free space for one in-progress encode and the existing JPEG cache.

The upgrade retains existing 320 px thumbnails, 1600 px detail crops and 2560 px fit JPEGs at their existing paths for review, fallback, ML and reports. They continue to use the existing stale-source cleanup. New still variants decode from the source on demand to avoid JPEG re-encoding and correct the input color. Video variants can reuse the existing 2560 px poster. After a cache directory move, requests regenerate missing files at the new location. An old derivative cache root can be cleared after both services point to the new location.

Nine generated stills (3200–3840 px long edge, with varied detail) measured 9.8–48.6 KiB per photo for all six grid files, with a 17.5 KiB median, and 0.48–6.27 MiB for the complete fit set, with a 3.57 MiB median. At 16,000 stills this extrapolates to 0.27 GiB hot and 55.9 GiB large at the medians, before video, native JPEGs and the legacy cache. The texture mix and encoder settings affect these figures. Two proxies need at most about 22.6 MB per minute at their configured caps, before container overhead. The cache normally stores the variants clients request rather than every combination. Reproduce the still estimate with `uv run --locked --extra worker python -m scripts.benchmark_derivatives`.

## nginx and deployment

For Compose, pre-create the host cache directories, make them writable to the worker, and apply `deploy/compose.browser.example.yml` with the base Compose file. The override mounts the two roots read-only in the API and proxy and enables X-Accel-Redirect. For host nginx, match the `alias` paths in `deploy/systemd/nginx-photocull.conf` to the canonical cache paths. Grant nginx read and traverse permissions for those roots. Add a systemd drop-in with `ReadWritePaths=/srv/photocull-cache-hdd` for the worker when using that HDD path. Keep the existing IP rules and token injection.

The two nginx locations are internal. Public media requests still pass through the API authentication and catalog check. nginx serves the resulting file with sendfile, ETag, Last-Modified, 304 and Range support. To serve directly with FastAPI, leave `accel_redirect` off. The nginx example enables gzip for JSON and UI assets. Test the configuration with `nginx -t` before reloading.

## Performance budgets

| Check | Budget and measurement |
| --- | --- |
| Initial JavaScript | At most 200 KiB gzipped for the main and browser entry chunks and their static dependencies. Check the build in CI. |
| Grid API | Warm p95 below 100 ms for 72 results using 100,000 synthetic assets, nine libraries and mixed first pages, cursor seeks and month jumps. Record local and CI baselines. |
| First thumbnail | Under 3 s with a warm server cache, 375 px viewport, 400 kb/s down, 400 kb/s up and 150 ms latency. Use the Playwright Chromium network profile and record the environment. |
| Grid scrolling | Bound mounted rows to viewport plus overscan. Sample animation frames during a sustained scroll and target a median frame interval of 16.7 ms on a 60 Hz display. |
| API isolation | Keep the existing API dependency-import guard. Verify that catalog reads and media requests do not load decoders, FFmpeg or ML libraries. |

Run `uv run --locked python -m scripts.benchmark_browser` for the API gate. The program uses nine SQLite databases, measures the HTTP response and JSON parsing in FastAPI's test client, and reports first-page, month-jump and cursor samples. Run `npm run build && node e2e/check-bundle.mjs` from `frontend` for the gzip budget. The build manifest identifies the initial entry points and their static imports.

## Authentication and offline

The browser inherits the API's shared authentication and uses same-origin requests through nginx. It stores no tokens. Offline browsing will be opt-in, with a clear-data control and recent thumbnail eviction. External map tiles stay off by default. Browse by indexed place names with or without a map.
