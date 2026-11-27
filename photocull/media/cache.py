from pathlib import Path


def cache_path(state: Path, variant: str, asset_id: int, content_hash: str) -> Path:
    return state / variant / f"{asset_id}-{content_hash}.jpg"


def video_path(state: Path, asset_id: int, digest: str) -> Path:
    return state / "video" / f"{asset_id}-{digest}-proxy-v1.mp4"
