"""Stage 2: face detection, selection and alignment (embedding and liveness are added in Task 13)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, Union

import cv2
import numpy as np

from ..contracts import EngineError, Reason, ReasonCode, StageResult

# InsightFace 112x112 reference landmarks: left eye, right eye, nose, left mouth, right mouth (image order).
REFERENCE_112 = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)
DETECT_MAX_SIDE = 640


@dataclass(frozen=True, eq=False)
class FaceBox:
    score: float
    box: tuple[int, int, int, int]  # x, y, w, h in original pixels
    landmarks: np.ndarray  # (5, 2) float32 in original pixels

    @property
    def area(self) -> int:
        return self.box[2] * self.box[3]


class FaceDetector:
    def __init__(self, model_path: Path, score_threshold: float = 0.7) -> None:
        self._det = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), score_threshold, 0.3, 50)

    def detect(self, img_bgr: np.ndarray) -> list[FaceBox]:
        h, w = img_bgr.shape[:2]
        scale = min(1.0, DETECT_MAX_SIDE / max(h, w))
        small = img_bgr if scale == 1.0 else cv2.resize(img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        self._det.setInputSize((small.shape[1], small.shape[0]))
        _, faces = self._det.detect(small)
        if faces is None:
            return []
        out = []
        for f in faces:
            lm = (f[4:14].reshape(5, 2) / scale).astype(np.float32)
            x, y, bw, bh = (f[0:4] / scale)
            out.append(FaceBox(float(f[14]), (int(x), int(y), int(bw), int(bh)), lm))
        return out


def alignment_matrix(landmarks: np.ndarray) -> np.ndarray:
    m, _ = cv2.estimateAffinePartial2D(landmarks.astype(np.float32), REFERENCE_112, method=cv2.LMEDS)
    if m is None:
        raise EngineError("ALIGN_FAILED")
    return m


def align_112(img_bgr: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
    return cv2.warpAffine(img_bgr, alignment_matrix(landmarks), (112, 112), borderValue=(0, 0, 0))


# Selfie quality. A poor selfie gets a retake instead of a failed match. On AgeDB-30, genuine pairs with a blurry face
# (sharpness under the 1st percentile, 70) failed the match 44% of the time against 26%; dark faces 47% against 27%.
MIN_SELFIE_FACE_PX = 80               # shorter side of the face box; smaller faces are upscaled into the 112x112 crop
MAX_YAW = 0.4                         # nose offset from the eye midpoint, in eye widths (not yet measured on real selfies)
MIN_BRIGHTNESS, MAX_BRIGHTNESS = 50, 215  # mean grey of the aligned crop
MIN_SHARPNESS = 70.0                  # variance of the Laplacian of the aligned crop (AgeDB-30 1st percentile)


def selfie_quality(face: FaceBox, crop_bgr: np.ndarray) -> Optional[Reason]:
    """None for a usable selfie, otherwise a retryable IMAGE_QUALITY reason that says what to fix."""
    def retake(problem: str) -> Reason:
        return Reason(ReasonCode.IMAGE_QUALITY, True, f"Your selfie is {problem}. Please retake it.")

    if min(face.box[2], face.box[3]) < MIN_SELFIE_FACE_PX:
        return retake("taken from too far away: your face is too small, hold the phone closer")
    left_eye, right_eye, nose = (np.asarray(p, np.float64) for p in face.landmarks[:3])
    eye_width = float(np.linalg.norm(right_eye - left_eye))
    if eye_width <= 0 or abs(nose[0] - (left_eye[0] + right_eye[0]) / 2) / eye_width > MAX_YAW:
        return retake("turned away from the camera: face the camera directly")
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    brightness = float(gray.mean())
    if brightness < MIN_BRIGHTNESS:
        return retake("too dark: find more light")
    if brightness > MAX_BRIGHTNESS:
        return retake("too bright: avoid direct light behind or on your face")
    if cv2.Laplacian(gray, cv2.CV_64F).var() < MIN_SHARPNESS:
        return retake("too blurry: hold the phone still")
    return None


def pick_licence_face(faces: list[FaceBox]) -> Optional[FaceBox]:
    """The largest face: licences often carry a small ghost portrait as well."""
    return max(faces, key=lambda f: f.area) if faces else None


def pick_selfie_face(faces: list[FaceBox]) -> Union[FaceBox, Reason]:
    if not faces:
        return Reason.of(ReasonCode.FACE_NOT_FOUND_SELFIE)
    ranked = sorted(faces, key=lambda f: f.area, reverse=True)
    if len(ranked) > 1 and ranked[1].score >= 0.9 and ranked[1].area >= 0.25 * ranked[0].area:
        return Reason.of(ReasonCode.IMAGE_QUALITY)
    return ranked[0]


class _Thresholds(Protocol):
    face_match_threshold: float
    liveness_threshold: float


def embed(aligned_bgr: np.ndarray, session) -> np.ndarray:
    """128-d L2-normalised SFace embedding from a 112x112 aligned BGR face.

    Fed as OpenCV's FaceRecognizerSF does: RGB, 0-255, no mean or scale (a test checks it against that reference).
    """
    blob = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB).astype(np.float32).transpose(2, 0, 1)[None]
    out = np.asarray(session.run(None, {session.get_inputs()[0].name: blob})[0], dtype=np.float64).reshape(-1)
    norm = np.linalg.norm(out)
    if norm == 0 or not np.isfinite(norm):
        raise EngineError("EMBEDDING_FAILED")
    return out / norm


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def run_face_stage(
    selfie: np.ndarray,
    licence_front: np.ndarray,
    detector,
    embedder: Callable[[np.ndarray], np.ndarray],
    liveness,
    settings: _Thresholds,
) -> StageResult:
    result = StageResult(name="face", ran=True)
    t0 = time.perf_counter()
    selfie_face = pick_selfie_face(detector.detect(selfie))
    licence_face = pick_licence_face(detector.detect(licence_front))
    result.timings_ms["detect"] = round((time.perf_counter() - t0) * 1000, 1)

    selfie_crop = None
    if isinstance(selfie_face, Reason):
        result.reasons.append(selfie_face)
    elif (poor := selfie_quality(selfie_face, selfie_crop := align_112(selfie, selfie_face.landmarks))) is not None:
        result.reasons.append(poor)  # retake first: no point judging liveness or the match on a poor selfie
    else:
        t1 = time.perf_counter()
        score = float(liveness.score(selfie, selfie_face))
        result.timings_ms["liveness"] = round((time.perf_counter() - t1) * 1000, 1)
        result.details.update(liveness_score=round(score, 4), liveness_threshold=settings.liveness_threshold)
        if score < settings.liveness_threshold:
            result.reasons.append(Reason.of(ReasonCode.LIVENESS_FAILED))
    if licence_face is None:
        result.reasons.append(Reason.of(ReasonCode.FACE_NOT_FOUND_LICENCE))
    if result.reasons:
        return result

    t2 = time.perf_counter()
    licence_crop = align_112(licence_front, licence_face.landmarks)
    similarity = cosine(embedder(selfie_crop), embedder(licence_crop))
    result.timings_ms["embed_match"] = round((time.perf_counter() - t2) * 1000, 1)
    result.details.update(similarity=round(similarity, 4), threshold=settings.face_match_threshold)
    result.crops = {"selfie_face": selfie_crop, "licence_face": licence_crop}
    if similarity < settings.face_match_threshold:
        result.reasons.append(Reason.of(ReasonCode.FACE_MISMATCH))
    return result
