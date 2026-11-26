from PIL import Image


class FakeEmbedding:
    """Dominant dark/red versus light/blue, with a third axis for unrelated tags."""

    def __init__(self, settings, cache):
        pass

    def image_vectors(self, paths):
        vectors = []
        for path in paths:
            with Image.open(path) as image, image.resize((1, 1)).convert("RGB") as tiny:
                red, green, blue = tiny.getpixel((0, 0))
            vectors.append((1, 0, 0) if red > blue or red == blue < 140 else (0, 1, 0))
        return vectors, [None] * len(paths)

    def text_vectors(self, texts):
        values = []
        for text in texts:
            if (
                "cat" in text.lower()
                or "猫" in text
                or any(
                    phrase in text for phrase in ("aesthetically pleasing", "polished professional")
                )
            ):
                values.append((1, 0, 0))
            elif (
                "dog" in text.lower()
                or "犬" in text
                or any(phrase in text for phrase in ("poorly composed", "unattractive snapshot"))
            ):
                values.append((0, 1, 0))
            else:
                values.append((0, 0, 1))
        return values

    def release_text(self):
        pass
