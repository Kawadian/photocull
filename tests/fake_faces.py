from PIL import Image


class FakeFaces:
    """Two subjects with deterministic boxes and one SFace-sized axis each."""

    def detect(self, path):
        with Image.open(path) as image, image.resize((1, 1)).convert("RGB") as sample:
            red, _, blue = sample.getpixel((0, 0))
        if red == blue == 255:
            return []
        vector = bytearray(256)
        offset = 0 if red >= blue else 2
        vector[offset : offset + 2] = b"\x00\x3c"  # one in little-endian float16
        return [((0.2, 0.2, 0.6, 0.8), 0.98, 0.91, bytes(vector))]
