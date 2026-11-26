# Photo search, similarity and tags

Photocull embeds cached still-photo previews on the CPU. JPEG, RAW and HEIF photos use the same oriented preview already generated for review. Search, grouping and tag assignment write to the library's SQLite catalog. Original media is read-only in this pipeline. XMP export requires the existing explicit review and confirmation.

## Model and execution

The default is the split INT8 ONNX export of the multilingual **SigLIP 2 Base/16 224** model. Photocull pins the repository revision and stores the model ID, revision and preprocessing settings under a fingerprint in SQLite. Changing any of these requeues image jobs and prevents results from mixing vectors across versions. Vectors are L2-normalized, 768-dimensional little-endian float16 BLOBs (1,536 bytes per photo before SQLite overhead). [Google model card](https://huggingface.co/google/siglip2-base-patch16-224) · [ONNX export](https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX)

| Consideration | ONNX Runtime CPU, split INT8 | OpenCLIP + PyTorch CPU, comparable full-precision model |
| --- | --- | --- |
| Model cache | 90.2 MiB vision, 270.3 MiB text, 32.8 MiB tokenizer | About 1.4 GiB just for the equivalent upstream FP32 weights |
| RAM | 841 MiB peak child RSS measured with both encoders loaded | The FP32 parameters alone require about 1.4 GiB resident when both encoders are loaded, plus the Python runtime and activations |
| CPU | Quantized matrix kernels and a measured 0.205 s per photo on CT 205, 2 threads and batches of 4 | FP32 inference reads four-byte weights and uses float kernels. Runtime and speed depend on the CPU and build. No PyTorch timing was taken on CT 205. |
| Packages | `onnxruntime`, `tokenizers`, `usearch` plus the existing worker extra | PyTorch CPU, torchvision and OpenCLIP, plus a compatible multilingual model and tokenizer |

ONNX Runtime provides a smaller cache and supports separate CPU sessions for vision and text. The measured speed and memory use fit the worker budget with both sessions resident. Quantized performance varies with the CPU instruction set. [Upstream weight sizes](https://huggingface.co/google/siglip2-base-patch16-224/tree/main) · [ONNX Runtime quantization guide](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)

The image and text sessions open when first needed. The worker downloads only the required files into `<model_dir>/<owner>/<model>/<revision>/`, verifies new default downloads and renames them into place. `photocull fetch-model` prepares all three files ahead of time. A worker with both text search and tags disabled needs only the vision file. Set `ml.offline: true` after provisioning to prohibit downloads. Missing files, missing packages and insufficient allocation budget surface in embedding progress. Pending image jobs record a failure when the encoder is unavailable. The worker picks those jobs back up on restart once the cache or configuration is fixed.

## Configuration

Set the feature flags and model identity in the shared API and worker configuration. Install `photocull[ml]` only in the worker environment. Use `PHOTOCULL_WORKER_MEMORY_MB=6144` with an 8 GiB service memory ceiling on a 16 GiB host. CT 205's small validation library ran with 3 GiB and two threads. The worker applies Linux `RLIMIT_AS` to the one shared ML subprocess after reserving its own resident memory and decode headroom. Compose `mem_limit` or systemd `MemoryMax` caps the whole service, including concurrent decoding and subprocesses. Start with two inference threads and one media decode task, then size thread count and service CPU quota for the machine.

| Environment variable | YAML | Default |
| --- | --- | --- |
| `PHOTOCULL_MODEL_DIR` | `model_dir` | `<state_dir>/models` |
| `PHOTOCULL_ML_ENABLED` | `ml.enabled` | `false` |
| `PHOTOCULL_ML_SEARCH_ENABLED` | `ml.search_enabled` | `true` when ML is enabled |
| `PHOTOCULL_ML_TAGS_ENABLED` | `ml.tags_enabled` | `true` when ML is enabled |
| `PHOTOCULL_ML_MODEL_ID` | `ml.model_id` | `onnx-community/siglip2-base-patch16-224-ONNX` |
| `PHOTOCULL_ML_MODEL_REVISION` | `ml.model_revision` | `ba1f3b0843f24bc5417d38e19c37b287d719b2f4` |
| `PHOTOCULL_ML_IMAGE_SIZE` | `ml.image_size` | `224`, or `256` for a compatible export |
| `PHOTOCULL_ML_OFFLINE` | `ml.offline` | `false` |
| `PHOTOCULL_ML_BATCH_SIZE` | `ml.batch_size` | `4` photos per inference call |
| `PHOTOCULL_ML_THREADS` | `ml.threads` | `2` ONNX CPU threads |
| `PHOTOCULL_ML_KEEP_TEXT_ENCODER` | `ml.keep_text_encoder` | `true`, reuse the session and tokenizer across requests |
| `PHOTOCULL_ML_SIMILAR_THRESHOLD` | `ml.similar_threshold` | `0.90` minimum cosine for image matches and similarity groups |
| `PHOTOCULL_ML_TAG_TOP_K` | `ml.tag_top_k` | `3` labels per photo |
| `PHOTOCULL_BURST_MIN_COSINE` | `burst.min_cosine` | `0.88` minimum cosine for burst members with embeddings |

Custom models must use the split fixed-resolution SigLIP 2 ONNX signatures and filenames, a matching tokenizer with 64 padded tokens, and a pinned 40-character commit revision. Use matching `image_size` and weights. Japanese retrieval depends on the chosen checkpoint's multilingual training. A monolingual model can give misleading Japanese results. Setting `keep_text_encoder: false` frees the text session and tokenizer after each query and after preparing the tag vocabulary. It adds a session load to every text query.

Extend or override a default tag by ID in YAML. Each tag accepts up to four labels. Bilingual defaults include Cats, Dogs, Birds, People, Food, Flowers, Mountains, Ocean, Snow, Sunset, Architecture and Street scenes. The worker averages the normalized label-prompt vectors for each tag and stores the top labels and cosine scores per photo. Scores indicate model similarity and are uncalibrated. Vocabulary and top-k changes refresh the tags from existing image embeddings.

```yaml
worker:
  memory_mb: 6144
ml:
  enabled: true
  offline: true
  tag_vocabulary:
    bicycle: [Bicycle, 自転車]
    cat: [Cat, 猫, 子猫]
```

## Offline deployment

**Docker Compose.** Set the worker budget and flags in the mounted YAML. Create the state and model directories with write access for the configured UID/GID. `PHOTOCULL_MODELS_DIR` is the host-side cache mount, mapped to `/models` in the worker only. The API image uses the base Python package. Set `PHOTOCULL_WORKER_MEM_LIMIT=8g` and adjust `PHOTOCULL_WORKER_CPUS` in Compose's `.env`. A one-off container can prepare the cache before starting the network-isolated worker. [Compose example](../docker-compose.yml)

```sh
docker compose build api worker
PHOTOCULL_WORKER_NETWORK_MODE=bridge docker compose run --rm --no-deps worker photocull fetch-model
docker compose up -d
```

**systemd.** Prepare the model cache as the service user outside the service's private network namespace. Make sure the cache location matches the worker environment. Apply the memory and CPU overrides in a worker unit drop-in, reload systemd, then restart both processes. [Worker unit](../deploy/systemd/photocull-worker.service)

```sh
sudo -u photocull env PHOTOCULL_CONFIG=/etc/photocull/photocull.yaml /opt/photocull/.venv/bin/photocull fetch-model
```

```ini
[Service]
MemoryHigh=6G
MemoryMax=8G
CPUQuota=600%
```

## Query and indexing behavior

- `POST /api/libraries/{library}/search` accepts a `query`, optional `tag` and optional `limit`. `POST /api/libraries/{library}/assets/{id}/similar` accepts the same options without the text query. The API writes a short-lived SQLite request and returns a request ID. Poll `GET /api/libraries/{library}/search/{id}` for pending, running, done or error. The queue allows up to 16 outstanding requests per library. Results expire after two minutes.
- The worker prioritizes requests between indexing steps. Text similarity ranks indexed photos. Image similarity excludes the selected photo and applies the minimum cosine threshold. Exact cosine ranking streams SQLite vectors in fixed-size blocks and retains the requested top results. The API assembles the resulting assets, checking their current content identities.
- `GET /api/libraries/{library}/similar-groups` lists groups by anchor photo. Pass `similar_group_id` to the asset list to page through its members. Grouping uses an approximate HNSW graph of anchors on cosine distance. Every member must meet the threshold against its anchor. Capture date has no effect. The worker builds groups in chunks after indexing, then publishes them atomically.
- Burst comparisons still use a capture-time window. Jobs add changed embedding times to the existing incremental burst queue. Photos without an embedding retain the dHash fallback. Best choices and rating edits continue to use SQLite review state.
- `GET /api/libraries/{library}/tags` lists labels. Pass a tag ID to the asset list or a search request. A tag filter in the UI applies to the selected search as well. Closing search results returns to the current browse selection.

## Validation

CI uses a deterministic fake encoder. To run the real CPU test, install the ML extra and use the default model cache or set `PHOTOCULL_MODEL_DIR`. The marked test uses four public sample images. Set `PHOTOCULL_TEST_FIXTURES` to a directory containing `astronaut.png`, `chelsea.png`, `coffee.png` and `rocket.jpg` to use local copies. Set `PHOTOCULL_ML_OFFLINE=1` when the models are already cached. [Sample image credits](https://scikit-image.org/docs/0.25.x/api/skimage.data.html)

```sh
uv run --locked --extra ml pytest -m model -s
uv run --locked --extra ml python scripts/benchmark_catalog.py --assets 50000 --requests 25 --ml
```

CT 205 had two visible Ryzen 5 5500U CPU cores and 4 GiB RAM. On 2026-11-26, the first verified cache download took 13.9 s and used 393.3 MiB for the three files. The cached model test took 12.5 s wall time. The ML child peaked at 841 MiB RSS. After warm-up it processed 32 previews in batches of four at 0.205 s per photo. At that rate, encoding 50,000 photos takes about 2.8 hours and 100,000 takes about 5.7 hours. These are preview-to-vector estimates. Initial decode, indexing, tag preparation, model warm-up and contention add time.

| Query | Top photo | Worker/API round trip on four photos |
| --- | --- | ---: |
| `a photo of a cat` | Chelsea the cat | 68 ms |
| `猫の写真` | Chelsea the cat | 68 ms |
| `a cup of coffee` | Coffee cup | 66 ms |
| `コーヒーの入ったカップ` | Coffee cup | 65 ms |

The text figures include enqueue, one worker step and result retrieval with a warm text encoder. Browser polling and larger catalogs add latency.

On the same host, a synthetic catalog with 50,000 768-D vectors and 10,000 planted five-photo clusters used 148.6 MiB for its SQLite file, including scores, job rows and both kinds of groups. Across 25 measured requests per case:

| Operation | p50 | p95 |
| --- | ---: | ---: |
| Worker exact vector ranking, top 60 | 230 ms | 239 ms |
| Similar-group list, 100 groups | 11.1 ms | 14.0 ms |
| Similar-group membership page | 3.7 ms | 3.9 ms |

The full approximate grouping pass took 67.9 s and recovered 9,995 of the 10,000 planted groups intact. Group build time and recall depend on photo diversity and the threshold. Ranking figures exclude text inference and the queue, and the synthetic cluster result does not measure accuracy on a personal library.
