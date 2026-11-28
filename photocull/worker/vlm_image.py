from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from PIL import Image

from photocull.media.artifacts import open_artifact
from photocull.vlm import ProviderError


def image_data_url(preview: Path) -> str:
    try:
        with (
            open_artifact(preview) as (fd, _),
            os.fdopen(os.dup(fd), "rb") as handle,
            Image.open(handle, formats=("JPEG",)) as cached,
            cached.convert("RGB") as image,
            io.BytesIO() as output,
        ):
            image.thumbnail((768, 768), Image.Resampling.LANCZOS)
            image.info.clear()
            image.save(output, format="JPEG", quality=82)
            return "data:image/jpeg;base64," + base64.b64encode(output.getbuffer()).decode("ascii")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ProviderError("preview_unavailable") from None
