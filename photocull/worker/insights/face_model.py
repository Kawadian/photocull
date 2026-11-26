from __future__ import annotations

from pathlib import Path


class FaceModel:
    def __init__(self, cache: Path, revision: str, offline: bool, threads: int):
        import cv2

        from photocull.worker.insights.model_cache import face_files

        detector, recognizer = face_files(cache, revision, offline)
        cv2.setNumThreads(threads)
        self.detector = cv2.FaceDetectorYN.create(str(detector), "", (320, 320), 0.85, 0.3, 100)
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer), "")
        self.cv = cv2

    def detect(self, path: Path) -> list:
        import numpy as np
        from PIL import Image

        from photocull.worker.ml.vectors import STORAGE_DTYPE, normalized

        with Image.open(path) as source:
            source.thumbnail((960, 960))
            image = self.cv.cvtColor(np.asarray(source.convert("RGB")), self.cv.COLOR_RGB2BGR)
        height, width = image.shape[:2]
        # YuNet's internal strides are 32 px. Keep the real bounds for normalization.
        padded = self.cv.copyMakeBorder(
            image, 0, -height % 32, 0, -width % 32, self.cv.BORDER_CONSTANT
        )
        self.detector.setInputSize((padded.shape[1], padded.shape[0]))
        _, detections = self.detector.detect(padded)
        faces = []
        for detection in detections if detections is not None else ():
            x, y, w, h = (float(value) for value in detection[:4])
            left, top = max(0, int(x)), max(0, int(y))
            right, bottom = min(width, int(x + w)), min(height, int(y + h))
            if min(right - left, bottom - top) < 12:
                continue
            aligned = self.recognizer.alignCrop(image, detection)
            vector = normalized(self.recognizer.feature(aligned).reshape(-1))
            gray = self.cv.cvtColor(aligned, self.cv.COLOR_BGR2GRAY)
            blur = float(self.cv.Laplacian(gray, self.cv.CV_32F).var())
            confidence = float(detection[-1])
            quality = confidence * min(1, blur / 120) * min(1, min(w, h) / 80)
            faces.append(
                (
                    (left / width, top / height, right / width, bottom / height),
                    confidence,
                    quality,
                    vector.astype(STORAGE_DTYPE).tobytes(),
                )
            )
        return sorted(faces, key=lambda face: (face[0][0], face[0][1]))
