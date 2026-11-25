import tempfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.core  # noqa: F401
import cv2

from backend.app.main import create_app
from backend.core.config import Settings
from backend.app.pages import STATIC
from backend.core.contracts import EngineError
from backend.core.imaging import IMAGE_KEYS, decode_image
from backend.core.monitor import EngineMonitor

KEY = "test-api-key-123"


@pytest.fixture
def settings():
    return Settings(_env_file=None, api_key=KEY, dev_skip_liveness=True, max_upload_bytes=200_000, busy_wait_s=0.2)


class FakeEngine:
    def __init__(self, settings, mode="ok"):
        self.mode, self.seen, self.models = mode, [], {"face_embedder": True, "plate_detector": True, "plate_ocr": True, "liveness": False}
        self.monitor = EngineMonitor()

    def verify(self, inp):
        self.seen.append(inp)
        for k in IMAGE_KEYS:
            decode_image(inp.image_bytes[k], 2048)  # raises InputError exactly like the real engine
        if self.mode == "error":
            raise EngineError("ENGINE_ERROR")
        return {"request_id": inp.request_id, "decision": "APPROVED", "reasons": []}

    def health_info(self):
        return {"models_loaded": self.models}

    monitor = None  # set per instance below

    def models_info(self):
        return [{"role": "face_embedder", "stage": "face", "file": "face_recognition_sface_2021dec.onnx", "loaded": True},
                {"role": "liveness", "stage": "face", "file": None, "loaded": False}]


def make(settings, mode="ok"):
    eng = FakeEngine(settings, mode)
    return TestClient(create_app(settings, eng)), eng


def jpg(seed=0):
    return cv2.imencode(".jpg", np.random.default_rng(seed).integers(0, 255, (40, 40, 3), dtype=np.uint8))[1].tobytes()


FIELDS = {
    "request_id": "req-1", "driver_id": "drv-1", "consent_at": "2026-11-11T12:00:00Z", "consent_policy_version": "v1",
    "claimed.full_name": "Alex Tester", "claimed.date_of_birth": "1990-03-15", "claimed.licence_number": "T1234-56789-01234",
    "claimed.issuing_province": "ON", "claimed.plate_number": "ABC123", "claimed.vehicle_colour": "Grey",
    "claimed.licence_class": "G", "claimed.issue_date": "2023-09-14", "claimed.expiry_date": "2030-12-31",
}


def post(client, fields=None, files=None, key=KEY, drop=()):
    data = {k: v for k, v in (fields or FIELDS).items() if k not in drop}
    f = {k: (f"{k}.jpg", v, "image/jpeg") for k, v in (files or {k: jpg(i) for i, k in enumerate(IMAGE_KEYS)}).items()}
    headers = {"X-API-Key": key} if key else {}
    return client.post("/v1/verify", data=data, files=f, headers=headers)


def code(r):
    return r.json()["error"]["code"]


def test_missing_and_wrong_key_401(settings):
    c, _ = make(settings)
    assert post(c, key=None).status_code == 401 and code(post(c, key=None)) == "UNAUTHORIZED"
    assert post(c, key="nope").status_code == 401


def test_happy_path_returns_engine_response(settings):
    c, eng = make(settings)
    r = post(c)
    assert r.status_code == 200 and r.json()["decision"] == "APPROVED"
    inp = eng.seen[0]
    assert inp.request_id == "req-1" and inp.claimed.date_of_birth.isoformat() == "1990-03-15"
    assert (inp.claimed.licence_class, str(inp.claimed.issue_date), str(inp.claimed.expiry_date)) == ("G", "2023-09-14", "2030-12-31")
    assert inp.consent_at.tzinfo is not None


def test_missing_consent_422(settings):
    c, _ = make(settings)
    for drop in (("consent_at",), ("consent_policy_version",), ("consent_at", "consent_policy_version")):
        r = post(c, drop=drop)
        assert r.status_code == 422 and code(r) == "CONSENT_REQUIRED"


def test_consent_without_timezone_422(settings):
    c, _ = make(settings)
    r = post(c, {**FIELDS, "consent_at": "2026-11-11T12:00:00"})
    assert r.status_code == 422 and code(r) == "INVALID_FIELD"


def test_bad_province_422_and_nf_accepted_as_nl(settings):
    c, eng = make(settings)
    assert code(post(c, {**FIELDS, "claimed.issuing_province": "ZZ"})) == "INVALID_FIELD"
    assert post(c, {**FIELDS, "claimed.issuing_province": "NF", "request_id": "req-nf"}).status_code == 200
    assert eng.seen[-1].claimed.issuing_province == "NL"


def test_missing_field_and_bad_date_422(settings):
    c, _ = make(settings)
    assert code(post(c, drop=("claimed.full_name",))) == "MISSING_FIELD"
    assert code(post(c, {**FIELDS, "claimed.date_of_birth": "15/03/1990"})) == "INVALID_FIELD"
    assert code(post(c, {**FIELDS, "claimed.vehicle_year": "abc"})) == "INVALID_FIELD"
    for name in ("claimed.licence_class", "claimed.issue_date", "claimed.expiry_date"):
        assert code(post(c, drop=(name,))) == "MISSING_FIELD"
    assert code(post(c, {**FIELDS, "claimed.expiry_date": "2030/12/31"})) == "INVALID_FIELD"


def test_request_id_rejects_path_characters(settings):
    c, _ = make(settings)
    for bad in ("../x", "a/b", "a b", "x" * 65):
        r = post(c, {**FIELDS, "request_id": bad})
        assert r.status_code == 422 and code(r) == "INVALID_FIELD"


def test_missing_image_422(settings):
    c, _ = make(settings)
    files = {k: jpg(i) for i, k in enumerate(IMAGE_KEYS) if k != "vehicle"}
    r = post(c, files=files)
    assert r.status_code == 422 and code(r) == "MISSING_FIELD"


def test_oversize_upload_422(settings):
    c, _ = make(settings)
    files = {k: jpg(i) for i, k in enumerate(IMAGE_KEYS)}
    files["selfie"] = b"\xff\xd8\xff" + b"0" * 250_000
    r = post(c, files=files)
    assert r.status_code == 422 and code(r) == "IMAGE_TOO_LARGE"


def test_random_bytes_image_422_not_500(settings):
    c, _ = make(settings)
    files = {k: jpg(i) for i, k in enumerate(IMAGE_KEYS)}
    files["licence_back"] = b"this is not an image at all"
    r = post(c, files=files)
    assert r.status_code == 422 and code(r) == "INVALID_IMAGE"


def test_busy_returns_503_with_retry_after_and_gate_releases(settings):
    c, _ = make(settings)
    assert c.app.state.gate.try_acquire()
    r = post(c)
    assert r.status_code == 503 and code(r) == "BUSY" and r.headers["Retry-After"] == str(settings.retry_after_s)
    c.app.state.gate.release()
    assert post(c).status_code == 200
    assert post(c, files={k: b"x" for k in IMAGE_KEYS}).status_code == 422
    assert post(c, {**FIELDS, "request_id": "req-2"}).status_code == 200  # gate released after a failed request


def test_a_second_request_waits_for_its_turn(settings):
    # Ticket item 5: while one check runs, the next one waits instead of failing at once.
    import threading
    c, _ = make(settings.model_copy(update={"busy_wait_s": 5.0}))
    gate = c.app.state.gate
    assert gate.try_acquire()
    threading.Timer(0.3, gate.release).start()
    assert post(c).status_code == 200


def test_engine_error_500_has_no_decision_key(settings):
    c, _ = make(settings, "error")
    r = post(c)
    assert r.status_code == 500 and code(r) == "ENGINE_ERROR" and set(r.json()) == {"error"}


def test_health_no_secrets_and_degraded(settings):
    c, eng = make(settings)
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and KEY not in r.text
    eng.models["plate_ocr"] = False
    r = c.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded"


def test_health_reports_the_queue(settings):
    c, _ = make(settings)
    assert c.get("/health").json()["queue"] == {"backend": "local", "waiting": None}


def test_verify_goes_through_the_redis_queue_and_leaves_it(settings):
    from backend.app.gate import RedisQueue
    from tests.app.test_queue import FakeRedis

    with make(settings)[0] as c:  # the context runs start-up, which resets the line
        fake = FakeRedis()
        c.app.state.queue = RedisQueue(fake, c.app.state.gate, key="t:q")
        assert post(c).status_code == 200
        assert fake.lists.get("t:q") == [] and c.get("/health").json()["queue"] == {"backend": "redis", "waiting": 0}


def test_redis_url_comes_from_the_shared_env_name(tmp_path):
    from backend.core.config import Settings

    f = tmp_path / ".env"
    f.write_text("KOMUTE_API_KEY=k\nREDIS_URL=redis://127.0.0.1:6379/0\n")
    assert Settings(_env_file=f).redis_url == "redis://127.0.0.1:6379/0"
    f.write_text("KOMUTE_API_KEY=k\nREDIS_URL=\n")
    assert Settings(_env_file=f).redis_url is None


def test_there_is_no_replay_route_and_the_same_request_id_runs_again(settings):
    c, eng = make(settings)
    assert c.get("/v1/sessions/req-9", headers={"X-API-Key": KEY}).status_code == 404
    assert post(c).status_code == 200 and post(c).status_code == 200
    assert len(eng.seen) == 2


def test_uploads_stay_in_memory(settings, monkeypatch):
    # An upload part over 1 MiB used to be written to a temporary file by the form parser. It must not be.
    def no_rollover(self, *a, **k):
        raise AssertionError("an upload was written to disk")

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", no_rollover)
    big = settings.model_copy(update={"max_upload_bytes": 4_000_000})
    c, _ = make(big)
    files = {k: jpg(i) for i, k in enumerate(IMAGE_KEYS)}
    files["vehicle"] = bytes([0xFF, 0xD8, 0xFF]) + b"0" * 2_500_000 + jpg(1)[3:]  # about 2.5 MB, over the old 1 MiB limit
    r = post(c, files=files)
    assert r.status_code in (200, 422)  # accepted or rejected as an image, but never written to disk


def test_pages_are_not_served_when_the_static_folder_is_absent(settings, monkeypatch, tmp_path):
    import backend.app.main as app_main
    import backend.app.pages as pages

    monkeypatch.setattr(app_main, "STATIC", tmp_path / "no-such-folder")
    monkeypatch.setattr(pages, "STATIC_STATS", tmp_path / "no-such-folder" / "stats.html")
    c, _ = make(settings)
    assert c.get("/").status_code == 404 and c.get("/stats").status_code == 404
    # a browser asking /v1/system gets the key check and JSON, not a page
    assert c.get("/v1/system", headers={"Accept": "text/html"}).status_code == 401


@pytest.mark.skipif(not STATIC.is_dir(), reason="backend/static is not part of the copy inside the main app")
def test_index_served(settings):
    c, _ = make(settings)
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "Komute Verify" in r.text


def test_a_missing_key_setting_fails_closed(settings):
    # Settings refuse to load without a key; if one ever got past them, every call is still refused.
    open_settings = settings.model_copy(update={"api_key": None})
    c, _ = make(open_settings)
    assert post(c, key=None).status_code == 401
    assert post(c, {**FIELDS, "request_id": "req-k"}, key="anything").status_code == 401
    assert c.get("/health").status_code == 200


def test_security_headers_on_every_response(settings):
    client, _ = make(settings)
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_system_needs_key_and_hides_secrets(settings):
    client, eng = make(settings)
    assert client.get("/v1/system").status_code == 401
    with client:  # runs the lifespan, so the system sampler starts
        r = client.get("/v1/system", headers={"X-API-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    text = r.text
    assert KEY not in text
    assert body["settings"]["authentication"] is True and "api_key" not in body["settings"]
    assert body["sample"]["memory"]["process_rss_mb"] > 0 and body["host"]["cpu_logical_cores"] >= 1
    status = {m["role"]: m["status"] for m in body["models"]}
    assert status == {"face_embedder": "idle", "liveness": "skipped (dev)"}
    assert body["engine"]["busy"] is False and body["engine"]["gate_busy"] is False


@pytest.mark.skipif(not STATIC.is_dir(), reason="backend/static is not part of the copy inside the main app")
def test_stats_page_served_on_both_paths(settings):
    client, _ = make(settings)
    for path in ("/stats", "/system-stats"):
        r = client.get(path)
        assert r.status_code == 200 and "Komute System Monitor" in r.text and r.headers["Cache-Control"] == "no-store"
    # A browser opening /v1/system gets the page without a key; the page's fetch (Accept */*) still needs one.
    r = client.get("/v1/system", headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    assert r.status_code == 200 and "Komute System Monitor" in r.text
    assert client.get("/v1/system", headers={"Accept": "*/*"}).status_code == 401
