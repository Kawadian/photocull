"""Still-image codec entry point. Runs inside the bounded subprocess."""

from __future__ import annotations

import hashlib
import json
import sys

from PIL import Image

from photocull.media.images import register_heif


def description(image):
    image.load()
    return {
        "size": list(image.size),
        "alpha": "A" in image.getbands() or "transparency" in image.info,
        "orientation": image.info.get("original_orientation") or image.getexif().get(274, 1),
    }


def main():
    source, *args = sys.argv[1:]
    register_heif()
    Image.MAX_IMAGE_PIXELS = 100_000_000
    with Image.open(source) as image:
        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("Image exceeds the 100 megapixel conversion limit")
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("Image has multiple frames or pages")
        if not args:
            print(json.dumps(description(image)))
            return
        if args == ["fingerprint"]:
            info = description(image)
            if image.info.get("bit_depth", 8) > 8:
                raise ValueError("Cannot verify high-bit-depth pixels with this decoder")
            info.update(
                mode=image.mode,
                pixels=hashlib.sha256(image.tobytes()).hexdigest(),
                icc=hashlib.sha256(image.info.get("icc_profile") or b"").hexdigest(),
                color=image.info.get("nclx_profile"),
            )
            print(json.dumps(info, sort_keys=True))
            return
        output, codec, quality, threads, preset, location_policy = args
        info = description(image)
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
            raise ValueError(f"Unsupported image mode for verified conversion: {image.mode}")
        if image.info.get("bit_depth", 8) > 8:
            raise ValueError("High-bit-depth HEIF requires a separate color-managed workflow")
        if info["alpha"] and codec == "jpeg":
            raise ValueError("JPEG cannot preserve the alpha channel")
        detached = image.convert("RGBA" if info["alpha"] else "RGB")
        metadata = image.info
        if (
            not metadata.get("icc_profile")
            and metadata.get("chromaticity")
            and "srgb" not in metadata
        ):
            raise ValueError("PNG chromaticities need a portable ICC profile")
        exif = Image.Exif()
        if info["orientation"] != 1:
            exif[274] = info["orientation"]
        options = {"quality": int(quality)}
        tags = ("icc_profile", "xmp") if location_policy == "include" else ("icc_profile",)
        for tag in tags:
            if metadata.get(tag):
                options[tag] = metadata[tag]
        if exif:
            options["exif"] = exif.tobytes()
        if codec == "jpeg":
            options.update(subsampling=0, optimize=preset != "fast")
        elif codec == "avif":
            options.update(
                max_threads=int(threads), speed={"fast": 8, "balanced": 6, "slow": 3}[preset]
            )
        else:
            profile = metadata.get("nclx_profile")
            if profile:
                options["nclx_profile"] = profile
            options.update(
                enc_params={
                    "preset": {"fast": "fast", "balanced": "medium", "slow": "slow"}[preset],
                    "x265:pools": "none",
                    "x265:frame-threads": threads,
                }
            )
        with detached:
            detached.info.clear()
            detached.save(
                output, format={"jpeg": "JPEG", "heic": "HEIF", "avif": "AVIF"}[codec], **options
            )


if __name__ == "__main__":
    main()
