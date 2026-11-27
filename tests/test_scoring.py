from dataclasses import asdict

import pytest
from PIL import Image, ImageDraw, ImageFilter

from photocull.worker.scoring import score_preview


def test_preview_scores_sharpness_and_clipping(tmp_path):
    sharp = Image.new("L", (192, 128), 45)
    draw = ImageDraw.Draw(sharp)
    for x in range(4, 192, 12):
        draw.rectangle((x, 0, x + 5, 127), fill=210)
    sharp.save(tmp_path / "sharp.jpg")
    sharp.filter(ImageFilter.GaussianBlur(4)).save(tmp_path / "blur.jpg")
    white = Image.new("L", sharp.size, 255)
    white.save(tmp_path / "white.jpg")
    Image.new("L", sharp.size, 0).save(tmp_path / "black.jpg")

    sharp_score = score_preview(tmp_path / "sharp.jpg")
    blur_score = score_preview(tmp_path / "blur.jpg")
    assert 0 <= blur_score.sharpness < sharp_score.sharpness <= 1
    assert abs(blur_score.exposure - sharp_score.exposure) < 0.02
    assert sharp_score.clipped_highlights == sharp_score.clipped_shadows == 0
    assert len(sharp_score.dhash) == 16
    assert score_preview(tmp_path / "white.jpg").exposure == 1
    assert score_preview(tmp_path / "white.jpg").clipped_highlights == 1
    assert score_preview(tmp_path / "black.jpg").exposure == 0
    assert score_preview(tmp_path / "black.jpg").clipped_shadows == 1


def test_dhash_distinguishes_opposing_gradients(tmp_path):
    gradient = Image.linear_gradient("L").rotate(90)
    gradient.save(tmp_path / "gradient.png")
    gradient.resize((160, 160)).point(lambda value: (value + 30) // 2).save(
        tmp_path / "similar.png"
    )
    gradient.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(tmp_path / "opposite.png")
    original = int(score_preview(tmp_path / "gradient.png").dhash, 16)
    similar = int(score_preview(tmp_path / "similar.png").dhash, 16)
    opposite = int(score_preview(tmp_path / "opposite.png").dhash, 16)
    assert (original ^ similar).bit_count() == 0
    assert (original ^ opposite).bit_count() == 64


@pytest.mark.parametrize(
    ("background", "patch", "size", "reason"),
    [
        (255, (40, 180), 12, None),
        (0, (20, 160), 12, None),
        (0, (12, 64), 16, None),
        (0, (25, 25), 16, "underexposed"),
        (255, (255, 255), 12, "clipped_highlights"),
        (0, (0, 0), 12, "clipped_shadows"),
        (5, (25, 25), 12, "blur"),
        (127, (127, 127), 12, "blur"),
    ],
)
def test_reject_requires_low_detail_in_exposure_extremes(tmp_path, background, patch, size, reason):
    import sqlite3
    from contextlib import closing

    from photocull.config import ScoringSettings
    from photocull.worker.rejections import REJECT_REASON_SQL

    image = Image.new("L", (100, 100), background)
    for y in range(size):
        for x in range(size):
            image.putpixel((10 + x, 10 + y), patch[(x + y) % 2])
    preview = tmp_path / "synthetic.png"
    image.save(preview)
    scores = score_preview(preview)
    if patch[0] != patch[1]:
        assert scores.detail > ScoringSettings().min_detail_when_clipped
    else:
        assert scores.detail == 0
    if background == 255:
        assert scores.bright_p99 == 1
    elif background == 0:
        assert scores.dark_p01 == 0
    with closing(sqlite3.connect(":memory:")) as db:
        (actual,) = db.execute(
            f"""SELECT {REJECT_REASON_SQL} FROM (
                SELECT :sharpness AS sharpness, :exposure AS exposure, :detail AS detail,
                :clipped_highlights AS clipped_highlights, :clipped_shadows AS clipped_shadows)""",
            asdict(scores) | ScoringSettings().model_dump(),
        ).fetchone()
    assert actual == reason
