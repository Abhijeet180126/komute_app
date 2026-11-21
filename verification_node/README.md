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
2. `python verification_node/fetch_models.py` (about 180 MB; every file is checked against `models.lock`).
3. Add these lines to the main app's `.env` (the node reads only `KOMUTE_*` keys and `DL_VERIFICATION_API_KEY`; it ignores the rest):

   ```text
   KOMUTE_ENV=dev
   KOMUTE_DEV_SKIP_LIVENESS=true
   KOMUTE_MODELS_DIR=verification_node/backend/models
   ```

   `KOMUTE_ENV=production` needs a liveness model (`KOMUTE_LIVENESS_MODEL`) and a key of 24+ characters; until a model exists, run in `dev`.
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
python -m pytest -q          # needs the model files for the tests marked "models"
```

## Settings

The full list with defaults is in the Komute-Local repository (`.env.example`). The ones that matter here: `KOMUTE_FACE_MATCH_THRESHOLD` (0.35), `KOMUTE_NAME_MATCH_THRESHOLD` (90), `KOMUTE_MIN_AGE` (18), `KOMUTE_RESPONSE_BARCODE_FIELDS` (keep `false`).

## Contract

`POST /api/v1/verify` takes the JSON the DL-Verification client already sends, plus `images.license_back_base64` (the back of the licence), `consent_at` and `consent_policy_version`. `license_details.license_class` and `.issue_date` are optional. It answers `decision`, `stages.license_ocr|face_biometrics|vehicle_verification.passed`, `stages.face_biometrics.vlm_confidence`, `rejection_reasons`, `reason_codes`, `advisories`. Full text: `docs/INTEGRATION_GUIDE.md` in Komute-Local, section 10.
