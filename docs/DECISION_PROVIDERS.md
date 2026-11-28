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
- **Test connection** sends a synthetic image through the API process to check that the configured model accepts image input and returns JSON. It displays success or failure and the masked endpoint. In Compose the API keeps host networking so nginx can still reach it on `127.0.0.1:8000`. This probe connects directly from the API. The worker sends production requests over its own network path.

## Docker Compose worker egress

The default deployment uses `network_mode: none` for the worker and starts no egress proxy. The worker and API share SQLite, including the job queue, and the worker uses local mounts for media, derivatives, file watching and model caches. Local decisions work with the network disabled and do not depend on the proxy. If you use optional ML, provision models before starting the isolated worker and enable the appropriate offline settings. See the [ML cache instructions](ML.md#offline-deployment) and [Insights setup](INSIGHTS.md).

For remote decisions, use Linux Docker Engine 28 or newer and Docker Compose v2.24.4 or newer. Add the egress file after your other Compose overrides and enable its profile. Keep the endpoint, model and optional key in your private deployment environment as described above.

```sh
docker compose -f docker-compose.yml -f deploy/compose.egress.yml --profile egress up -d --build
```

The egress override replaces the worker's network mode with a single internal bridge shared with the egress proxy. The worker has no default route and the bridge has no host gateway address. Only the proxy joins an outbound bridge, and it publishes no ports. The proxy accepts only CONNECT to port 443 of exact DNS hostnames. It resolves and connects to checked public IPv4 addresses. The worker verifies the destination's TLS certificate inside the tunnel and sends the API key only after TLS succeeds. Redirects are never followed.

With the standard egress deployment, an unset or empty `PHOTOCULL_EGRESS_ALLOW` allows only `openrouter.ai`. The optional [Web settings overlay](SETTINGS.md#判定プロバイダーと-egress) supports a saved host list when the environment variable is unset or empty. To change the list, set the variable in your private Compose environment to comma-separated hostnames. Include the default hostname in the list if you still want to use it. Enter only hostnames, with each subdomain listed separately. Recreate the egress proxy with the same Compose files and profile after changing its environment.

Compose configures `HTTPS_PROXY`, `HTTP_PROXY` and loopback exclusions in `NO_PROXY` for the worker. The VLM client reads uppercase `HTTPS_PROXY` and `NO_PROXY`. It supports an HTTP proxy endpoint without credentials, using CONNECT for HTTPS. Ordinary HTTP requests sent to the egress proxy are rejected. Model downloads can use different destinations and redirects, so the default list does not enable them. See the cache instructions above for separate provisioning. The Compose profile does not change the example systemd units.

### Troubleshooting

- If **Test connection** fails, check the API environment, vision model, destination TLS and shared request budget. The status is based on the API's direct route.
- If the probe succeeds but remote jobs fail, check the worker and proxy logs with the same Compose files and profile. Proxy logs give the approved hostname and port or a rejection reason. They omit headers, request bodies and raw request targets. A `host` or `authority` rejection points to the exact hostname, scheme or port configuration. A `dns`, `non_public_address` or `upstream` entry points to DNS resolution, a blocked address or connection failure. With no proxy log entry, check that the proxy is running, that the worker was recreated with the override, and that the job made a network attempt. A cache hit or exhausted budget may prevent an attempt. Remote failures use the configured `on_failure` policy.
- To return to the closed deployment, select Local for the decisions you want processed locally, remove the egress service with `docker compose -f docker-compose.yml -f deploy/compose.egress.yml --profile egress rm --stop --force egress-proxy`, and recreate the worker with your normal Compose files and `PHOTOCULL_WORKER_NETWORK_MODE` unset or set to `none`.

## Privacy, caching and limits

Photos are sent only when remote analysis is selected. The worker reads the cached JPEG preview, reduces it to at most 768 pixels on the longest edge, and re-encodes the RGB pixels with no EXIF, GPS, ICC or other source metadata. Requests include the configured model, a classification prompt, and the derived image. Filenames, paths, catalog attributes and originals stay on the machine. Any information visible in the pixels is still visible to the configured server.

Successful validated results are cached in the writable state directory and reused across libraries. The cache key includes the content hash, endpoint, model, image recipe and decision prompt. Duplicate requests for the same content coordinate across processes. When both decisions are queued together, a single request can fill both entries. If one entry is already cached, only the missing decision is requested. Changing the model, endpoint, image recipe or relevant prompt can require an upload. Use a new model identifier when changing model weights behind a compatible server.

Limits count HTTP attempts, including bounded retries for temporary network errors, 429 and 5xx. Concurrency, request rate, and the UTC daily budget are shared by processes using the same state directory. The run budget applies to each API or worker process from startup, and the daily budget survives restarts. Cache hits consume no budget. A zero budget disables further outbound calls. The connection test consumes the same budget. Transport failures, invalid JSON, and exhausted budgets use `on_failure`: `local` tries the existing local decision, while `unjudged` records no decision. If local tagging is unavailable, a tag fallback stays unjudged.

## Reanalysis

In **Decision providers**, save the desired choices. Select tags, flags or both, then choose all results, unjudged photos, or results produced by a specific provider. You can restrict the request to an inclusive date range in the current library. The worker queues only active still images with a generated preview. Cached decisions remain eligible for reuse. Auto-reject provenance and automatic tag provenance are shown in Review and the browser. Manual tags, ratings, manual rejects, burst choices and dismissed auto-rejects remain in place.
