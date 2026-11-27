"""Run the API and worker against small generated fixtures for browser smoke tests."""

import subprocess
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from gallery_fixtures import seed_gallery
from PIL import Image

from photocull.api import create_app
from photocull.config import (
    ArchiveSettings,
    DerivativeSettings,
    InsightSettings,
    Library,
    MLSettings,
    ScoringSettings,
    Settings,
    WorkerSettings,
)
from photocull.db import connect, initialize, write
from photocull.worker import run
from tests.fake_embedding import FakeEmbedding
from tests.fake_faces import FakeFaces


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
    if root.name == "archive":
        gradient.resize((384, 256)).convert("RGB").save(root / "zz-review.tiff")
    if root.name == "insights":
        Image.new("RGB", (120, 90), (30, 50, 220)).save(root / "blue.jpg")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x96:r=12",
                "-t",
                "6",
                "-c:v",
                "mpeg4",
                str(root / "walk.mov"),
            ],
            check=True,
        )
    return Library(root=root)


def main() -> None:
    with TemporaryDirectory(prefix="photocull-playwright-") as directory:
        root = Path(directory)
        archive_output = root / "archive-output"
        archive_output.mkdir()
        galleries = {f"gallery-{year}": year for year in (2024, 2025)}
        for name in galleries:
            (root / name).mkdir()
        settings = Settings(
            libraries={
                **{
                    name: write_fixtures(root / name)
                    for name in (
                        "desktop",
                        "mobile",
                        "semantic",
                        "insights",
                        "archive",
                        "scan-errors",
                    )
                },
                **{name: Library(root=root / name) for name in galleries},
            },
            state_dir=root / "state",
            static_dir=Path(__file__).resolve().parents[1] / "dist",
            scoring=ScoringSettings(min_sharpness=0),
            derivatives=DerivativeSettings(prewarm=0, original_download=True),
            worker=WorkerSettings(memory_mb=3072),
            ml=MLSettings(enabled=True, tag_top_k=1, offline=True),
            archive=ArchiveSettings(
                write_roots=(root / "archive",),
                export_roots={"archive": archive_output},
            ),
            insights=InsightSettings(
                aesthetic=True,
                taste=True,
                faces=True,
                story=True,
                report=True,
                video=True,
                export=True,
                offline=True,
            ),
        )
        for name, year in galleries.items():
            seed_gallery(settings, name, year)
        index = settings.library_state("scan-errors") / "index.db"
        initialize(index)
        with connect(index) as db, write(db):
            db.execute("""
                INSERT INTO scan_requests(id,status,created_at,file_errors,directory_errors)
                VALUES(1,'done',0,50,1)
            """)
            db.executemany(
                """INSERT INTO scan_errors(scan_id,path,kind,operation,code,errno)
                   VALUES(1,?,'file','read','permission_denied',13)""",
                ((f"2025/写真-{index:02d}.NEF",) for index in range(50)),
            )
            db.execute("""
                INSERT INTO scan_errors(scan_id,path,kind,operation,code,errno)
                VALUES(1,'closed-folder','directory','list','io_error',5)
            """)
        context = get_context("fork")
        stop = context.Event()
        worker = context.Process(
            target=run,
            args=(settings, stop),
            kwargs={"model_factory": FakeEmbedding, "face_factory": FakeFaces},
        )
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
