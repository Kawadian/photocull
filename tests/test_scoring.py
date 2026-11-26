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
