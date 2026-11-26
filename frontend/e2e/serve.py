"""Run the API and worker against small generated fixtures for browser smoke tests."""

from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from PIL import Image

from photocull.api import create_app
from photocull.config import Library, ScoringSettings, Settings
from photocull.worker import run


def write_fixtures(root: Path) -> Library:
    root.mkdir()
    gradient = Image.linear_gradient("L").rotate(90).resize((128, 96))
    for filename, stamp, lightness in (
        ("a.jpg", "05", 0.5),
        ("b.jpg", "06", 0.7),
        ("c.jpg", "06", 0.9),
        ("d.jpg", "15", 0.5),
        ("e.jpg", "16", 0.7),
    ):
        exif = Image.Exif()
        exif[34665] = {36867: f"2025:02:03 04:05:{stamp}"}
        levels = [round(px * lightness) for px in range(256)]
        gradient.point(levels).save(root / filename, exif=exif)
    Image.new("L", (128, 96), 255).save(root / "white.jpg")
    return Library(root=root)


def main() -> None:
    with TemporaryDirectory(prefix="photocull-playwright-") as directory:
        root = Path(directory)
        settings = Settings(
            libraries={name: write_fixtures(root / name) for name in ("desktop", "mobile")},
            state_dir=root / "state",
            static_dir=Path(__file__).resolve().parents[1] / "dist",
            scoring=ScoringSettings(min_sharpness=0),
        )
        context = get_context("fork")
        stop = context.Event()
        worker = context.Process(target=run, args=(settings, stop))
        worker.start()
        try:
            uvicorn.run(create_app(settings), host="127.0.0.1", port=18787, proxy_headers=False)
        finally:
            stop.set()
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join()
            worker.close()


if __name__ == "__main__":
    main()
