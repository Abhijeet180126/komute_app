"""POST /api/v1/verify: the request and response shapes the main Komüte app's DL-Verification client uses."""
import base64

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.core  # noqa: F401
import cv2

from backend.app.compat import to_dl_response
from backend.app.main import create_app
from backend.core.config import Settings
from backend.core.contracts import EngineError
from backend.core.monitor import EngineMonitor

KEY = "test-api-key-123"
URL = "/api/v1/verify"


def jpg(seed=0):
    return cv2.imencode(".jpg", np.random.default_rng(seed).integers(0, 255, (40, 40, 3), dtype=np.uint8))[1].tobytes()


def b64(b):
    return base64.b64encode(b).decode()


@pytest.fixture
def settings():
    return Settings(_env_file=None, api_key=KEY, dev_skip_liveness=True, max_upload_bytes=200_000)


def stage(passed=True, **details):
    return {"ran": True, "passed": passed, **details}


def result(decision="APPROVED", reasons=(), barcode=None, face=None, vehicle=None, advisories=()):
    return {
        "request_id": "req_ab12", "decision": decision, "reasons": list(reasons), "advisories": list(advisories),
        "stages": {"barcode": barcode or stage(), "face": face or stage(similarity=0.62), "vehicle": vehicle or stage()},
    }


def reason(code, message, retryable=False):
    return {"code": code, "retryable": retryable, "message": message}


class FakeEngine:
    monitor = EngineMonitor()

    def __init__(self, reply=None, error=False):
        self.reply, self.error, self.seen = reply or result(), error, []

    def verify(self, inp):
        self.seen.append(inp)
        if self.error:
            raise EngineError("ENGINE_ERROR")
        return self.reply

    def health_info(self):
        return {"models_loaded": {}}


def body(**over):
    b = {
        "request_id": "req_ab12", "driver_id": "usr_42",
        "consent_at": "2026-11-21T12:00:00Z", "consent_policy_version": "2026-10-29",
        "personal_info": {"full_name": "Alex Tester", "date_of_birth": "1990-03-15"},
        "license_details": {"license_number": "T1234-56789-01234", "issuing_province": "ON", "expiry_date": "2030-12-31"},
        "vehicle_details": {"make": "Honda", "model": "Civic", "year": 2020, "color": "Grey", "plate_number": "ABC 123"},
        "images": {
            "selfie_base64": b64(jpg(1)), "license_image_base64": b64(jpg(2)),
            "license_back_base64": b64(jpg(3)), "vehicle_photo_base64": b64(jpg(4)),
        },
    }
    b.update(over)
    return b


def make(settings, **kw):
    eng = FakeEngine(**kw)
    return TestClient(create_app(settings, eng)), eng


def post(c, payload=None, key=KEY, **kw):
    return c.post(URL, json=payload if payload is not None else body(), headers={"X-API-Key": key} if key else {}, **kw)


def code(r):
    return r.json()["error"]["code"]


def test_the_old_request_maps_onto_the_node_input(settings):
    c, eng = make(settings)
    r = post(c)
    assert r.status_code == 200
    inp = eng.seen[0]
    assert (inp.request_id, inp.driver_id, inp.consent_policy_version) == ("req_ab12", "usr_42", "2026-10-29")
    cl = inp.claimed
    assert (cl.full_name, str(cl.date_of_birth), cl.licence_number, cl.issuing_province, str(cl.expiry_date)) == (
        "Alex Tester", "1990-03-15", "T1234-56789-01234", "ON", "2030-12-31")
    assert (cl.vehicle_make, cl.vehicle_model, cl.vehicle_year, cl.vehicle_colour, cl.plate_number) == (
        "Honda", "Civic", 2020, "Grey", "ABC 123")
    assert (cl.licence_class, cl.issue_date, cl.optional_class_issue) == (None, None, True)
    assert inp.image_bytes["selfie"] == jpg(1) and inp.image_bytes["licence_front"] == jpg(2)
    assert inp.image_bytes["licence_back"] == jpg(3) and inp.image_bytes["vehicle"] == jpg(4)


def test_class_and_issue_date_are_used_when_the_client_sends_them(settings):
    c, eng = make(settings)
    b = body()
    b["license_details"].update(license_class="G", issue_date="2023-09-14")
    assert post(c, b).status_code == 200
    assert (eng.seen[0].claimed.licence_class, str(eng.seen[0].claimed.issue_date)) == ("G", "2023-09-14")


def test_approved_response_has_the_shape_the_main_app_reads(settings):
    c, _ = make(settings, reply=result(advisories=["LIVENESS_SKIPPED_DEV"]))
    out = post(c).json()
    assert out["decision"] == "APPROVED" and out["rejection_reasons"] == [] and out["reason_codes"] == []
    assert out["stages"] == {
        "license_ocr": {"passed": True},
        "face_biometrics": {"passed": True, "vlm_confidence": 0.62},
        "vehicle_verification": {"passed": True},
    }
    assert out["advisories"] == ["LIVENESS_SKIPPED_DEV"]


def test_rejection_maps_failed_and_skipped_stages_to_not_passed(settings):
    reasons = [reason("FACE_MISMATCH", "The selfie does not match the face on the licence.")]
    r = result("REJECTED", reasons, face=stage(False, similarity=0.12), vehicle={"ran": False})
    out = to_dl_response(r)
    assert out["stages"]["license_ocr"]["passed"] is True
    assert out["stages"]["face_biometrics"] == {"passed": False, "vlm_confidence": 0.12}
    assert out["stages"]["vehicle_verification"]["passed"] is False
    assert out["rejection_reasons"] == ["The selfie does not match the face on the licence."]
    assert out["reason_codes"] == ["FACE_MISMATCH"]


def test_barcode_failure_leaves_face_and_vehicle_not_passed(settings):
    msg = "The barcode on the back of your licence is not clearly visible. Please retake the photo of the barcode."
    r = result("REJECTED", [reason("BARCODE_UNREADABLE", msg, True)], barcode=stage(False, readable=False),
               face={"ran": False}, vehicle={"ran": False})
    out = to_dl_response(r)
    assert [out["stages"][k]["passed"] for k in ("license_ocr", "face_biometrics", "vehicle_verification")] == [False] * 3
    assert out["stages"]["face_biometrics"]["vlm_confidence"] == 0.0
    # the main app sorts reasons by keyword: image problems must contain "quality"
    assert out["rejection_reasons"][0].startswith("Image quality: the barcode on the back")


def test_other_reasons_keep_their_own_text(settings):
    r = result("REJECTED", [reason("NAME_MISMATCH", "The name given does not match the name on the licence.")],
               barcode=stage(False))
    assert to_dl_response(r)["rejection_reasons"] == ["The name given does not match the name on the licence."]


def test_key_is_required(settings):
    c, _ = make(settings)
    assert post(c, key=None).status_code == 401 and post(c, key="wrong").status_code == 401


def test_back_photo_is_required(settings):
    c, _ = make(settings)
    b = body()
    del b["images"]["license_back_base64"]
    r = post(c, b)
    assert r.status_code == 422 and code(r) == "MISSING_FIELD" and "license_back_base64" in r.json()["error"]["message"]


def test_consent_is_required(settings):
    c, _ = make(settings)
    for drop in ("consent_at", "consent_policy_version"):
        b = body()
        del b[drop]
        r = post(c, b)
        assert r.status_code == 422 and code(r) == "CONSENT_REQUIRED"


def test_bad_inputs_are_422_never_500(settings):
    c, _ = make(settings)
    b = body()
    b["images"]["selfie_base64"] = "***not base64***"
    assert code(post(c, b)) == "INVALID_IMAGE"
    assert code(post(c, ["not", "an", "object"])) == "INVALID_FIELD"
    assert code(c.post(URL, content=b"{not json", headers={"X-API-Key": KEY})) == "INVALID_FIELD"
    b = body()
    b["personal_info"] = "oops"
    assert code(post(c, b)) == "INVALID_FIELD"
    b = body()
    b["personal_info"]["full_name"] = ""  # an incomplete profile
    assert code(post(c, b)) == "MISSING_FIELD"
    b = body()
    b["vehicle_details"]["year"] = True
    assert code(post(c, b)) == "INVALID_FIELD"
    b = body()
    b["license_details"]["issuing_province"] = "ZZ"
    assert code(post(c, b)) == "INVALID_FIELD"


def test_oversized_photo_and_body_are_422(settings):
    c, _ = make(settings)
    b = body()
    b["images"]["vehicle_photo_base64"] = b64(b"\xff\xd8\xff" + b"0" * 250_000)
    assert code(post(c, b)) == "IMAGE_TOO_LARGE"
    b = body()
    b["images"]["vehicle_photo_base64"] = "A" * 2_000_000  # past the whole-body limit
    assert code(post(c, b)) == "IMAGE_TOO_LARGE"


def test_busy_returns_503_with_retry_after_and_releases(settings):
    c, _ = make(settings)
    assert c.app.state.gate.try_acquire()
    r = post(c)
    assert r.status_code == 503 and code(r) == "BUSY" and r.headers["Retry-After"] == str(settings.retry_after_s)
    c.app.state.gate.release()
    assert post(c).status_code == 200
    assert post(c, ["bad"]).status_code == 422
    assert post(c).status_code == 200  # the gate is released after a failed request


def test_engine_error_is_500_without_a_decision(settings):
    c, _ = make(settings, error=True)
    r = post(c)
    assert r.status_code == 500 and code(r) == "ENGINE_ERROR" and set(r.json()) == {"error"}


def test_the_native_route_still_requires_class_and_issue_date(settings):
    c, _ = make(settings)
    fields = {
        "request_id": "req-1", "driver_id": "drv-1", "consent_at": "2026-11-11T12:00:00Z", "consent_policy_version": "v1",
        "claimed.full_name": "Alex Tester", "claimed.date_of_birth": "1990-03-15", "claimed.licence_number": "T1",
        "claimed.issuing_province": "ON", "claimed.plate_number": "ABC123", "claimed.vehicle_colour": "Grey",
        "claimed.expiry_date": "2030-12-31",
    }
    files = {k: (f"{k}.jpg", jpg(i), "image/jpeg") for i, k in enumerate(("selfie", "licence_front", "licence_back", "vehicle"))}
    r = c.post("/v1/verify", data=fields, files=files, headers={"X-API-Key": KEY})
    assert r.status_code == 422 and code(r) == "MISSING_FIELD"
