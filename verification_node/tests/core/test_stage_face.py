import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import backend.core  # noqa: F401  (before cv2: the image-size clamp)
import cv2  # noqa: E402
from backend.core.contracts import EngineError, ReasonCode
from backend.core.liveness import DevSkipLiveness, OnnxLiveness
from backend.core.registry import ModelRegistry
from backend.core.stages.face import REFERENCE_112, FaceBox, cosine, embed, run_face_stage, selfie_quality

SETTINGS = SimpleNamespace(face_match_threshold=0.40, liveness_threshold=0.5)
IMG = np.random.default_rng(3).integers(60, 190, (300, 300, 3), dtype=np.uint8)


def face(score=0.99, size=100):
    return FaceBox(score, (50, 50, size, size), REFERENCE_112 * 2.5)


class Detector:
    def __init__(self, selfie_faces, licence_faces):
        self._queue = [selfie_faces, licence_faces]

    def detect(self, img):
        return self._queue.pop(0)


class Live:
    def __init__(self, s):
        self.s = s

    def score(self, img, f):
        return self.s


def vec(i):
    v = np.zeros(512)
    v[i] = 1.0
    return v


def test_cosine_identical_is_one_and_orthogonal_zero():
    assert cosine(vec(1), vec(1)) == pytest.approx(1.0)
    assert cosine(vec(1), vec(2)) == pytest.approx(0.0)


def test_liveness_fail_short_circuits_match():
    calls = []
    r = run_face_stage(IMG, IMG, Detector([face()], [face()]), lambda c: calls.append(1) or vec(0), Live(0.1), SETTINGS)
    assert [x.code for x in r.reasons] == [ReasonCode.LIVENESS_FAILED] and r.reasons[0].retryable
    assert calls == []


def test_no_faces_reported_together():
    r = run_face_stage(IMG, IMG, Detector([], []), lambda c: vec(0), Live(1.0), SETTINGS)
    assert {x.code for x in r.reasons} == {ReasonCode.FACE_NOT_FOUND_SELFIE, ReasonCode.FACE_NOT_FOUND_LICENCE}


def test_mismatch():
    vecs = iter([vec(1), vec(2)])
    r = run_face_stage(IMG, IMG, Detector([face()], [face()]), lambda c: next(vecs), Live(0.9), SETTINGS)
    assert [x.code for x in r.reasons] == [ReasonCode.FACE_MISMATCH] and not r.reasons[0].retryable
    assert r.details["similarity"] == pytest.approx(0.0)


def test_match_and_crops():
    r = run_face_stage(IMG, IMG, Detector([face()], [face()]), lambda c: vec(3), Live(0.9), SETTINGS)
    assert r.reasons == [] and r.details["similarity"] == pytest.approx(1.0)
    assert set(r.crops) == {"selfie_face", "licence_face"} and r.crops["selfie_face"].shape == (112, 112, 3)


def crop_of(value=None):
    """A 112x112 crop: textured mid-grey, or flat at `value`."""
    if value is None:
        return np.random.default_rng(4).integers(60, 190, (112, 112, 3), dtype=np.uint8)
    return np.full((112, 112, 3), value, np.uint8)


def test_a_clear_frontal_selfie_passes_the_quality_check():
    assert selfie_quality(face(), crop_of()) is None


@pytest.mark.parametrize("bad_face, crop, word", [
    (face(size=60), crop_of(), "small"),                                   # face under 80 px
    (FaceBox(0.99, (50, 50, 100, 100), (REFERENCE_112 + [[0, 0], [0, 0], [30, 0], [0, 0], [0, 0]]) * 2.5),
     crop_of(), "turned"),                                                 # nose far off the eye midpoint
    (face(), crop_of(20), "dark"),
    (face(), crop_of(240), "bright"),
    (face(), cv2.GaussianBlur(crop_of(), (0, 0), 6), "blurry"),
])
def test_a_poor_selfie_asks_for_a_retake(bad_face, crop, word):
    r = selfie_quality(bad_face, crop)
    assert r.code == ReasonCode.IMAGE_QUALITY and r.retryable and word in r.message.lower()


def test_a_poor_selfie_stops_before_liveness_and_match():
    calls = []

    class CountingLive(Live):
        def score(self, img, f):
            calls.append("liveness")
            return 1.0

    dark = np.full((300, 300, 3), 15, np.uint8)
    r = run_face_stage(dark, IMG, Detector([face()], [face()]), lambda c: calls.append("embed") or vec(0), CountingLive(1), SETTINGS)
    assert [x.code for x in r.reasons] == [ReasonCode.IMAGE_QUALITY] and "dark" in r.reasons[0].message.lower()
    assert calls == []


def test_dev_skip_liveness_scores_one():
    assert DevSkipLiveness().score(IMG, face()) == 1.0


class FakeSession:
    def __init__(self, logits):
        self.logits, self.seen = logits, None

    def get_inputs(self):
        return [SimpleNamespace(name="x")]

    def run(self, _, feed):
        self.seen = feed["x"]
        return [np.array([self.logits])]


def test_onnx_liveness_softmax_and_input_shape():
    s = FakeSession([0.0, 2.0, 0.0])
    p = OnnxLiveness(s, crop_scale=2.0, input_size=80, live_index=1).score(IMG, face())
    assert 0.78 < p < 0.79 and s.seen.shape == (1, 3, 80, 80) and s.seen.dtype == np.float32


def _minivision_box(src_w, src_h, bbox, scale):
    """Copy of CropImage._get_new_box from minivision-ai/Silent-Face-Anti-Spoofing (src/generate_patches.py, Apache-2.0)."""
    x, y, box_w, box_h = bbox
    scale = min((src_h - 1) / box_h, min((src_w - 1) / box_w, scale))
    new_w, new_h = box_w * scale, box_h * scale
    cx, cy = box_w / 2 + x, box_h / 2 + y
    l, t, r, b = cx - new_w / 2, cy - new_h / 2, cx + new_w / 2, cy + new_h / 2
    if l < 0:
        r -= l
        l = 0
    if t < 0:
        b -= t
        t = 0
    if r > src_w - 1:
        l -= r - src_w + 1
        r = src_w - 1
    if b > src_h - 1:
        t -= b - src_h + 1
        b = src_h - 1
    return int(l), int(t), int(r), int(b)


@pytest.mark.parametrize("box", [(100, 80, 60, 70), (0, 0, 120, 120), (250, 260, 45, 38), (10, 5, 290, 290)])
def test_onnx_liveness_crop_matches_minivision(box):
    # Same crop the model was trained on: scale shrinks to fit the image and the box shifts inside, never padded.
    img = np.random.default_rng(2).integers(0, 255, (300, 320, 3), dtype=np.uint8)
    s = FakeSession([0.0, 1.0, 0.0])
    OnnxLiveness(s, crop_scale=2.7, input_size=80, live_index=1).score(img, FaceBox(0.99, box, REFERENCE_112))
    l, t, r, b = _minivision_box(320, 300, box, 2.7)
    want = cv2.resize(img[t:b + 1, l:r + 1], (80, 80)).astype(np.float32).transpose(2, 0, 1)[None]
    assert np.array_equal(s.seen, want)


def test_onnx_liveness_face_near_border_does_not_crash():
    s = FakeSession([1.0, 1.0])
    edge = FaceBox(0.99, (0, 0, 120, 120), REFERENCE_112)
    assert OnnxLiveness(s, 4.0, 80, 0).score(IMG, edge) == pytest.approx(0.5)


@pytest.mark.models
def test_embed_shape_and_unit_norm():
    reg = ModelRegistry(Path("backend/models"), Path("backend/models/models.lock"), None)
    reg.load()
    out = embed(np.random.default_rng(0).integers(0, 255, (112, 112, 3), dtype=np.uint8), reg.session("face_embedder"))
    assert out.shape == (128,) and abs(np.linalg.norm(out) - 1.0) < 1e-4


@pytest.mark.models
def test_embed_matches_opencv_reference_sface():
    # OpenCV's own FaceRecognizerSF is the reference for SFace's input (colour order, scale). A strongly coloured
    # random crop makes a wrong channel order show up as a clearly lower similarity.
    import cv2

    from backend.core.registry import _FILES

    reg = ModelRegistry(Path("backend/models"), Path("backend/models/models.lock"), None)
    reg.load()
    crop = np.random.default_rng(1).integers(0, 255, (112, 112, 3), dtype=np.uint8)
    crop[:, :, 0] //= 4  # very little blue
    ref = cv2.FaceRecognizerSF.create(str(Path("backend/models") / _FILES["face_embedder"]), "").feature(crop).reshape(-1)
    ref = ref / np.linalg.norm(ref)
    ours = embed(crop, reg.session("face_embedder"))
    assert float(ours @ ref) > 0.9999


def test_embed_zero_vector_guard():
    class Zero(FakeSession):
        def run(self, _, feed):
            return [np.zeros((1, 512))]

    with pytest.raises(EngineError):
        embed(np.zeros((112, 112, 3), np.uint8), Zero([0]))


@pytest.mark.samples
def test_same_person_pairs_listed_in_pairs_csv_are_informational():
    csv = Path(os.environ.get("KOMUTE_SAMPLES_DIR", ""), "pairs.csv")
    if not csv.exists():
        pytest.skip("no pairs.csv yet: owner selfies not supplied")
