# Komute

A Django-based carpooling platform.

## Environment Variables

Copy `.env` and fill in values. All variables are required unless a default is noted.

### Email (SMTP)

| Variable | Description | Example |
|---|---|---|
| `EMAIL_BACKEND` | Django email backend | `django.core.mail.backends.smtp.EmailBackend` |
| `EMAIL_HOST` | SMTP server hostname | `smtp.hostinger.com` |
| `EMAIL_PORT` | SMTP port | `587` |
| `EMAIL_USE_TLS` | Enable STARTTLS | `True` |
| `EMAIL_HOST_USER` | SMTP login / sender address | `noreply@example.com` |
| `EMAIL_HOST_PASSWORD` | SMTP password | *(keep secret)* |

### Django Core

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | *(required)* | Django secret key — change in production |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated list of allowed hostnames |

### Infrastructure

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://127.0.0.1:6379` | Redis connection URL; use `rediss://` for TLS (certificate is verified) |

Chat and rate limiting need a running Redis. In development, run one locally and keep the default URL:

- **Docker:** `docker run -d --name komute-redis -p 127.0.0.1:6379:6379 redis:7`
- **WSL:** `sudo apt install redis-server && sudo service redis-server start`
- **Windows native:** install [Memurai](https://www.memurai.com/) (Developer edition), which runs as a service on port 6379

Don't point `REDIS_URL` at a free hosted plan: Upstash deletes free databases after 14 days of inactivity. The test suite doesn't need Redis (`config.settings_regression` uses the in-memory channel layer).

### Mobile App API

The JSON API for the Flutter app lives in the `api` app under `/api/v1/` (plan: `docs/api-modular-breakup.md`).
The app signs in through allauth.headless and sends its token as the `X-Session-Token` header.

| Variable | Default | Description |
|---|---|---|
| `API_ENABLED` | `False` | Registers the `/api/` routes. Off, every `/api/` URL is a 404 and the website is unchanged |
| `API_DOCS_PUBLIC` | `False` | Lets anyone open `/api/v1/schema/` and `/api/v1/docs/` (Swagger UI); off, staff only |
| `API_THROTTLE_ANON` | `60/min` | Per-address cap on API calls without a token |
| `API_THROTTLE_USER` | `240/min` | Per-user cap on API calls with a token |
| `APPLE_APP_BUNDLE_ID` | *(empty)* | The iOS app's bundle ID, for Sign in with Apple inside the app. Needs the other `APPLE_*` keys |

Auth endpoints are under `/api/v1/auth/` (module 1). The account rules live in `komute/services/accounts.py`, shared
by the website's views and the API.

API tests: `manage.py test api --settings=config.settings_regression`. Export the schema for the app developer with
`API_ENABLED=True manage.py spectacular --file schema.yml`.

### Application Tuning

| Variable | Default | Description |
|---|---|---|
| `SEARCH_RADIUS_KM` | `15` | Radius in km around origin/destination within which rides are shown in search results |
| `SERVICE_FEE_CENTS` | `100` | Flat service fee added to every booking, in cents (100 = $1.00) |
| `INVITE_EXPIRY_HOURS` | `48` | Hours before a ride invite link/token expires |

## Verification node

Driver verification runs in `verification_node/`, a second process in the same Python environment (Python 3.11). It checks the licence barcode, the selfie against the licence photo, and the vehicle plate. It stores nothing; the app keeps only the outcome (`VerificationLog`). Do not edit `verification_node/` here: change it in the Komute-Local repository and export it again.

1. `pip install -r requirements.txt` (it includes `verification_node/requirements.txt`).
2. `python verification_node/fetch_models.py` (about 180 MB; every file is checked against `models.lock`).
3. Add to `.env`:

   ```text
   KOMUTE_ENV=dev
   KOMUTE_DEV_SKIP_LIVENESS=true
   KOMUTE_MODELS_DIR=verification_node/backend/models
   DL_VERIFICATION_API_KEY=<same value the app uses>
   ```

4. From the repository root, start the node:

   ```text
   python -m uvicorn backend.app.main:create_app --factory --app-dir verification_node --host 127.0.0.1 --port 8080 --workers 1
   ```

5. `GET http://127.0.0.1:8080/health` must return `"status":"ok"`.

`DL_VERIFICATION_API_URL` defaults to `http://127.0.0.1:8080/api/v1/verify`. The node needs 2 GB of RAM or more and runs one verification at a time. There is no liveness model yet: `KOMUTE_ENV=production` refuses to start without one. Node tests: `cd verification_node && pip install -r requirements-dev.txt && python -m pytest -q`.
