# Library indexing

## Read errors and retries

The indexing panel shows files registered during the current scan and separate counts for unreadable files and folders. Open the count to see the library-relative path, failed operation and cause. A scan can finish with warnings after cataloguing the readable files. An inaccessible library root ends that library's scan with an error and keeps its previous catalog.

An unreadable file or subtree keeps its last known catalog records, including ratings. A later complete scan retires entries that are absent. Fix file or directory access for the worker user, then run **Scan library** again. Polling watch mode detects recovered access and changed source identities. Use a manual scan to retry a persistent read I/O error on an otherwise unchanged file.

- `GET /api/libraries/{library}/jobs`, the library event stream and `GET /api/health` include `files_indexed`, `bytes_hashed`, `file_errors`, and `directory_errors` in the latest `scan`. `bytes_hashed` counts successfully hashed source bytes and excludes unchanged files whose hash was reused.
- `GET /api/libraries/{library}/scans/{scan_id}/errors?limit=50&cursor=0` returns `items` and `next_cursor`. Each issue includes `path`, `kind`, `operation`, `code`, and nullable `errno`. Use `next_cursor` to continue. The report for an interrupted scan starts fresh when the worker retries that request.
- `photocull status <library>` shows the same scan counters from SQLite.

## Initial ingest on one HDD

A previous worker turn interleaved a small hash batch, media jobs and model work for each library. With several folders on one HDD, that sent reads to different parts of the disk before any library finished registering files.

A scan traverses the library, hashes new or changed files and queues the media jobs. The default worker scans one whole library at a time and gives scans precedence over EXIF, previews, scoring and model analysis. While scans run, the API can show the catalog and the indexing count before previews become available. Searches of existing embeddings and worker heartbeats still run.

A pinned directory supplies file stats directly. Hashing reads each file in order with a reusable 4 MiB buffer and Linux sequential read-ahead advice. Identity checks still fence files replaced or edited during hashing. The worker commits up to 64 scan outcomes at a time and also checkpoints between files when a batch has been open for about two seconds.

After scanning, the worker processes a bounded wave of nearby media jobs for one library before visiting the next library. It fills free EXIF, preview and score slots as jobs finish. Scores wait for the preview they use. Each library drains basic media jobs before embeddings, tags and the other analyses; face and video work use the existing insight queues and model limits. The native EXIF reader uses a process-wide lock. Preview and score work can use the other stage slots while one EXIF reader is active. The pending-media index avoids walking the embedding backlog to claim those jobs. When a source changes, the worker cleans the specific cache files for the old content hash.

| YAML under `worker` | Environment override | Default | Effect |
| --- | --- | ---: | --- |
| `scan_parallelism` | `PHOTOCULL_WORKER_SCAN_PARALLELISM` | 1 | Whole libraries scanned concurrently. Use 1 for a single HDD. Consider 2 on separate physical disks or a fast SSD. |
| `scan_overlap` | `PHOTOCULL_WORKER_SCAN_OVERLAP` | false | Permit media processing in other libraries during scans. Enable when separate disks or fast storage make overlapping reads worthwhile. |
| `scan_batch_size` | `PHOTOCULL_WORKER_SCAN_BATCH_SIZE` | 64 | Maximum scan outcomes per commit. Use this to tune scan transactions previously controlled by `batch_size`. |
| `hash_read_mib` | `PHOTOCULL_WORKER_HASH_READ_MIB` | 4 | Reusable read buffer per active scan. |
| `parallelism` | `PHOTOCULL_WORKER_PARALLELISM` | 1 | Maximum concurrent EXIF, preview or scoring jobs across the worker. |
| `batch_size` | `PHOTOCULL_WORKER_BATCH_SIZE` | 8 | Jobs per library turn, raised to `parallelism` if necessary to fill every slot. |

For an 8-core / 16 GiB CPU-only worker using one HDD, keep `scan_parallelism: 1` and `scan_overlap: false`. Start with `parallelism: 2`, then try 3–4 if CPU utilization stays low and the HDD still has headroom. Set `batch_size: 16` or `32` if frequently switching libraries hurts media throughput. Keep the library mount read-only and place the SQLite state and preview cache on SSD when available. The example systemd service caps the worker at two cores and 2 GiB, and Compose has equivalent defaults. When raising concurrency, check `CPUQuota` or `PHOTOCULL_WORKER_CPUS` and the service memory limit along with `worker.memory_mb`. For ML, account separately for the inference child and tune `ml.threads`, `ml.batch_size` and `insights.threads` for the enabled models.

## Synthetic benchmark

The benchmark writes large uncompressed TIFFs and runs the regular worker with a fresh index for each revision. It measures the time until all scans have registered their files and the time until EXIF, preview and scoring have completed. The optional `--cold-source` flag flushes the fixture files and asks Linux to evict their pages before each run. It needs space for the source fixtures and each run's state and previews.

```sh
git worktree add --detach /tmp/photocull-ingest-baseline 2cc7c49
uv run --locked --extra insights python -m scripts.benchmark_ingest \
  --baseline /tmp/photocull-ingest-baseline --directory /path/to/test-disk \
  --files 16 --parallelism 2 --repeat 2 --cold-source
```

Two completed development runs on local ext4/SSD used 2 vCPUs, 4 GiB RAM and eight libraries of 16 TIFFs each. Each TIFF was 3,072 × 2,048 RGB and about 18 MiB. The source total was 2.25 GiB. The second run reversed the order of the cases. Times include worker startup.

| Worker revision | Media slots | Time to register all files | Time through EXIF, preview and score | Equivalent time for 10,000 files |
| --- | ---: | ---: | ---: | ---: |
| Before (`2cc7c49`) | 1 | 10.0–22.5 s | 46.2–59.8 s | 60–78 min |
| With ingest changes | 1 | 8.2–8.7 s | 43.0–46.9 s | 56–61 min |
| Before (`2cc7c49`) | 2 | 11.4–19.6 s | 42.2–64.7 s | 55–84 min |
| With ingest changes | 2 | 8.0–8.6 s | 32.9–34.3 s | 43–45 min |

The two-slot runs registered 267–287 MiB/s of source media with the new scheduler, compared with 118–202 MiB/s before. These are end-to-end registration rates. The old scheduler also spent part of that time running media jobs. The 10,000-file estimates scale the measured time per TIFF. Production time on HDD depends on file size and layout, RAW decoding, video duration and the enabled ML stages. A repeat during sustained system-wide I/O stalls was interrupted and excluded from the table.

The final worker commit (`b37e4e6`) was also run with 96 synthetic TIFFs of 2,560 × 1,536 pixels (1.05 GiB) on tmpfs. With one media slot, registration took 2.29 s before and 1.40 s after, and the full pipeline took 8.00 s before and 7.41 s after. The two-slot pipeline took 6.50 s before and 43.28 s after. The latter trial coincided with about 67–76% full system-wide I/O stall time reported by `/proc/pressure/io`, even with source and state on tmpfs. Keep that trial separate from the ext4 results when setting throughput expectations.
