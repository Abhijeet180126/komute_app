"""Single-photo passive liveness: MiniFASNetV2 (minivision-ai/Silent-Face-Anti-Spoofing, Apache-2.0), fetched by
scripts/fetch_models.py and enabled with KOMUTE_LIVENESS_MODEL."""
from __future__ import annotations

from typing import Protocol

import cv2
import numpy as np

from .stages.face import FaceBox


class LivenessModel(Protocol):
    def score(self, img_bgr: np.ndarray, face: FaceBox) -> float:
        """Probability in [0, 1] that the face is a live person."""


class DevSkipLiveness:
    """Dev only. Settings refuse it in production; every response carries advisory LIVENESS_SKIPPED_DEV."""

    def score(self, img_bgr: np.ndarray, face: FaceBox) -> float:
        return 1.0


class OnnxLiveness:
    """Passive anti-spoof classifier (MiniFASNetV2): crop around the face as minivision does, resize, softmax.

    MiniFASNetV2 takes an 80x80 BGR crop at 2.7x the face box, raw 0-255 values; class 1 is a live face.
    """

    def __init__(self, session, crop_scale: float, input_size: int, live_index: int) -> None:
        self._session = session
        self._input = session.get_inputs()[0].name
        self._scale = crop_scale
        self._size = input_size
        self._live = live_index

    def _crop(self, img: np.ndarray, face: FaceBox) -> np.ndarray:
        # minivision's CropImage._get_new_box, the crop the model was trained on: the scale shrinks so the box fits
        # the image, and a box past an edge is shifted back inside (never padded).
        src_h, src_w = img.shape[:2]
        x, y, w, h = face.box
        scale = min((src_h - 1) / h, (src_w - 1) / w, self._scale)
        cx, cy = x + w / 2, y + h / 2
        l, t, r, b = cx - w * scale / 2, cy - h * scale / 2, cx + w * scale / 2, cy + h * scale / 2
        if l < 0:
            r, l = r - l, 0
        if t < 0:
            b, t = b - t, 0
        if r > src_w - 1:
            l, r = l - (r - src_w + 1), src_w - 1
        if b > src_h - 1:
            t, b = t - (b - src_h + 1), src_h - 1
        l, t, r, b = int(l), int(t), int(r), int(b)
        return cv2.resize(img[t:b + 1, l:r + 1], (self._size, self._size))

    def score(self, img_bgr: np.ndarray, face: FaceBox) -> float:
        blob = self._crop(img_bgr, face).astype(np.float32).transpose(2, 0, 1)[None]
        logits = np.asarray(self._session.run(None, {self._input: blob})[0], dtype=np.float64).reshape(-1)
        e = np.exp(logits - logits.max())
        return float((e / e.sum())[self._live])
