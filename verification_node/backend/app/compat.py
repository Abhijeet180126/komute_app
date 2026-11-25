"""Route for the main Komüte app's existing DL-Verification client: POST /api/v1/verify.

The client sends JSON with base64 photos and reads `decision`, `stages.<name>.passed`,
`stages.face_biometrics.vlm_confidence` and `rejection_reasons`. This route maps that request onto the node's own
input, runs the same engine as POST /v1/verify, and maps the result back. Nothing is stored.

Request (the first four sections are what the client already sends; marked items are new):
  request_id, driver_id, consent_at (new), consent_policy_version (new),
  personal_info{full_name, date_of_birth},
  license_details{license_number, issuing_province, expiry_date, license_class (optional), issue_date (optional)},
  vehicle_details{make, model, year, color, plate_number},
  images{selfie_base64, license_image_base64 (front), license_back_base64 (new), vehicle_photo_base64}
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.core.contracts import ReasonCode

from .errors import ApiError
from .schemas import build_input
from .security import require_api_key

router = APIRouter()

# (section, key) -> node form field
FIELD_MAP = {
    ("personal_info", "full_name"): "claimed.full_name",
    ("personal_info", "date_of_birth"): "claimed.date_of_birth",
    ("license_details", "license_number"): "claimed.licence_number",
    ("license_details", "issuing_province"): "claimed.issuing_province",
    ("license_details", "expiry_date"): "claimed.expiry_date",
    ("license_details", "license_class"): "claimed.licence_class",
    ("license_details", "issue_date"): "claimed.issue_date",
    ("vehicle_details", "make"): "claimed.vehicle_make",
    ("vehicle_details", "model"): "claimed.vehicle_model",
    ("vehicle_details", "year"): "claimed.vehicle_year",
    ("vehicle_details", "color"): "claimed.vehicle_colour",
    ("vehicle_details", "plate_number"): "claimed.plate_number",
}
IMAGE_MAP = {  # DL-Verification key -> node image key
    "selfie_base64": "selfie",
    "license_image_base64": "licence_front",
    "license_back_base64": "licence_back",
    "vehicle_photo_base64": "vehicle",
}
# The main app sorts the reasons it shows by keyword. These retryable image problems have no such keyword in the
# node's own wording, so their text starts with "Image quality".
QUALITY_CODES = {ReasonCode.BARCODE_UNREADABLE.value, ReasonCode.BARCODE_NOT_AAMVA.value, ReasonCode.IMAGE_QUALITY.value}


def _bad(code: str, message: str) -> ApiError:
    return ApiError(422, code, message)


async def _read_json(request: Request, limit: int) -> dict[str, Any]:
    """Read the body without ever holding more than `limit` bytes, then parse it."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _bad("IMAGE_TOO_LARGE", "The request is larger than the allowed upload size.")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except ValueError:
        raise _bad("INVALID_FIELD", "The body must be JSON.") from None
    if not isinstance(body, dict):
        raise _bad("INVALID_FIELD", "The body must be a JSON object.")
    return body


def _value(v: Any, where: str) -> str:
    if v is None:
        return ""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise _bad("INVALID_FIELD", f"{where} must be a string or a number.")
    return str(v)


def to_node_input(body: dict[str, Any], max_bytes: int) -> tuple[dict[str, str], dict[str, bytes]]:
    """Map the DL-Verification request body to the node's form fields and image bytes."""
    form: dict[str, str] = {}
    for key in ("request_id", "driver_id", "consent_at", "consent_policy_version"):
        form[key] = _value(body.get(key), key)
    for (section, key), target in FIELD_MAP.items():
        part = body.get(section)
        if part is None:
            part = {}
        if not isinstance(part, dict):
            raise _bad("INVALID_FIELD", f"{section} must be an object.")
        form[target] = _value(part.get(key), f"{section}.{key}")

    section = body.get("images")
    if not isinstance(section, dict):
        raise _bad("MISSING_FIELD", "images is required.")
    images: dict[str, bytes] = {}
    for source, target in IMAGE_MAP.items():
        raw = section.get(source)
        if not isinstance(raw, str) or not raw:
            raise _bad("MISSING_FIELD", f"images.{source} is required.")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise _bad("INVALID_IMAGE", f"images.{source} is not valid base64.") from None
        if not data:
            raise _bad("MISSING_FIELD", f"images.{source} is required.")
        if len(data) > max_bytes:
            raise _bad("IMAGE_TOO_LARGE", f"images.{source} is larger than {max_bytes} bytes.")
        images[target] = data
    return form, images


def to_dl_response(result: dict[str, Any]) -> dict[str, Any]:
    """Map the node's result to the shape the main app reads. A stage that did not run counts as not passed."""
    stages = result.get("stages", {})
    barcode, face, vehicle = (stages.get(n) or {} for n in ("barcode", "face", "vehicle"))
    messages = []
    for r in result.get("reasons", []):
        m = r["message"]
        messages.append(f"Image quality: {m[0].lower()}{m[1:]}" if r["code"] in QUALITY_CODES else m)
    return {
        "request_id": result["request_id"],
        "decision": result["decision"],
        "stages": {
            "license_ocr": {"passed": bool(barcode.get("passed"))},
            "face_biometrics": {"passed": bool(face.get("passed")), "vlm_confidence": float(face.get("similarity", 0.0))},
            "vehicle_verification": {"passed": bool(vehicle.get("passed"))},
        },
        "rejection_reasons": messages,
        "reason_codes": [r["code"] for r in result.get("reasons", [])],
        "advisories": result.get("advisories", []),
    }


@router.post("/api/v1/verify", dependencies=[Depends(require_api_key)])
async def verify_dl(request: Request) -> JSONResponse:
    queue = request.app.state.queue
    job = await queue.acquire(request.app.state.settings.busy_wait_s)
    if job is None:
        raise ApiError(
            503, "BUSY", "A verification is already running.",
            {"Retry-After": str(request.app.state.settings.retry_after_s)},
        )
    try:
        s = request.app.state.settings
        # 4 base64 photos (4/3 of the bytes each) plus room for the text fields.
        limit = 4 * (s.max_upload_bytes * 4 // 3 + 4) + 200_000
        body = await _read_json(request, limit)
        form, images = to_node_input(body, s.max_upload_bytes)
        inp = build_input(form, images, s.max_text_chars, optional_class_issue=True)
        result = await run_in_threadpool(request.app.state.engine.verify, inp)
        return JSONResponse(to_dl_response(result))
    finally:
        await queue.release(job)
