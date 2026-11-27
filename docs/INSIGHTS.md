# Local insights and exports

Phase 4 features are opt-in. Put the settings in the YAML shared by the API and
worker. Install extras in the **worker** environment. The API uses the base
package and reads the SQLite results, serves cached media, and queues jobs.

| Feature | Setting | Worker installation |
| --- | --- | --- |
| Aesthetic scoring and taste learning | `insights.aesthetic`, `insights.taste`, plus `ml.enabled` | `photocull[ml]` |
| Face detection and people | `insights.faces` | `photocull[faces]` |
| Day and place stories | `insights.story` | `photocull[worker]` |
| Video highlights and dead clips | `insights.video` | `photocull[worker]` and FFmpeg |
| Local video transcripts | `insights.transcript` plus `insights.video` | `photocull[transcript]` |
| Folder watching | `insights.watch` | `photocull[worker,watch]`. Polling also works with the worker extra alone. |
| Keeper ZIPs and copies | `insights.export` | `photocull[worker]` |
| Offline trip reports | `insights.report` plus `insights.story` | `photocull[worker]` |

Use `uv sync --locked --extra insights` to install the combined worker extras.
On Debian or Ubuntu, install `ffmpeg` and `libgomp1`. Video proxies require an
FFmpeg build with `libx264` and AAC encoding. `ffprobe` comes with FFmpeg.
OpenCV uses the headless wheel and needs no windowing packages. Optional native
dependencies are OpenCV, watchdog, faster-whisper, CTranslate2, PyAV and the
Hugging Face Hub client.

## Review workflow

- **Recommendations** offers an aesthetic-score filter and descending sort.
  The Photocull prompt head has no separately published or supervised aesthetic
  weights. At first use the worker normalizes four vectors from the pinned
  SigLIP 2 text encoder, then sets its weight vector to the mean of the two
  positive prompts minus the mean of the two negative prompts. The prompts are
  defined in [the source](../photocull/insights.py). For a normalized image
  embedding `v`, the score is `sigmoid(8 * dot(v, weights))`, with no fitted
  intercept. The local float32 weights take about 3 KiB. The prompt head
  implementation and prompt text are MIT-licensed through Photocull. The
  encoder and its weights are Apache 2.0. LAION's published heads were trained
  for CLIP embedding spaces, including a different 768-value space.
  This zero-shot score measures alignment with composition and lighting
  prompts and has no calibrated aesthetic accuracy across genres or cameras.
  The separate supervised taste model learns from the operator's ratings and
  uses the aesthetic score as one feature. [LAION aesthetic predictor](https://github.com/LAION-AI/aesthetic-predictor)
- Burst recommendations blend technical quality and the aesthetic score when
  it is available. Set `burst.aesthetic_weight` to change the blend. The default
  is `0.25`. Existing manual best picks take precedence.
- **Learned preferences** uses the current manual reject flag or 1–2 stars as
  negative examples, and 4–5 stars as positive examples. Three stars and
  unrated photos are neutral. There must be at least eight embedded photos in
  each class by default. A regularized, class-balanced logistic regression
  samples at most 4,096 labeled photos per class. It combines the image vector,
  technical scores and, if enabled, the aesthetic score. The displayed factors
  are signed contributions to the logit for the three strongest feature
  groups. Ratings queue retraining, and **Retrain from ratings** requests a
  fresh fit. Use keyboard ratings and undo in the regular Review view.
- **People** shows detected faces, the photo count and a representative thumbnail
  with the normalized face box. Rename a group, merge it into another, or select
  individual faces to split into a new group. **Review photos of …** filters the
  filmstrip. YuNet supplies boxes and five landmarks. Aligned-crop sharpness,
  pixel size and detector confidence contribute to face quality. Five
  landmarks do not resolve eyelids, so the stage does not produce a closed-eye
  classification. SFace uses a 128-value float16 vector. An incremental HNSW
  lookup assigns sufficiently similar, high-quality faces to existing groups.
  Low-quality detections are saved as unassigned. Open **Unassigned faces** to
  assign them to an existing or new person. The People list defaults to groups
  with at least two faces. Use **Show small groups** to see all assigned groups.
  The migration retains manual and named assignments. Those assignments also
  carry forward when a reprocessed box overlaps the original detection.
  Matched boxes retain their face IDs so in-progress selections remain valid.
  The worker checks current assignments after taking the same SQLite writer
  lock used by merge and split. Face
  vectors, boxes and names stay in the local library state.
- **Stories** uses capture day and splits at a 90-minute gap or a location more
  than 15 km from the current stop's anchor. Undated media is grouped by the
  file day in a separate segment. Up to six representatives per stop balance
  ratings, scoring and visual variety, with one photo per burst. The route
  sketch uses local coordinates. Optional reverse geocoding searches an
  extracted UTF-8 GeoNames `cities1000.txt` or `cities5000.txt` file. Configure
  `insights.story_geonames` with a local path and restart the worker after
  replacing the data. A place farther than 75 km from the closest indexed town
  is displayed as coordinates. [GeoNames downloads and attribution terms](https://www.geonames.org/export/)
- **Videos** samples low-resolution frames twice per second and 4-second
  audio levels. Motion, scene changes, audio energy and image detail rank
  highlight candidates. The blur measure scales edge strength by frame
  contrast. Very short clips and possible pocket recordings receive an
  automatic dead-clip flag. Other signals need corroboration. Darkness
  contributes to the flag only with quiet audio or pocket evidence.
  Review hints show reasons without setting a dead-clip flag. The video list
  groups stabilized copies and copies with a YYYYMMDD or YYYY-MM-DD suffix
  when their unique original is in the same folder and on the current page.
  The override can
  set or clear the flag explicitly or restore the automatic value.
  Clicking a highlight plays its interval in a state-directory 720p H.264/AAC
  proxy. Local speech recognition defaults to off. The optional base model
  runs in INT8 with 30-second audio blocks and supplies seekable text and
  WebVTT captions. Codec, motion and speech results
  vary with source conditions. No original file changes are needed.
- **Watch** uses Linux notifications when available and periodically reconciles
  metadata through the same jailed scan walker. Set `watch_mode: poll` for
  a remote mount with unreliable notifications. Watch scans reuse hashes for
  unchanged files. Once the quiet interval elapses, the watcher checks for
  changes remaining after any manual scan. A quiet interval and before/after source identity checks
  defer files that are still changing. For writers that pause for longer than
  the debounce interval, write a temporary filename outside the supported
  media extensions, then atomically rename it into the library.
- **Exports** selects by inclusive date range, minimum rating, manual reject,
  auto-reject, dead-clip flag or person. ZIP64 archives are written and served
  from disk in chunks. A copy goes to a per-job directory inside a configured
  destination. The worker reserves filenames with the asset ID and preserves
  original file bytes. **Include existing XMP sidecars** copies an existing
  `.xmp` or `.XMP` file with the matching exported stem. The source is opened read
  only. Progress is reported through SQLite. Cancellation discards temporary
  output until final publication starts. A worker restart restarts an interrupted transfer from its
  selection or recognizes an already published output by its filesystem
  identity. Publication uses an atomic no-replace rename on the destination
  filesystem. An existing destination is an error. The standalone XMP write action
  in Review still requires confirmation.
- **Trip reports** generates one responsive static HTML file from the stored
  stories. It waits for pending story updates, then embeds small thumbnails
  and includes the capture dates, stop names and photo captions. It works
  offline and includes GeoNames attribution when applicable.

Use **I** to switch to insights or back to the filmstrip, **Esc** to return to
Review, and **← / →** to move between clips in Videos. Browser video controls
retain their keyboard behavior.

## Settings

Set `PHOTOCULL_INSIGHTS_<FIELD>` on both API and worker or use the `insights`
YAML section. All feature switches default to false.

| YAML field | Environment suffix | Default |
| --- | --- | --- |
| `aesthetic`, `taste`, `faces`, `story` | `AESTHETIC`, `TASTE`, `FACES`, `STORY` | `false` |
| `video`, `transcript`, `watch`, `export`, `report` | `VIDEO`, `TRANSCRIPT`, `WATCH`, `EXPORT`, `REPORT` | `false` |
| `offline` | `OFFLINE` | `false`. Enables offline-only model loading for faces and speech. `ml.offline` controls SigLIP 2 separately. |
| `threads` | `THREADS` | `2` for native Phase 4 stages |
| `face_revision` | `FACE_REVISION` | `48b10fa505aeb51fbd127fc60a5c660040e823b7` |
| `face_detect_score` | `FACE_DETECT_SCORE` | `0.85` YuNet confidence |
| `face_min_px` | `FACE_MIN_PX` | `12` pixels on the detector input |
| `face_threshold` | `FACE_THRESHOLD` | `0.45` SFace cosine |
| `face_min_quality` | `FACE_MIN_QUALITY` | `0.12` for automatic assignment |
| `people_min_faces` | `PEOPLE_MIN_FACES` | `2` |
| `video_very_short_s` | `VIDEO_VERY_SHORT_S` | `1.5` seconds |
| `video_dark_fraction` | `VIDEO_DARK_FRACTION` | `0.65` |
| `video_pocket_fraction` | `VIDEO_POCKET_FRACTION` | `0.5` |
| `video_blur_fraction` | `VIDEO_BLUR_FRACTION` | `0.8` |
| `video_static_motion` | `VIDEO_STATIC_MOTION` | `0.008` |
| `video_static_volume` | `VIDEO_STATIC_VOLUME` | `0.012` |
| `taste_min_per_class` | `TASTE_MIN_PER_CLASS` | `8` |
| `story_gap_minutes` | `STORY_GAP_MINUTES` | `90` |
| `story_distance_km` | `STORY_DISTANCE_KM` | `15` |
| `story_geonames` | `STORY_GEONAMES` | unset |
| `transcript_model` | `TRANSCRIPT_MODEL` | `Systran/faster-whisper-base` |
| `transcript_revision` | `TRANSCRIPT_REVISION` | `ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66` |
| `watch_mode` | `WATCH_MODE` | `auto`. Linux notifications and polling together. |
| `watch_interval` | `WATCH_INTERVAL` | `60` seconds |
| `watch_debounce` | `WATCH_DEBOUNCE` | `3` seconds |
| `export_roots` | YAML only | `{}`. Maps user-facing names to absolute or YAML-relative folders. |

The other new environment variable is `PHOTOCULL_BURST_AESTHETIC_WEIGHT`.
The shared configuration is shown in [deploy/photocull.example.yaml](../deploy/photocull.example.yaml).
The existing `PHOTOCULL_MODEL_DIR` defaults to `<state_dir>/models`. Use a
private state directory and model cache readable by the service accounts.
Model identifiers and revisions are recorded in SQLite. A changed model
identity or a changed media hash queues derived results for recomputation.
Each library's learned model is local to that library.

## Models and deployment

| Model or data | License | Files |
| --- | --- | ---: |
| [Google SigLIP 2](https://huggingface.co/google/siglip2-base-patch16-224) / [pinned ONNX conversion](https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX) | Apache 2.0 | 393.25 MiB shared with Phase 3 |
| [YuNet 2023mar](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet) | MIT | 232,589 bytes |
| [SFace 2021dec](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_recognition_sface) | Apache 2.0 | 38,696,353 bytes |
| [Whisper base CTranslate2](https://huggingface.co/Systran/faster-whisper-base/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66) | MIT | 141.03 MiB |
| Photocull prompt head and taste implementation | MIT code, weights built on device | Approximately 3 KiB per head |
| [GeoNames cities1000](https://download.geonames.org/export/dump/) | CC BY 4.0 | Size changes with the data release |

Prefetch on the worker environment before using Docker's disabled networking
or the systemd unit's private network. The built-in face files are pinned and
SHA-256 verified. A custom face revision expects the matching two ONNX files
to be provisioned under `<model_dir>/opencv-faces/<revision>/`. Speech weights
use the pinned Hugging Face snapshot. Other speech checkpoints require a
compatible CTranslate2 multilingual model and a pinned revision.

```sh
uv run --locked --extra insights photocull fetch-model
uv run --locked --extra insights photocull fetch-insight-models --faces --transcript
```

For an 8-core, 16 GiB CPU-only host, start with `worker.memory_mb: 6144`, a service
memory limit of 8 GiB, and two inference threads. Start with one decode task.
SigLIP 2, OpenCV DNN and CTranslate2 load lazily in one memory-limited child.
Video analysis and archive I/O run in the worker. The service memory limit
also covers FFmpeg. Video proxy disk usage depends on the content and can
reach roughly 0.9 GB per hour at the configured video bitrate ceiling plus
audio and container overhead. An export needs disk space approximately equal
to the selected originals and sidecars. Completed downloads in
`<state_dir>/libraries/<library>/transfers` remain available until the operator
removes the files. Size state and destination storage accordingly.

For copy mode, create each destination before startup. Keep it outside every
library root and outside state. Use real directories without symlinks. Mount
the destination read/write in the **worker** service. The API needs only the
configured destination name. For the hardened systemd example, add a
`ReadWritePaths=/srv/photocull-exports` drop-in for the worker. In Compose, use
the [optional worker-only Compose override](../deploy/compose.insights.example.yml)
with `PHOTOCULL_EXPORT_HOST_DIR` and the matching container path in YAML. Start
with `docker compose -f docker-compose.yml -f deploy/compose.insights.example.yml up -d`.
The destination filesystem must support Linux no-replace rename. The default worker
image contains all opt-in dependencies. The build argument
`PHOTOCULL_WORKER_EXTRA=ml` or `worker` produces a smaller worker image when
those extras are sufficient. [Worker Dockerfile](../deploy/docker/Dockerfile.worker)

## Validation on CT 205

CT 205 exposed two Ryzen 5 5500U CPU cores and 4 GiB RAM. The real-model test
used a 3 GiB worker allocation budget, two threads, a public portrait and an
11-second public speech recording. The cold cache fetch took 2.11 seconds
for the face models and 36.21 seconds for Whisper in this run.

| Measurement | CT 205 |
| --- | ---: |
| SigLIP 2 prompt head from four texts | 1.862 s |
| YuNet plus SFace cold | 0.332 s |
| YuNet plus SFace warm, 12 runs of the fixture | 0.034 s/photo |
| Whisper base INT8, 11 seconds audio | 2.804 s |
| Peak RSS of the shared child after all three workloads | 1,024.0 MiB |
| Model files shared with Phase 3 | 393.25 MiB |
| New face and speech model files | 178.16 MiB |

```sh
uv run --locked --extra insights pytest -m phase4_model -s
uv run --locked --extra insights python scripts/benchmark_catalog.py \
  --assets 100000 --requests 25 --ml --insights
```

The Phase 4 benchmark on 100,000 synthetic assets and 25 requests per case
measured p95 of 14.03 ms for a deep aesthetic cursor with a minimum score,
27.22 ms for the people list, 13.48 ms for photos filtered by person and
3.93 ms for story days. A full synthetic story build took 0.54 s and keeper
selection by person took 0.40 ms. These Phase 4 SQL measurements used
`--insights` alone, with a 185.9 MiB SQLite file. They exclude inference,
decoding, and reading or copying media files.

Ordinary CI uses fake embedding and face models and synthetic media. The
real-model marker checks execution and resource use on selected fixtures.
It does not estimate recognition accuracy on a private collection. Review
face matches and dead-clip flags before using them to select keepers.
