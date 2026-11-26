from threading import Lock

import pillow_heif

HEIF_LOCK = Lock()
heif_registered = False


def register_heif() -> None:
    global heif_registered
    with HEIF_LOCK:
        if not heif_registered:
            pillow_heif.register_heif_opener(decode_threads=1)
            heif_registered = True
