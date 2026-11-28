# Automatic decision providers

Photocull defaults to local decisions. The Review toolbar's **Decision providers** dialog selects a provider for automatic tags and a separate provider for scores and auto-reject. A UI save creates a per-library override kept in the catalog. **Restore YAML defaults** removes that override. New still previews use the current selection. Existing results keep their provider until you queue reanalysis. Switching a decision back to Local cancels its pending OpenAI jobs. Requests already in progress can finish transmission.

- **Local tags** use the existing SigLIP vocabulary and cosine scores. Enable `ml.enabled` and `ml.tags_enabled` to use them. Local quality scores and auto-reject use the existing preview measurements and thresholds.
- **OpenAI compatible** uses Chat Completions with an image-capable model. It can assign IDs from the same tag vocabulary and estimate sharpness, exposure and clipping, with an optional technical reason for rejection. The displayed remote scores are model estimates. Local embeddings still power similarity and text search. Burst grouping and ranking still use the local metrics and image hashes.

## Configuration

Use the same configuration on API and worker. A minimal YAML example with defaults follows. The endpoint and model can also be supplied as `vlm.openai.base_url` and `vlm.openai.model` in a private deployment configuration.

```yaml
vlm:
  tags: local
  flags: local
  on_failure: unjudged
  openai:
    timeout_seconds: 20
    concurrency: 2
    requests_per_minute: 30
    max_requests_per_run: 100
    max_requests_per_day: 500
```

- `PHOTOCULL_VLM_BASE_URL` and `PHOTOCULL_VLM_MODEL` override those YAML connection settings. A server root, an API prefix ending in a version such as `/v1`, or the complete Chat Completions path works, with or without a trailing slash. A server with an unversioned completions route can use its complete path. Configure a vision model exposed by your server. This uses the Chat Completions format supported by vision models served through vLLM, Ollama and LM Studio.
- `PHOTOCULL_VLM_API_KEY` is read only from the API and worker process environments. Supply it through your service environment, for example systemd's `EnvironmentFile`, or through Compose's environment pass-through. A directly launched Photocull process needs the key exported in its environment. Leave it unset for a server that accepts unauthenticated requests. Keep endpoint values and credentials outside the repository. Use HTTPS for authenticated traffic across untrusted networks.
- **Test connection** sends a synthetic image through the API process to check that the configured model accepts image input and returns JSON. It displays success or failure and the masked endpoint. Both the API and worker need network access to the configured server. The default Compose worker network mode and systemd worker's private network must be adjusted in the deployment when enabling remote analysis.

## Privacy, caching and limits

Photos are sent only when remote analysis is selected. The worker reads the cached JPEG preview, reduces it to at most 768 pixels on the longest edge, and re-encodes the RGB pixels with no EXIF, GPS, ICC or other source metadata. Requests include the configured model, a classification prompt, and the derived image. Filenames, paths, catalog attributes and originals stay on the machine. Any information visible in the pixels is still visible to the configured server.

Successful validated results are cached in the writable state directory and reused across libraries. The cache key includes the content hash, endpoint, model, image recipe and decision prompt. Duplicate requests for the same content coordinate across processes. When both decisions are queued together, a single request can fill both entries. If one entry is already cached, only the missing decision is requested. Changing the model, endpoint, image recipe or relevant prompt can require an upload. Use a new model identifier when changing model weights behind a compatible server.

Limits count HTTP attempts, including bounded retries for temporary network errors, 429 and 5xx. Concurrency, request rate, and the UTC daily budget are shared by processes using the same state directory. The run budget applies to each API or worker process from startup, and the daily budget survives restarts. Cache hits consume no budget. A zero budget disables further outbound calls. The connection test consumes the same budget. Transport failures, invalid JSON, and exhausted budgets use `on_failure`: `local` tries the existing local decision, while `unjudged` records no decision. If local tagging is unavailable, a tag fallback stays unjudged.

## Reanalysis

In **Decision providers**, save the desired choices. Select tags, flags or both, then choose all results, unjudged photos, or results produced by a specific provider. You can restrict the request to an inclusive date range in the current library. The worker queues only active still images with a generated preview. Cached decisions remain eligible for reuse. Auto-reject provenance and automatic tag provenance are shown in Review and the browser. Manual tags, ratings, manual rejects, burst choices and dismissed auto-rejects remain in place.
