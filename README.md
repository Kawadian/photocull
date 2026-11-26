# Photocull

A local photo and video culler with a lightweight web API and a separate indexing worker. See the [project specification](docs/SPEC.md).

Photocull targets Python 3.12. Install the API and CLI with `uv sync` or `pip install .`. Install the Phase 1 media decoders with `uv sync --extra worker` or `pip install '.[worker]'`. The reserved `ml` extra is empty in Phase 1.

## 日本語

Photocull は、写真と動画をローカル環境で選別するためのツールです。Web API とインデックス作成ワーカーは別プロセスで動作します。
