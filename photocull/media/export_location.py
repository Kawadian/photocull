"""Supported containers for verified, metadata-only export copies."""

RAW = frozenset({".dng", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".orf", ".rw2", ".pef"})
STILLS = frozenset({".jpg", ".jpeg", ".heic", ".heif", ".hif", ".tif", ".tiff"})
VIDEO_CONTAINERS = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska"}
MEDIA_SUFFIXES = tuple(sorted(RAW | STILLS | VIDEO_CONTAINERS.keys()))


class LocationSkip(ValueError):
    """An item that cannot be exported with its requested privacy policy."""


def unsupported_reason(suffix: str) -> str:
    return f"Metadata-only location removal is unavailable for {suffix.lower() or 'this format'}"
