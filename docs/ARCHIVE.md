# Archive and storage

The **Archive** view previews copies, verified conversions, batch renames, and trash actions. You can select manually rejected photos, suggested rejects, flagged unwanted clips, the remainder of a burst, or specific catalog asset IDs. For the remainder of a burst, Photocull excludes the current best pick. Bulk jobs can also match a library-relative folder and its descendants, an inclusive capture date range, elapsed days, camera make or model, extensions, rating, dimensions, size, video bitrate and input video codec. If capture date is unavailable, the catalog falls back to the modification date. The worker collects video properties with ffprobe, including for files indexed before Phase 5. Allow that pass to finish before relying on codec filters.

**Separate export folder** is the default. It writes the verified result under the configured export path and keeps the source in place. **Replace originals after verification** publishes the result next to the source and retains the old file in trash. If the extension changes, the worker publishes the new name before removing the old name. **Export and trash verified originals** publishes to the separate export directory before removing the originals. **Move to trash** is available directly for explicit IDs, rejects, flagged clips, and burst remainders. For selected files that should be re-encoded in place, choose a conversion and apply the selection filter in the same plan.

One preview covers the full matching set. The summary shows the matching and eligible counts, skipped entries, total input bytes, expected output and savings, and estimated runtime. The details are paginated in groups of 100. Confirming from any page approves the same full snapshot. `after_id` remains available if you deliberately want a smaller scope. Storage remains allocated to backups until purge.

The worker reads the catalog from a single SQLite snapshot and saves the manifest in short batches. The API queues the preview and reads status or a page from SQLite. Previewing writes only planning records in Photocull state. The confirmation digest covers the policy, filters, and ordered manifest, including catalog hashes, file identities, sidecar state, paths and aggregate totals. Confirm within seven days. The worker materializes approved items in batches of up to 128. It never expands the approved set to include newly indexed files. A changed source, selection or sidecar is skipped and recorded in the audit. Estimates use codec heuristics and known clip durations.

## Configure the worker

Install the locked worker extra and the two runtime packages below. The Compose worker image already installs them and the pinned DNGLab binary. The API image continues to use the base Python package and SQLite.

```sh
sudo apt-get install --no-install-recommends ffmpeg libimage-exiftool-perl util-linux curl ca-certificates
sudo scripts/install_dnglab.sh
uv sync --locked --extra worker
```

FFmpeg must expose `libx265` and `libaom-av1`. Use the Debian/Ubuntu package with those encoders or an equivalent build. The locked Pillow and pillow-heif manylinux wheels supply JPEG, AVIF and HEIC encoders. Source-built wheels need the corresponding native codec libraries and build configuration. The install script fetches the upstream `amd64` or `arm64` DNGLab deb and verifies its SHA-256.

Share these settings between the API, worker, and CLI. Create the export root with the worker's UID/GID before starting. It must be outside all media and state directories. Set write roots only for media subtrees that the worker may change. The value of `PHOTOCULL_WRITE_ROOTS`, when set, is a colon-separated list of canonical absolute paths and overrides `archive.write_roots` in YAML.

```yaml
archive:
  export_roots:
    photos: /srv/photocull-archive
  write_roots:
    - /mnt/PhotoLibrary/ArchiveCandidate
  retention_days: 0
  parallelism: 1
  threads: 2
  memory_mb: 1536
  timeout_seconds: 86400
```

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `PHOTOCULL_WRITE_ROOTS` | Empty | Explicit archive write scopes |
| `PHOTOCULL_ARCHIVE_THREADS` | 2 | Maximum visible CPU cores and configured codec threads per child |
| `PHOTOCULL_ARCHIVE_PARALLELISM` | 1 | Concurrent items, at most four |
| `PHOTOCULL_ARCHIVE_MEMORY_MB` | 1536 | Per-child Linux virtual address space limit in MiB |
| `PHOTOCULL_ARCHIVE_TIMEOUT_SECONDS` | 86400 | Time limit per tool invocation |
| `PHOTOCULL_ARCHIVE_RETENTION_DAYS` | 0 | Days before automatic purge, disabled at zero |
| `PHOTOCULL_ARCHIVE_EXPORT_HOST_DIR` | `./.photocull/archive-exports` | Host directory used by the sample Compose override |

Use a read-only mount for the library and a narrow read-write bind at the same canonical path for each approved subtree in the worker. Bind the export directory read-write to the worker and read-only at the identical path in the API. Apply the same layout to a separate trash mount. [Compose override example](../deploy/compose.archive.example.yml)

For systemd, keep `ProtectSystem=strict` and add only the archive destination and allowed subtrees to the worker's `ReadWritePaths` drop-in. The worker needs read and write access to the approved paths for filesystem checks and publication. Its existing permissions for explicitly requested XMP rating export are configured separately.

```ini
[Service]
ReadWritePaths=/srv/photocull-archive /mnt/PhotoLibrary/ArchiveCandidate
CPUQuota=600%
MemoryHigh=6G
MemoryMax=8G
```

For the planned 8-core, 16 GiB host, start with one item and two threads, then measure representative exports before increasing concurrency. Account for the existing ML/indexing RSS, simultaneous codec children, codec address-space limits, and the service's shared memory ceiling. Large or complex media may need a higher child limit. Archive work starts when the index queue is idle. Encoding, verification and video probing use CPU affinity, per-tool limits, `nice`, and idle I/O priority where supported. Archive children are tied to the lifetime of their worker. Exports need space for a private input snapshot, staged output, and any cross-device backups. Trash on the same filesystem normally transfers directory entries directly.

## Filesystem preflight

Open **Archive → Write access** to see the worker's result for each configured write root. The worker tests the root on startup or after it detects a changed mount identity and reports readiness through the API. Use **Run worker filesystem checks** to request a fresh result after a mount or permissions change. In-place overwrite, removal, trash operations, and batch rename are disabled until the relevant write roots pass. Exports and catalog reports remain available with their own configured permissions.

Run the same diagnostics from the worker's user and mount namespace to inspect any existing directory inside an approved write root. Add `--library NAME` when more than one library is configured.

```sh
photocull archive selftest /mnt/PhotoLibrary/ArchiveCandidate
docker compose exec worker photocull archive selftest /mnt/PhotoLibrary/ArchiveCandidate
```

The command creates a private directory of synthetic files. It checks directory permissions and available bytes, durable file and directory `fsync`, no-clobber rename, and atomic exchange. It reports the mount ID, default or configured trash path, and whether the two directories share a mount. Standalone CLI results are diagnostic. The worker performs and records its own check before enabling destructive jobs and rechecks live mount identity before each publication. Declare nested mounts separately in `PHOTOCULL_WRITE_ROOTS` so they receive their own probe. The temporary test directory is excluded from scanning and watching.

NFS and CIFS configurations vary by server, mount options and kernel support. A failing probe disables destructive work on that scope. A passing probe demonstrates the tested operations under current conditions. Power-loss behavior and remote storage recovery remain the responsibility of the filesystem and storage provider. Test the actual production mount in the worker namespace before enabling it. Keep SQLite state on a local filesystem.

## CLI

An example for RAW captures from 2018 through 2024. Run the same request and use the confirmation digest printed by preview. Pass `--yes` when submitting any job.

```json
{
  "operation": "dng",
  "filters": {
    "start": "2018-01-01",
    "end": "2024-12-31",
    "extensions": ["arw", "dng"]
  },
  "reason": "Archive 2018–2024 RAW"
}
```

```sh
photocull archive photos preview plan.json
preview_id='paste-the-preview-id-here'
photocull archive photos preview-page "$preview_id" --after 100
confirmation='paste-the-preview-digest-here'
photocull archive photos apply plan.json --confirmation "$confirmation" --yes
photocull archive photos jobs
job_id='paste-the-job-id-here'
photocull archive photos jobs "$job_id"
photocull archive photos cancel "$job_id"
photocull archive photos recover "$job_id"
photocull archive photos trash
photocull archive photos audit --after 0
photocull archive photos storage --by camera
photocull archive photos duplicates --by exact
```

For photos, use `operation` `dng`, `jpeg`, `heic`, or `avif`. For videos, use `hevc` or `av1`. For a verified file copy, use `copy`. Set `mode` to `export`, `overwrite`, or `remove` for conversions. JPEG, HEIC and AVIF accept quality 1–100, with defaults 85, 72, and 70. HEVC accepts CRF 0–51 and AV1 0–63, with defaults 25 and 31. Lossy codecs offer `fast`, `balanced`, and `slow` presets. Filters use `ids`, `after_id`, `start`, `end`, `older_days`, `folder`, `camera`, `extensions`, `selection`, `burst_id`, `min_rating`, `max_rating`, `min_width`, `min_height`, `min_bytes`, `min_kbps` and `codecs`.

Use `{"operation":"restore","trash_ids":["<trash-id>"],"collision":"rename"}` for a restore with a numeric suffix when occupied. Use `operation: "purge"` for permanent deletion, or `operation: "undo"` for a rename recorded in trash. For a cutoff use `{"operation":"purge","trash_before":"2025-01-01"}`. Cutoffs use UTC midnight and choose entries strictly earlier than that date, oldest first. Preview and apply those requests the same way. The default `collision: "fail"` skips occupied names. A restored copy of an original may coexist with a previously published conversion.

## Conversion tools and verification

| Tool | Reason and license | Runtime sizes |
| --- | --- | --- |
| DNGLab 0.8.0 | Native Linux CLI, lossless JPEG-compressed DNG and RAW sample checksums. LGPL-2.1. | Upstream amd64 deb 3.11 MiB, with 12.56 MiB installed. |
| FFmpeg with x265 and libaom | CPU-only HEVC/AV1 and portable probing. FFmpeg with GPL components is GPL-2.0-or-later, x265 is GPL-2.0, libaom is BSD-2-Clause with the AOMedia patent license. | Already a worker dependency. On the validation CT, FFmpeg package 2.72 MiB, libx265 16.37 MiB, libaom 5.30 MiB installed, excluding other libraries. |
| ExifTool | Structured transfer and verification of EXIF, GPS, XMP, IPTC and ICC. GPL-1.0-or-later or Artistic-1.0, on the same terms as Perl. | `libimage-exiftool-perl` 27.52 MiB installed on the validation CT. |
| Pillow and pillow-heif | Reuse locked worker wheels for stills. Pillow MIT-CMU and pillow-heif BSD-3-Clause. Bundled libavif BSD-2-Clause, libheif LGPL-3.0 and x265 GPL-2.0. | On x86-64, approximately 4.7 MiB for libavif, 2.9 MiB for libheif and 21.8 MiB for its x265 library. These wheels were already worker dependencies. |
| util-linux `ionice` | Idle I/O class when supported. The `ionice` component of the distribution package is GPL-2.0-or-later. | Included in the Debian base installation. |

Tool sources and terms are available upstream. [DNGLab license](https://github.com/dnglab/dnglab/blob/v0.8.0/LICENSE) · [FFmpeg licensing](https://ffmpeg.org/legal.html) · [x265 licensing](https://x265.readthedocs.io/en/2.7/introduction.html) · [libaom license](https://aomedia.googlesource.com/aom/+/refs/heads/main/LICENSE) · [ExifTool](https://exiftool.org/) · [Bundled HEIF terms](https://github.com/bigcat88/pillow_heif/blob/master/LICENSES_bundled.txt) · [libavif license](https://github.com/AOMediaCodec/libavif/blob/main/LICENSE)

Adobe distributes DNG Converter for macOS and Windows. The Fimagena Linux `raw2dng` project focuses on the original Sony A7 and lacks a published α7C II coverage matrix. DNGLab lists **Sony α7C II (ILCE-7CM2)**, including all ARW modes in its pinned support matrix, plus many Canon, Nikon, Fujifilm, Panasonic, Pentax and Olympus models. Current coverage includes DNG input. Photocull locally exercised generated Bayer DNG samples. Sony support is based on the upstream matrix. [Adobe converter](https://helpx.adobe.com/camera-raw/desktop/dng-and-file-formats/adobe-dng-converter.html) · [raw2dng](https://github.com/Fimagena/raw2dng) · [DNGLab v0.8.0 camera list](https://github.com/dnglab/dnglab/blob/v0.8.0/SUPPORTED_CAMERAS.md)

- DNG uses lossless JPEG compression. The worker verifies raw dimensions, decoded sample checksums, black/white levels, white balance, crop, and specified portable metadata. It omits the embedded original RAW file. The default `"dng_preview": "embed"` writes a JPEG preview and thumbnail so browsers such as Photocull and Lightroom can display the DNG without demosaicing raw samples for every initial view. Use `"dng_preview": "omit"` to prioritize storage for cold archives. The worker verifies the selected preview policy and still requires the output to be smaller. Original formats with sensor, crop or metadata transformations that cannot be verified fail closed. Vendor MakerNotes, proprietary aux planes, external edits and nondisplayed alternate images do not have a general interchange equivalent. Keep archival backups where those are required.
- Still conversion verifies decoded dimensions, orientation and alpha presence, retained capture and exposure metadata, GPS, portable XMP/IPTC and the original ICC payload when present. JPEG requires an opaque source. Multipage, animated, high-bit-depth, unsupported color-mode and incompatible-profile input requires a separate workflow. Lossy compression changes pixel values.
- Video uses FFmpeg software encoding, keeps the input container family, maps all streams and chapters, copies non-primary streams, and preserves selected video pixel format/color properties and rotation. The verifier decodes all output audio and video, compares stream types/counts and metadata, checks chapter timing, allows 100 ms of overall duration difference and at most one decoded video frame per video stream. Unsupported streams, container combinations and metadata differences fail that item. AV1 uses libaom for its programmable CPU-use presets and stable decoder support. SVT-AV1 was considered for throughput, but is not the selected encoder.
- A verified conversion is published only if output is smaller. File mtime is preserved to the precision recorded by the source filesystem. An incompatible destination timestamp precision rejects the operation before removal.
- Both `STEM.xmp` and `STEM.ext.xmp` follow the verified output. For same-stem in-place publication, a sidecar whose path already matches stays beside the output. A shared `STEM.xmp` for a JPEG+RAW pair stays with surviving siblings and is copied when the new stem or directory differs. Removing an asset moves its unshared sidecar into the same trash ticket. A rename and its undo include renamed sidecars. Conflicting or symlink sidecars are skipped.

## Trash, recovery and audits

The default trash is `.photocull-trash` under the allowed subtree on the source's filesystem. For nested filesystems the worker finds an eligible ancestor on the same device. To use a separate path, set `archive.trash_roots.<library>` and authorize its parent via `PHOTOCULL_WRITE_ROOTS`. A custom trash inside media must sit under a `.photocull-trash` directory so the scanner and watcher skip it. Across filesystems, the worker copies to a private temporary file, verifies SHA-256 and metadata for the copy, publishes it and then removes the original. For replacement, the worker verifies the output and secures the old bytes in trash before publishing. Overwriting the same name uses an atomic exchange.

The worker verifies original catalog identity, rechecks the confirmed selection and sidecars, rejects symbolic-link components for writes, and uses no-clobber publication for new names. Destructive workflows use direct library paths. Linked views can export read-only copies, including those sourced from allowed link targets. The writable mounts need durable file and directory `fsync` plus Linux `renameat2` support for the requested operation. Use a filesystem local to the worker with those guarantees for write scopes and verify with synthetic data before using a remote mount.

One confirmed archive job can be active per library. Cancellation stops uncommitted work and allows an already journaled item to reach its recorded end state. Restart replays the durable per-item journal, checking hashes and file identities. An item with a new conflict stops in **recovery**. Inspect the affected names and audit entries, resolve the external conflict and choose **Retry blocked recovery**. Keep the database, private staging directories and trash until the recovery completes. Keep importers and other writers out of the same writable subtree during destructive jobs.

For the same library, filesystem publication and an index scan cannot overlap. A scan already in flight can finish while encoding runs and the worker revalidates the item before publication. A crash during a verified overwrite holds the old and new catalog paths until publication recovery resolves. An in-place conversion preserves the original catalog ID when the target has no separate catalog history. It keeps ratings and manual burst picks, then regenerates previews, scores, embeddings, and groups for the new hash. Existing face boxes are used only to match new detections and preserve assignments. A historical asset at the target keeps its catalog ID and receives the review rating. Its visuals and groups are rebuilt from the new data.

The job records approved actor, reason and time, and append-only audit entries hold before/after SHA-256, byte sizes and trash paths. API actions use the configured local or shared-token principal and the authenticated connection address as observed by the API. A shared proxy credential cannot distinguish individual browser users. CLI actions use the local OS user and UID. SQLite triggers protect append-only records during normal operation. Filesystem or database administrators control the underlying storage. Back up the index and keep it with the corresponding trash directories.

Trash lists total count and allocated file bytes, including sidecars. Its cutoff preview takes the oldest entries first across all pages and requires normal permanent-delete confirmation. A missing trash directory can be recreated for new backups when its approved parent still passes. Entries whose backup files vanished stay in the catalog for diagnosis and are skipped on restore or purge.

Retention is off by default. Enabling it approves worker-managed purge of expired trash entries. Permanent deletion is ordinary filesystem removal. Snapshots, backups, hard links and storage-provider recycle bins can retain data outside Photocull.

## Storage and duplicates

**Storage** groups indexed bytes and rough replacement savings by year, folder, format or camera. **Duplicates** groups exact SHA-256 matches, visually similar assets from ML/bursts or matching daily perceptual hashes, and same-folder JPEG+RAW pairs with matching case-insensitive stems. Select IDs from a result and preview an operation. Near matches and file pairs are review suggestions.

**Batch rename** accepts `{stem}`, `{date}`, `{year}`, `{camera}`, `{id}` and `{n}`, plus literal text. The extension stays the same. Use `{id}` for a suffix stable across batches. In-place renames retain the originals in trash so they can be undone. Retention applies to rename backups as well. Ratings transfer to verified changed names during reindexing.

The worker skips outputs that grow. Duplicate matches and filename pairs remain review suggestions because visual similarity and shared names do not establish whether a file can be discarded. Rename templates use the fixed tokens above.

## Location information in exports

Archive copies, export renames and keeper exports preserve the original media byte for byte by default, including GPS and embedded metadata. Archive preserves its usual sidecar behavior. Keeper exports include existing XMP only when requested. The Browser’s sharing and report preference has no effect on these defaults.

Choose **Remove location information** to scrub only the exported copy. ExifTool removes location metadata from JPEG, HEIC/HEIF/HIF and DNG and removes descriptive XMP, IPTC and camera MakerNotes that can also carry location. It preserves orientation, color and the image format, with decoded pixel verification for JPEG and supported HEIF and raw-sample and calibration verification for DNG. Location-free DNG copies omit embedded originals and private metadata blocks. Supported MP4/M4V, MOV and MKV files use FFmpeg stream copy with container and stream metadata removed. The worker checks stream properties and encoded audio/video packet hashes. Removal omits XMP sidecars.

Unsupported formats, additional or timed metadata tracks, and files that fail verification require retaining metadata or choosing an explicit Archive conversion. The worker never silently re-encodes a copy or changes its extension. The original remains available with its metadata. Archive conversions apply the selected codec and its existing verification when the operator chooses that operation.

## Saved conversion rules

Choose a conversion in **Plan**, enter its filters and quality, and choose **Save as a rule**. For example, select HEVC, CRF 25, minimum width 3840, minimum video bitrate 30,000 kbps and minimum age 1095 days. The default interval of 0 creates a manual preview rule. Set a positive interval to have the worker prepare a new snapshot periodically. **Rules** indicates when a new preview is available, and offers **Pause schedule** and manual **Prepare preview**. Every rule result goes through the standard confirmation dialog before conversion. Trash retention is configured separately.

## CT 205 validation

Run the generated-fixture benchmark from the project directory. `--smoke` uses small inputs for the Docker build check. The full run uses a 3072×2048 Bayer DNG and TIFF and an eight-second 1280×720 clip with audio and capture metadata.

```sh
uv run --locked --extra worker python -m scripts.benchmark_archive --dng-preview omit
```

The benchmark reports end-to-end worker time, source/output size and peak RSS of the worker and the largest codec/verification child. Results depend on image content, codec build, storage and presets. The illustrative compression ratios from these synthetic samples do not predict user library savings.

The original 2026-11-26 run omitted DNG previews and used the CT 205 LXC workspace, two AMD Ryzen 5 5500U cores, 4 GiB RAM, and CPU only. DNGLab 0.8.0, FFmpeg 8.0.1, ExifTool 13.50, Pillow 13.0.0 and pillow-heif 1.9.0 ran with one item and two allowed CPU cores. Overall worker RSS in the isolated benchmark was 94.1 MiB, excluding indexing or ML running separately.

| Output | Worker time | Input → output | Reduction | Peak child RSS |
| --- | ---: | ---: | ---: | ---: |
| DNG | 1.45 s | 12,583,486 → 2,372,472 bytes | 81.15% | 55.1 MiB |
| JPEG | 1.42 s | 18,878,196 → 217,056 bytes | 98.85% | 112.4 MiB |
| HEIC | 3.94 s | 18,878,196 → 228,851 bytes | 98.79% | 212.1 MiB |
| AVIF | 2.03 s | 18,878,196 → 42,599 bytes | 99.77% | 238.4 MiB |
| HEVC | 11.95 s (0.67× clip speed) | 5,670,901 → 2,798,935 bytes | 50.64% | 318.0 MiB |
| AV1 | 14.86 s (0.54× clip speed) | 5,670,901 → 2,343,627 bytes | 58.67% | 301.6 MiB |

### DNG embedded preview cost

On 2026-11-27, the same 12,583,486-byte synthetic DNG produced the following results on CT 205. The default embedded policy trades a larger child process and a 110,656-byte increase in output for an embedded view.

| DNG policy | Worker time | Output | Peak child RSS |
| --- | ---: | ---: | ---: |
| Embed JPEG preview and thumbnail | 2.01 s | 2,483,128 bytes | 235.5 MiB |
| Omit preview and thumbnail | 1.45 s | 2,372,472 bytes | 55.2 MiB |

### 100,000-item planning and API load

On 2026-11-27, CT 205 prepared a manifest from 100,000 generated files matching a 2018–2024 DNG conversion. Planning took **26.33 s**. The API queued the request in **6.73 ms** and submitted the confirmed job in **4.82 ms**. The API and planner ran in separate processes and the test issued 775 requests to each measured endpoint during planning.

| Endpoint | Concurrent p95 |
| --- | ---: |
| Health | 4.58 ms |
| Gallery, 20 items | 7.27 ms |
| Preview status or first detail page | 5.42 ms |

These are in-process HTTP test client measurements for API and SQLite work on CT 205. Filesystem and client latency depend on deployment. The benchmark confirms the full manifest without processing the queued conversion.

```sh
uv run --locked python -m scripts.benchmark_archive_preview --assets 100000
```
