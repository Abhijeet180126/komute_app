# verification_node

The Komüte driver verification node, inside the main app's repository. It replaces DL-Verification.

- **What it does:** reads the PDF417 barcode on the licence back, checks the typed details against it, compares the selfie with the licence photo, and reads the vehicle's licence plate. It answers `APPROVED` or `REJECTED` with reason codes.
- **What it stores:** nothing. Photos and licence data stay in memory for one request. The main app keeps the outcome (`VerificationLog`).
- **How it runs:** as its own process (systemd unit `komute-verify`) from the same Python 3.11 environment as the main app, on `127.0.0.1:8080`. The main app calls `POST /api/v1/verify` (the URL in `DL_VERIFICATION_API_URL`, the key in `DL_VERIFICATION_API_KEY`).
- **Source of truth:** the Komute-Local repository. **Do not edit these files here.** Change them in Komute-Local and export again: `python scripts/export_to_main_app.py <this repo's root>`.

## Layout

```text
verification_node/
  backend/core/        the engine (stages, gates, matching, settings)
  backend/app/         the REST wrapper (/api/v1/verify, /v1/verify, /health, /v1/system)
  backend/models/      models.lock (SHA-256 of each model; the .onnx files are not in git)
  tests/               unit and API tests
  deploy/              komute-verify.service
  fetch_models.py      downloads the four models and checks each hash
  requirements.txt     run-time packages, exact versions (Python 3.11)
  requirements-dev.txt test tools
```

## Set up (once per machine)

1. `pip install -r requirements.txt` from the repo root. The main `requirements.txt` includes `-r verification_node/requirements.txt`.
2. `python verification_node/fetch_models.py` (five models, about 50 MB; every file is checked against `models.lock`).
3. Add these lines to the main app's `.env` (the node reads only `KOMUTE_*` keys and `DL_VERIFICATION_API_KEY`; it ignores the rest):

   ```text
   KOMUTE_ENV=production
   KOMUTE_MODELS_DIR=verification_node/backend/models
   KOMUTE_LIVENESS_MODEL=verification_node/backend/models/minifasnet_v2.onnx
   DL_VERIFICATION_API_KEY=<24+ random characters, the same value the app sends>
   KOMUTE_REDIS_URL=redis://127.0.0.1:6379/1
   ```

   The node refuses to start without an API key, in every mode. `KOMUTE_ENV=production` also needs the key to be 24+
   characters and a liveness model, and refuses `KOMUTE_DEV_SKIP_LIVENESS`. `dev` is for local testing only.
4. Install the unit: `deploy/komute-verify.service` (see the comments in it), then `systemctl enable --now komute-verify`.
5. Check: `curl http://127.0.0.1:8080/health`.

## Run by hand (development)

```bash
python -m uvicorn backend.app.main:create_app --factory --app-dir verification_node --host 127.0.0.1 --port 8080 --workers 1
```

Start it from the repo root, so that `.env` is found and `KOMUTE_MODELS_DIR` resolves.

## Tests

```bash
cd verification_node
pip install -r requirements-dev.txt
python -m pytest -q          # tests marked "models" skip when the model files are missing
```

## Settings

The full list with defaults is in the Komute-Local repository (`.env.example`). The ones that matter here: `KOMUTE_FACE_MATCH_THRESHOLD` (0.40), `KOMUTE_LIVENESS_THRESHOLD` (0.5), `KOMUTE_NAME_MATCH_THRESHOLD` (90), `KOMUTE_MIN_AGE` (18), `KOMUTE_BUSY_WAIT_S` (100), `KOMUTE_REDIS_URL` (or the main app's `REDIS_URL`), `KOMUTE_RESPONSE_BARCODE_FIELDS` (keep `false`).

## Models and their licences

| Role | File | Source | Licence |
|---|---|---|---|
| Face detection | `face_detection_yunet_2023mar.onnx` | OpenCV Zoo (YuNet) | MIT |
| Face embedding | `face_recognition_sface_2021dec.onnx` | OpenCV Zoo (SFace) | Apache-2.0 |
| Liveness | `minifasnet_v2.onnx` | minivision-ai/Silent-Face-Anti-Spoofing (MiniFASNetV2), ONNX by UniFace | Apache-2.0 |
| Plate detection | `yolo-v9-t-384-license-plates-end2end.onnx` | ankandrew/open-image-models | **not confirmed**: check before production |
| Plate OCR | `cct_xs_v2_global.onnx` | ankandrew/fast-plate-ocr | **not confirmed**: weights reported MIT, training data CC-BY-NC-ND |

`w600k_r50.onnx` (InsightFace buffalo_l) is no longer used: InsightFace licenses its models for non-commercial research only.
SFace on AgeDB-30: at 0.40, 1 of 3,000 different people accepted and 29% of same-person pairs 30 years apart refused
(`docs/calibration/face-agedb30-sface.md` in Komute-Local).

## Scope

Canadian licences only: the issuer (IIN) and province tables cover the 13 provinces and territories. A licence from
anywhere else is refused (`INVALID_FIELD` on `issuing_province`, or `JURISDICTION_MISMATCH`). Riders are not affected.

## Contract: `POST /api/v1/verify`

Header `X-API-Key` (always required; otherwise `401 UNAUTHORIZED`). Body: JSON, at most about 4 x 1.34 x
`KOMUTE_MAX_UPLOAD_BYTES`.

```json
{
  "request_id": "req_ab12cd34",              // letters, digits, - and _, max 64
  "driver_id": "usr_42",                     // same rule
  "consent_at": "2026-11-24T10:00:00+00:00", // ISO-8601 with a time zone
  "consent_policy_version": "2026-10-29",
  "personal_info": {"full_name": "...", "date_of_birth": "YYYY-MM-DD"},
  "license_details": {"license_number": "...", "issuing_province": "ON", "expiry_date": "YYYY-MM-DD",
                      "license_class": "G (optional)", "issue_date": "YYYY-MM-DD (optional)"},
  "vehicle_details": {"make": "...", "model": "...", "year": 2020, "color": "red", "plate_number": "ABC123"},
  "images": {"selfie_base64": "...", "license_image_base64": "... (front)", "license_back_base64": "... (barcode side)",
             "vehicle_photo_base64": "..."}
}
```

Required: every field above except `license_class`, `issue_date`, `make`, `model` and `year`. Text fields are at most
`KOMUTE_MAX_TEXT_CHARS` (200). Images: JPEG, PNG or WebP, each at most `KOMUTE_MAX_UPLOAD_BYTES`.

**200 response** (a decision was made):

```json
{
  "request_id": "req_ab12cd34",
  "decision": "APPROVED",                    // or "REJECTED"
  "stages": {"license_ocr": {"passed": true},
             "face_biometrics": {"passed": true, "vlm_confidence": 0.62},
             "vehicle_verification": {"passed": true}},
  "rejection_reasons": [],                   // sentences for the driver
  "reason_codes": [],                        // the codes below
  "advisories": []                           // COLOUR_MISMATCH, COLOUR_UNVERIFIED, LICENCE_FORMAT_UNUSUAL, LIVENESS_SKIPPED_DEV
}
```

`vlm_confidence` is the SFace **cosine similarity** (-1 to 1; the face passes at `KOMUTE_FACE_MATCH_THRESHOLD`, 0.40).
It is not a probability or a percentage: show pass or fail, not the number.

Reason codes in a `REJECTED` answer: `BARCODE_UNREADABLE`, `BARCODE_NOT_AAMVA`, `IMAGE_QUALITY` (retake the photo: a selfie too small, turned away, too dark, too bright or too blurry, or two people in it; the message says which);
`FACE_NOT_FOUND_SELFIE`, `FACE_NOT_FOUND_LICENCE`, `LIVENESS_FAILED`, `FACE_MISMATCH`; `JURISDICTION_MISMATCH`,
`NAME_MISMATCH`, `DOB_MISMATCH`, `LICENCE_NUMBER_MISMATCH`, `CLASS_MISMATCH`, `ISSUE_DATE_MISMATCH`,
`EXPIRY_DATE_MISMATCH`, `LICENCE_EXPIRED`, `LICENCE_NOT_YET_VALID`, `UNDERAGE`; `PLATE_NOT_FOUND`, `PLATE_UNREADABLE`,
`PLATE_MISMATCH`.

**Errors** (no decision; body `{"error": {"code": "...", "message": "..."}}`):

| Status | Code | Meaning | What the driver can do |
|---|---|---|---|
| 401 | `UNAUTHORIZED` | Missing or wrong `X-API-Key` | Nothing: a server setup problem |
| 422 | `MISSING_FIELD` | A required field or image is missing or empty (for example an empty vehicle colour, or no licence back photo) | Complete the profile or add the photo |
| 422 | `INVALID_FIELD` | A field has the wrong form: body not a JSON object, a section not an object, a date not YYYY-MM-DD, `consent_at` without a time zone, a non-Canadian `issuing_province`, a year not four digits, an id with other characters, a text field too long | Fix the profile value; non-Canadian licence: cannot verify |
| 422 | `CONSENT_REQUIRED` | `consent_at` or `consent_policy_version` missing | Nothing: the app must send them |
| 422 | `INVALID_IMAGE` | An image is not valid base64, or not a readable JPEG, PNG or WebP | Retake or re-upload the photo |
| 422 | `IMAGE_TOO_LARGE` | One image, or the whole request, is over the size limit | Use a smaller photo |
| 503 | `BUSY` | The request waited `KOMUTE_BUSY_WAIT_S` (100 s) in the queue without its turn coming; has `Retry-After` | Try again in a few seconds |
| 500 | `ENGINE_ERROR` | The check could not finish | Try again later |

## The verification queue

One verification runs at a time. Every request joins a first-come, first-served line in Redis (list
`komute:verify:queue`, set `KOMUTE_REDIS_URL` or the main app's `REDIS_URL`) and runs when it reaches the front, waiting
up to `KOMUTE_BUSY_WAIT_S` (100 s, inside the main app's 120 s call timeout). Redis holds only random job ids: photos and
licence data never leave the node's memory. A request that gives up or whose caller hangs up leaves the line; one that
died (a crash) is cleared from the front after 30 s. The line is cleared at start-up. If Redis is unreachable the node
keeps verifying one at a time without the shared line, and logs a warning. `GET /health` shows
`"queue": {"backend": "redis", "waiting": N}`.

It can share the main app's Redis server (chat, notifications, cache and rate limits use database 0). Use its own
database, `redis://127.0.0.1:6379/1`, so the line is kept apart. Rules: one node per Redis line (a node clears its line
when it starts); keep Redis on `noeviction` (the Ubuntu default), since an `allkeys-lru` policy could delete queue keys.
