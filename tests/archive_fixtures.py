from __future__ import annotations

import struct
import subprocess


def synthetic_dng(path, width=384, height=256):
    """Single-IFD uncompressed Bayer DNG with deterministic sensor samples."""
    from array import array
    from sys import byteorder

    def shorts(*values):
        return 3, len(values), struct.pack(f"<{len(values)}H", *values)

    def longs(*values):
        return 4, len(values), struct.pack(f"<{len(values)}I", *values)

    def byte(*values):
        return 1, len(values), bytes(values)

    def ascii_(value):
        encoded = value.encode() + b"\0"
        return 2, len(encoded), encoded

    def rationals(*values):
        return 10, len(values), b"".join(struct.pack("<ii", value, 1000) for value in values)

    bits = {
        254: longs(0),
        256: longs(width),
        257: longs(height),
        258: shorts(16),
        259: shorts(1),
        262: shorts(32803),
        271: ascii_("Photocull"),
        272: ascii_("Synthetic"),
        273: longs(0),
        274: shorts(1),
        277: shorts(1),
        278: longs(height),
        279: longs(width * height * 2),
        284: shorts(1),
        306: ascii_("2021:06:01 10:11:12"),
        33421: shorts(2, 2),
        33422: byte(0, 1, 1, 2),
        50706: byte(1, 4, 0, 0),
        50707: byte(1, 3, 0, 0),
        50708: ascii_("Photocull Synthetic"),
        50710: byte(0, 1, 2),
        50711: shorts(1),
        50713: shorts(1, 1),
        50714: shorts(0),
        50717: longs(4095),
        50719: longs(0, 0),
        50720: longs(width, height),
        50721: rationals(670, 180, 150, 100, 760, 140, 70, 110, 820),
        50728: rationals(800, 1000, 680),
        50778: shorts(21),
        50829: longs(0, 0, height, width),
    }
    offset = 8 + 2 + len(bits) * 12 + 4
    bits[273] = longs(offset + sum(len(b) for _, _, b in bits.values() if len(b) > 4))
    directory, overflow = [], bytearray()
    for tag, (kind, count, data) in sorted(bits.items()):
        value = data.ljust(4, b"\0")
        if len(data) > 4:
            value = struct.pack("<I", offset + len(overflow))
            overflow.extend(data)
        directory.append(struct.pack("<HHI", tag, kind, count) + value)
    pixels = array(
        "H", ((x + y * 3 + (x % 2) * 31) % 4095 for y in range(height) for x in range(width))
    )
    if byteorder != "little":
        pixels.byteswap()
    with path.open("wb") as file:
        file.write(b"II" + struct.pack("<HIH", 42, 8, len(bits)))
        file.write(b"".join(directory) + b"\0" * 4 + overflow)
        pixels.tofile(file)


def synthetic_image(path, size=(384, 256)):
    from PIL import Image, ImageCms, ImageDraw

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    ramp = Image.linear_gradient("L")
    red = ramp.rotate(90).resize(size)
    blue = ramp.resize(size)
    green = red.point(lambda pixel: 70 + pixel // 2)
    with Image.merge("RGB", (red, green, blue)) as image:
        draw = ImageDraw.Draw(image)
        w, h = size
        draw.ellipse(
            (w // 7, h // 8, w * 6 // 7, h * 7 // 8), outline=(250, 165, 60), width=max(3, w // 200)
        )
        draw.polygon(
            ((w // 3, h * 3 // 4), (w // 2, h // 5), (w * 4 // 5, h * 2 // 3)), fill=(15, 18, 52)
        )
        exif = image.getexif()
        exif[271] = "Photocull"
        exif[272] = "Synthetic"
        exif[274] = 6
        exif[36867] = "2020:04:20 12:34:45"
        image.save(path, icc_profile=profile, exif=exif)
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            "-GPSLatitude=35.25",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=139.25",
            "-GPSLongitudeRef=E",
            "-XMP-dc:Title=Synthetic",
            path,
        ],
        check=True,
        capture_output=True,
    )


def synthetic_video(path, size="384x256", duration=2):
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-v",
            "error",
            "-filter_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=523:sample_rate=48000",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-crf",
            "12",
            "-c:a",
            "aac",
            "-metadata",
            "creation_time=2021-06-01T12:34:56Z",
            "-metadata",
            "location=+35.2500+139.2500/",
            "-metadata",
            "title=Synthetic",
            "-metadata:s:a:0",
            "language=jpn",
            path,
        ],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["exiftool", "-overwrite_original", "-Rotation=90", path],
        check=True,
        capture_output=True,
    )
