import logging
import tempfile
from datetime import date, datetime, timezone

import numpy as np
import pytest

import backend.core  # noqa: F401
import cv2

from backend.core.aamva import AamvaRecord
from backend.core.config import Settings
from backend.core.contracts import (
    Advisory,
    Claimed,
    EngineError,
    InputError,
    Reason,
    ReasonCode,
    StageResult,
)
from backend.core.imaging import IMAGE_KEYS
from backend.core.pipeline import Engine, Stages, VerifyInput

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
SECRET_NAME, SECRET_LICENCE, SECRET_PLATE = "Zelda Quuxington", "Q99887766554433", "ZQX987"


@pytest.fixture
def settings():
    return Settings(_env_file=None, api_key="k", dev_skip_liveness=True)


def jpeg(seed=0, size=64):
    img = np.random.default_rng(seed).integers(0, 255, (size, size, 3), dtype=np.uint8)
    return cv2.imencode(".jpg", img)[1].tobytes()


def claimed():
    return Claimed(SECRET_NAME, date(1990, 1, 1), SECRET_LICENCE, "ON", SECRET_PLATE, "Red")


def inp(request_id="r1", images=None):
    return VerifyInput(request_id, "d1", NOW, "v1", claimed(), images or {k: jpeg(i) for i, k in enumerate(IMAGE_KEYS)})


class Fakes:
    def __init__(self, barcode_reasons=(), face_reasons=(), vehicle_reasons=(), vehicle_adv=(), face_raises=False):
        self.calls = []
        self.barcode_reasons, self.face_reasons = list(barcode_reasons), list(face_reasons)
        self.vehicle_reasons, self.vehicle_adv, self.face_raises = list(vehicle_reasons), list(vehicle_adv), face_raises

    def stages(self, skipped=False):
        def barcode(img, c, now):
            self.calls.append("barcode")
            r = StageResult("barcode", True, [Reason.of(x) for x in self.barcode_reasons], details={"readable": True})
            return r, AamvaRecord("636012", "09", {"DAQ": SECRET_LICENCE})

        def face(selfie, front):
            self.calls.append("face")
            if self.face_raises:
                raise RuntimeError(f"boom {SECRET_NAME}")
            crop = np.zeros((112, 112, 3), np.uint8)
            return StageResult("face", True, [Reason.of(x) for x in self.face_reasons],
                               details={"similarity": 0.9}, crops={"selfie_face": crop})

        def vehicle(img, c):
            self.calls.append("vehicle")
            return StageResult("vehicle", True, [Reason.of(x) for x in self.vehicle_reasons],
                               list(self.vehicle_adv), {"plate_read": "ZQX987"}, crops={"plate": np.zeros((20, 60, 3), np.uint8)})

        return Stages(barcode, face, vehicle, liveness_skipped=skipped)


def engine(settings, fakes, skipped=False):
    return Engine(settings, fakes.stages(skipped), clock=lambda: NOW)


def test_all_pass_approved(settings):
    resp = engine(settings, Fakes()).verify(inp())
    assert resp["decision"] == "APPROVED" and resp["reasons"] == []
    assert set(resp) == {"request_id", "decision", "reasons", "stages", "advisories", "timings_ms", "rss_mb", "memory_trace"}
    assert all(resp["stages"][n]["ran"] and resp["stages"][n]["passed"] for n in ("barcode", "face", "vehicle"))
    tr = resp["memory_trace"]
    assert len(tr["points"]) >= 2 and tr["peak_mb"] >= tr["start_mb"] and [m[0] for m in tr["marks"]][-1] == "saved"


def test_stage1_fail_skips_others_and_is_rejected(settings):
    f = Fakes(barcode_reasons=[ReasonCode.BARCODE_UNREADABLE])
    resp = engine(settings, f).verify(inp())
    assert f.calls == ["barcode"] and resp["decision"] == "REJECTED"
    assert resp["stages"]["barcode"]["passed"] is False
    assert resp["stages"]["face"] == {"ran": False} and resp["stages"]["vehicle"] == {"ran": False}
    assert resp["reasons"][0]["retryable"] is True


def test_stage2_and_3_failures_reported_together(settings):
    f = Fakes(face_reasons=[ReasonCode.FACE_MISMATCH], vehicle_reasons=[ReasonCode.PLATE_NOT_FOUND])
    resp = engine(settings, f).verify(inp())
    assert [r["code"] for r in resp["reasons"]] == ["FACE_MISMATCH", "PLATE_NOT_FOUND"]
    assert resp["decision"] == "REJECTED"
    assert [resp["stages"][n]["passed"] for n in ("barcode", "face", "vehicle")] == [True, False, False]


def test_stage_crash_raises_engine_error_never_approved(settings):
    with pytest.raises(EngineError):
        engine(settings, Fakes(face_raises=True)).verify(inp())


def test_input_error_is_not_an_engine_error(settings):
    bad = {k: jpeg(i) for i, k in enumerate(IMAGE_KEYS)}
    bad["vehicle"] = b"not an image"
    with pytest.raises(InputError):
        engine(settings, Fakes()).verify(inp(images=bad))


def test_the_engine_writes_nothing_to_disk(settings, tmp_path, monkeypatch):
    # Photos and licence data stay in memory: no file appears in the temp folder or the working folder.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    engine(settings, Fakes()).verify(inp())
    engine(settings, Fakes(face_reasons=[ReasonCode.FACE_MISMATCH])).verify(inp("r2"))
    assert list(tmp_path.iterdir()) == []


def test_the_same_request_runs_again_there_is_no_replay(settings):
    f = Fakes()
    e = engine(settings, f)
    e.verify(inp())
    e.verify(inp())
    assert f.calls.count("barcode") == 2


def test_no_pii_in_logs_or_response(settings, caplog):
    caplog.set_level(logging.DEBUG)
    resp = engine(settings, Fakes()).verify(inp())
    with pytest.raises(EngineError):
        engine(settings, Fakes(face_raises=True)).verify(inp("r2"))
    text = caplog.text + repr(resp)
    for secret in (SECRET_NAME, SECRET_LICENCE, "1990-01-01"):
        assert secret not in text


def test_full_barcode_fields_only_when_enabled(settings):
    assert "barcode_fields" not in engine(settings, Fakes()).verify(inp())
    on = settings.model_copy(update={"response_barcode_fields": True})
    assert engine(on, Fakes()).verify(inp("r2"))["barcode_fields"] == {"DAQ": SECRET_LICENCE}


def test_advisories_propagate_and_dev_skip_is_flagged(settings):
    f = Fakes(vehicle_adv=[Advisory.COLOUR_MISMATCH])
    resp = engine(settings, f, skipped=True).verify(inp())
    assert resp["advisories"] == ["COLOUR_MISMATCH", "LIVENESS_SKIPPED_DEV"] and resp["decision"] == "APPROVED"
