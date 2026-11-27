# Photo browser

Phase 6 adds a dedicated photo browser to Photocull. The review workflow keeps its existing entry points. The browser serves the same catalogs and media, including when the library is split into one catalog per year. The source tree stays read-only. Ratings, tags, albums and other browser edits live in SQLite. XMP still requires an explicit export.

## Delivery plan

Each row is a reviewable pull request. Stack unfinished branches and retarget after their dependencies merge. Start from `d337a9a`. Keep the browser API and UI in `photocull/api/browse.py`, `photocull/db/browse.py` and `frontend/src/browser/` respectively. Upgrade data in place with the existing migration mechanism.

| PR | Scope | Acceptance |
| --- | --- | --- |
| 6A · Timeline | Cross-library catalog, cursor feed, year/month/day navigation, virtualized justified grid, density and library controls, URL history, Japanese and English browser shell | Equal-time entries paginate without gaps across catalogs. A month jump works without fetching intervening pages. Desktop and 375 px mobile work with keyboard and touch. The existing review view still opens and handles its shortcuts. |
| 6B · Derivatives and delivery | Extend the preview worker, responsive stills and placeholders, video proxies, original downloads, immutable URLs, two cache tiers, reverse proxy examples | Exercise HEIF, RAW, video posters, metadata stripping, orientation, ICC, eviction and recovery. Verify conditional requests, Range, proxy and direct API delivery. Review storage measurements and migration. Inspect the grid with the new media. |
| 6C · Viewer | Fullscreen stills and video, progressive loading, gestures, keyboard, zoom, slideshow, metadata, faces and highlights | Exercise keyboard and touch paths, slow or failed media, source cropping and rotations, and synchronous URL navigation. Inspect screenshots at mobile and desktop sizes. |
| 6D · Search and organization | Filters, cross-library semantic and similar search, tags, people, albums, favorites, ratings, bulk selection, archive handoff, places, days/trips and recently added | Verify link replay, bulk retries and source-safe edits across catalogs, plus mixed-library selection and organization in Playwright. |
| 6E · Offline and release | PWA, adaptive network preferences, browser accessibility and compatibility, performance gates, end-to-end documentation | Verify installation and offline replay with a bounded cache and no client credentials. Confirm the complete profile on mobile and desktop and build the deployment images. |

Before each PR, review the full diff, test edge cases and inspect Playwright screenshots on mobile and desktop. Keep the docs in sync with the shipped behavior. Run Ruff, pytest, frontend lint, build and Playwright, then push and watch CI. Pull requests remain open for user review.

## Using the timeline

Open **Photo browser** in the Photocull header. The default scope includes every configured library. Narrow it with the library selector. Use **Jump to month** or the right-hand scrubber to start at a month and continue into earlier photos. The photos in each row keep their aspect ratios. Use **Grid density** to change how many appear per row. The browser follows the device's color scheme. Choose English or Japanese in its header.

Tab into the grid and use the arrow keys to move between thumbnails. Enter opens a preview. Left and Right step through the loaded photos, and Escape closes the preview. Listings and photo previews have shareable URLs. Use **Refresh timeline** after a scan or to update pending thumbnails. The grid uses the existing 320 px JPEGs, and previews use the existing fit JPEGs, including video posters.

## Catalog and navigation

Use a bounded keyset query against each selected library's SQLite catalog, then merge the sorted results in the API. Order newest capture day and time first, followed by descending library name and asset ID for equal timestamps. Within a day, photos without a capture time come after those with one. A cursor carries the sort key and the selected scope. Each catalog keeps an index for the same order so late pages can seek instead of scanning an offset. Month jumps use a date bound with the same feed. Measure the approach with 100,000 synthetic assets across nine libraries before considering a second index.

Use the History API for browser routing and lazy-load each view. Virtualize justified rows against the viewport. Keep one thumbnail in the Tab order and support arrow-key movement among photos. Load extra pages from the scroll boundary. Offer month selection through a menu and the timeline scrubber.

`GET /api/browse/timeline` accepts `library`, `month` (YYYY-MM), `limit` (1–120, default 72) and an opaque `cursor` returned by the prior page. Keep the same library and month when continuing a page. `GET /api/browse/months` returns the occupied months in the selected library or across all libraries. The feed reflects the live catalogs. Indexing can add photos or update capture dates during a browse session, so refresh to see those changes from the top.

## Media decisions for 6B

Use the existing worker scheduler for requested and background derivatives. The API looks up files and enqueues bounded work. Requested images take precedence over speculative work. Scanning and indexing take precedence over background warm-up. Use the existing media URL tree in both views and retain compatibility for the three JPEG URLs that review and analysis already use. Deploy X-Accel-Redirect as an option with private nginx locations and a direct-file fallback. Compress JSON at nginx.

Start with 256 and 512 px grid images and 1280, 1920 and 2560 px fit images in AVIF, WebP and JPEG. Encode lazily using a versioned recipe and source hash. Store a blurhash or thumbhash for display while media is pending. Apply EXIF orientation, convert to sRGB, retain an sRGB profile where needed for consistent presentation, and strip source metadata including GPS. Native zoom can use a metadata-stripped JPEG generated on demand. RAW uses the existing embedded preview or demosaic path. The worker makes low and medium fast-start H.264/AAC MP4 proxies. Progressive MP4 with byte ranges meets the initial playback requirement. Revisit HLS only if measured seek and startup results warrant segment generation and extra storage. Keep original streaming and download subject to explicit configuration.

Use configurable SSD and HDD cache roots outside the libraries, separate from archive exports, with per-tier limits, batched access tracking and worker eviction. Keep existing JPEGs usable during migration. Reuse them as inputs only where their dimensions and color handling meet the requested recipe. Document which files get regenerated and how existing cache space is reclaimed.

Initial capacity allowance for 16,000 stills is about 3.1 GiB on SSD at 210 KiB per fully warmed grid set of six images. The analogous nine-image fit set is about 24.4 GiB on HDD at 1.6 MiB per still. These are planning allowances, pending fixture measurements. At a combined 3 Mb/s, two video proxies use about 22.5 MB per minute before audio and container overhead. Actual bytes depend on cache demand, encoder settings, video duration and source content.

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

The browser inherits the API's shared authentication and uses same-origin requests through nginx. It stores no tokens. Derivative URLs can use long-lived private caching after their version is in the URL. Keep offline catalog scope minimal and size-limited. Offline browsing will be opt-in, with a clear-data control and recent thumbnail eviction. External map tiles stay off by default. Browse by indexed place names with or without a map.
