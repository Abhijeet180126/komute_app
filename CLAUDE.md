# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Komüte is a Django 5.2 carpooling site (Python 3.11, venv in `.venv`) with two small FastAPI "nodes" next to it. The root `README.md` documents the environment variables and how to start the verification node; read it for those.

## Commands

On this machine run every Python command through uv, never `pip` or the venv's python directly:
`uv run --no-project --python .venv python manage.py <command>` (written as `python` below).

```text
python manage.py runserver                  # ASGI dev server (daphne is in INSTALLED_APPS); needs Redis for chat and rate limits
python manage.py qcluster                   # django-q2 worker: queued emails, retries
python -m daphne -b 127.0.0.1 -p 8000 config.asgi:application   # production process

python manage.py test komute --settings=config.settings_regression          # main suite
python manage.py test api --settings=config.settings_regression             # mobile API suite
python manage.py test komute.tests_html_hygiene --settings=config.settings_regression
python manage.py test komute.tests_regression.PlaceAutocompleteTests.test_pages_load_shared_script --settings=config.settings_regression   # one test

cd map && python -m pytest -q                                   # map node (add tests/core/test_apple.py::test_name for one)
cd verification_node && python -m pytest -q                     # verification node
```

- **Always pass `--settings=config.settings_regression`.** It swaps Postgres, SES, Redis and axes for SQLite, in-memory equivalents and dummy Google/Apple sign-in keys, and runs queued emails inline. Without it many tests fail (axes rejects `client.login()`, social apps are missing).
- `config/settings.py` reads `SECRET_KEY`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `FIELD_ENCRYPTION_KEY` (a Fernet key), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SES_REGION` and `DEFAULT_FROM_EMAIL` with no default, at import time. `settings_regression` imports it, so tests need all nine set. If `.env` lacks them, export throwaway values.
- No linter or formatter is configured. `cron.txt` lists the scheduled management commands (ride completion, retention purges).
- A blank line in `.env` (`NAME=`) counts as set for python-decouple, so a default is ignored. Use `config('X', default='') or fallback` for such settings.

## Architecture

**Processes.** The Django ASGI site (Daphne), a django-q2 worker, Redis (cache, channel layer, rate limits) and two FastAPI nodes that run as separate processes in the same venv: `verification_node/` (driver identity checks, port 8080) and `map/` (Apple Maps, port 8081). The site calls a node over HTTP with a shared key in `X-API-Key` (`DL_VERIFICATION_API_*`, `MAP_API_*`) and treats node errors as its own messages. Nodes store nothing.

**`komute/` is one large app.** `views.py` (about 2,700 lines) handles requests. Business rules live in `komute/services/*` (rides, bookings, accounts, geo, verification, map_client…) and are shared by the website views and the mobile API, so change a rule in the service, not in a view. `models.py` is the data model (the rides table is `Rides`, with `RideStop`, `RideBooking`, `RideSearch`). `consumers.py` is the chat and notification websockets. `tasks.py` queues emails (each gets an `EmailLog` row). `middleware.py` holds the CSP and `LegalAcceptanceMiddleware`.

**Things that cut across files:**
- URLs carry hashids, not primary keys: `komute.hashid_utils.encode_id/decode_id`.
- Throttling is `settings.RATE_LIMITS` (name → max, window) plus `views._rate_limited(request, name, extra_key, per_ip)`. It fails open when the cache is down.
- Service area: `services/geo.py:in_us_canada` (US and Canada boxes) and `service_area.py` (Quebec pickups, stops and drop-offs are refused; data in `komute/data/quebec.json`).
- The CSP in `middleware.py` is report-only unless `CSP_ENFORCE=True`. A test (`PrivacyPolicyProcessorTests`) fails unless every host in the CSP is named in `templates/privacy-policy.html` and in its `CSP_HOSTS` map. Any new outside host needs all three.
- Changing `TERMS_VERSION` or `PRIVACY_VERSION` in settings makes every user accept the legal pages again.
- Templates in `templates/` carry large inline scripts. Shared scripts are in `static/js/` (for example `place-autocomplete.js`, used by post-ride, edit-ride and search-rides).
- `api/` is the Flutter app's JSON API (`/api/v1/`, DRF + allauth.headless, token in `X-Session-Token`). Its routes exist only when `API_ENABLED=True`; otherwise every `/api/` URL is a 404.
- Code comments cite audit and issue IDs (S-8, P-11, ISSUES.md M2). Those documents are not in the repo (`docs/` is git-ignored).

**Test helpers** are in `komute/tests_regression.py`: `make_user`, `make_driver_with_vehicle` (the plate comes from the first four characters of the email, so use distinct prefixes), `make_ride`. Page tests never see a browser; `tests_html_hygiene.py` crawls public pages for dead links and inert buttons.

## Map integration (in progress)

Replacing the public Photon, OSRM, Leaflet and OSM-tile services with Apple Maps through `map/`. Full state, decisions and measured results are in `map/CONTEXT.md`; read it before continuing.

- **Done, all behind `.env` switches that default to the original behaviour:**
  - Release 1, `MAP_AUTOCOMPLETE=photon|apple`: `/map/suggest/` and `/map/details/`; `static/js/place-autocomplete.js` keeps both paths.
  - Release 2, `MAP_ROUTES=osrm|apple`: `/map/route/`; `static/js/map-routes.js` (`KomuteRoutes.summary`) serves post-ride and edit-ride on either provider.
  - Release 3, `MAP_DISPLAY=leaflet|mapkit`: `/map/token/`; ride-summary and booking-details carry both map blocks. Apple's route is only drawn on Apple's map.
  - Code: `komute/views_map.py`, `komute/services/map_client.py`, `komute/templatetags/map_tags.py` (`{% map_config %}`, `{% map_setting %}`). Tests: `komute/tests_map.py`. Real-page e2e: `map/qa/e2e_apple.mjs`.
- **Decisions:** "Locate Me" stays on Photon. US and Canada both stay supported (`MAP_COUNTRIES=CA,US` on the node).
- **Pending:** a cleanup release after a stable week on Apple: remove the switches, the Photon autocomplete path, `map-routes.js`'s OSRM path, the Leaflet blocks, and `unpkg.com`, OSM tiles and `router.project-osrm.org` from the CSP, the privacy policy and `CSP_HOSTS`.
- `map/qa/` holds the live test scripts. `qa/seed_real_pages.py` runs the real site on a throwaway SQLite database (port 8770); `qa/*.mjs` drive headless Chrome over the DevTools protocol, with no browser-automation package installed.
- The Apple `.p8` key must stay out of git (`*.p8` is ignored). The node reads it from `APPLE_MAPS_PRIVATE_KEY_PATH`. `APPLE_MAPS_WEB_ORIGIN` must include the scheme (`https://komute.ca`); MapKit rejects a bare domain.
- Two things Apple's rules make unsafe to assume: its license is quoted as limiting stored map data and display on non-Apple maps (unconfirmed), and its autocomplete answers a full typed address with nothing unless a location is sent (the node adds a default).
