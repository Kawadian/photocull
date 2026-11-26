from pathlib import Path


def cache_path(state: Path, variant: str, asset_id: int, content_hash: str) -> Path:
    return state / variant / f"{asset_id}-{content_hash}.jpg"
