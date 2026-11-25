# Map project: full context and handoff

Written 23 Nov 2026 so a new session can continue without the chat history. `CLAUDE.md` in the repo root is the short guide; this file is the detail. Nothing here has been committed to git.

## Goal
Komüte used four free public services for maps: Leaflet 1.9.4 (map display), the OpenStreetMap tile server (map images), Photon at `photon.komoot.io` (address search and "Locate Me"), and the OSRM demo server (routes, drive time). Goal: find something more reliable and better for door-level Canadian and US addresses, and link it into the main app in small, reversible releases.

## Decisions made by the user
- Providers tried: TomTom first (built, tested, **deleted**), then **Apple Maps** (the user has a paid Apple Developer account; chosen).
- Apple is used for address autocomplete, routes and (last) the route map.
- **"Locate Me" stays on Photon** (free, no key; public server is "reasonable use", no guarantee; self-hosting is the later fix, Apache-2.0).
- **US and Canada both stay supported** (node `MAP_COUNTRIES=CA,US`). Quebec pickups stay refused by `komute/service_area.py`.
- Releases stay behind `.env` switches so each can be rolled back without a redeploy. No commits unless the user asks.

## What exists now
**`map/` (FastAPI node, port 8081, 91 tests).** Apple Maps Server API client (`backend/core/providers/apple.py`): suggest (autocomplete), details, geocode, reverse, route (one Apple call per leg, since Apple has no waypoint parameter), matrix (one call per origin per 10 destinations), plus `mapkit_token` for the browser map. Settings in `backend/core/config.py`: Apple credentials, `MAP_COUNTRIES`, `MAP_DEFAULT_BIAS` (Ottawa), `MAP_API_KEY`, cache TTLs. Endpoints under `/v1/`: `places/suggest`, `places/details`, `places/geocode`, `places/reverse`, `route`, `matrix`, `mapkit-token`, plus public `/health`. `deploy/komute-map.service` for production. `README.md` has the Apple credential steps.

**Main app, Release 1 (switch OFF).** `komute/services/map_client.py`, `komute/views_map.py` (`/map/suggest/`, `/map/details/`), `komute/templatetags/map_tags.py` (`{% map_config %}`, `{% map_setting %}`), `static/js/place-autocomplete.js` (Photon path kept; Apple path added), three templates (post-ride, edit-ride, search-rides) carry `{% map_config %}`, `config/settings.py` (`MAP_API_URL`, `MAP_API_KEY`, `MAP_AUTOCOMPLETE`, rate limits `map-suggest`, `map-details`), `komute/urls.py`, `templates/privacy-policy.html` (wording follows the switch), `requirements.txt` (`-r map/requirements.txt`), root `README.md` ("Map node"), `komute/tests_map.py` (23 tests). The page supplies the rider's own starting point as a location hint (`bias_for`: latest `RideSearch` origin for riders, latest ride origin for drivers); else a GPS fix the browser already allows; else the node's default.

**Main app, Releases 2 and 3 (switches OFF).** `MAP_ROUTES=osrm|apple`: `map_client.route()` (one node call; two in parallel threads when stops exist and `direct=1`; 5-minute cache; `line=1` adds the rounded line), view `/map/route/?o=&d=&s=&direct=1&line=1` (rate limit `map-route`, max 5 stops, points must be in US/Canada), `static/js/map-routes.js` (`KomuteRoutes.summary`, OSRM and Apple paths) used by post-ride `fetchRouteInfo` and edit-ride `triggerRouteSummary`. `MAP_DISPLAY=leaflet|mapkit`: `/map/token/` (404 unless mapkit; rate limit `map-token`), ride-summary and booking-details hold both map blocks behind `{% if map_display == 'mapkit' %}`. CSP (`komute/middleware.py`): `cdn.apple-mapkit.com` in script/img, `*.apple-mapkit.com` in connect, `worker-src 'self' blob:` (all three measured as needed by MapKit). Privacy policy wording follows every switch combination. `komute/tests_map.py` now 54 tests with `PrivacyPolicyProcessorTests`.

**Real-page e2e (23 Nov 2026), all switches on Apple, `map/qa/e2e_apple.mjs`: 31 of 32 checks pass.** MapKit map, route line and stats on both trip pages (458.8 km, 4h 36m with the Kingston stop); post-ride origin and destination picked through Apple autocomplete, strip 450.7 km / 4h 13m, `duration_seconds` filled; a Barrie stop raises the detour warning (72 km, 16%); edit-ride summary on load and after a new destination. No OSRM, OSM, unpkg or Photon-search request, no CSP violation, no console error from the map. The one failure is older than this work: `booking-details.html` loads `static/komute/logo.png`, which does not exist (the file is `static/images/logo.png`).

**Fixes from the manual test (23 Nov 2026), built-in browser, driver and rider, laptop and phone sizes:**
- `komute/views_map.py`: a location hint outside the US and Canada (a rider abroad, here a GPS fix in India) made every search fail with 400 and "Try again". It is now dropped and the node's default is used. Test `test_an_unusable_location_hint_is_dropped_not_refused`.
- `map/backend/core/providers/apple.py` `_send`: a pooled connection Apple had closed failed at once (`ConnectionError`, 502 in 0.25 s) and hid the edit-ride summary. One retry on a dropped connection (all Apple calls are GET); a timeout is not retried. Tests `test_a_dropped_connection_is_retried_once`, `test_a_timeout_is_not_retried`.
- `templates/booking-details.html`, `search-rides.html`: the nav logo pointed at `komute/logo.png` (404); now `images/logo.png`.
- `komute/services/route_demand.py`: `order_by('-created_at', '-id')`, so two searches with the same timestamp keep a fixed order (a flaky test, and a wrong seat count for drivers).
- Still open, older than this work: on a phone the header's user pill and logout button (`.kh-actions`) make pages wider than the screen; edit-ride calls `triggerRouteSummary` twice on load, so it asks for the same route twice.

**`.env`** (git-ignored): Apple settings filled by the user, `MAP_API_URL=http://127.0.0.1:8081`, `MAP_AUTOCOMPLETE=photon`, `MAP_ROUTES=osrm`, `MAP_DISPLAY=leaflet`, old `TOMTOM_*` lines left in place and unused (safe to delete). `MAP_API_KEY` is blank, so the node accepts any caller: set one (24+ characters, same value on both sides) before real deployment.

## Pending
**Mine:**
1. ~~Release 2, routes.~~ Done (see above). Old note: Switch `MAP_ROUTES=osrm|apple`, rate limit `map-route`, `map_client.route()`, view `/map/route/?o=lat,lng&d=lat,lng&s=lat,lng;...&direct=1`. Replace the OSRM calls in `templates/post-ride.html` (`fetchRouteInfo`, about lines 1209-1305: route with stops plus a direct route for the detour warning) and `templates/edit-ride.html` (`triggerRouteSummary`, about 450-480). Keep `duration_seconds` hidden field and `STOP_DETOUR_MAX_KM` / `STOP_DETOUR_MAX_PCT` logic. Short (5 min) Django cache on route answers. The browser still supplies `duration_seconds` (`views.py` ~720 and ~1520); server-side recompute is a later hardening step.
2. ~~Release 3, route map.~~ Done (see above); `unpkg`, OSM and OSRM stay in the CSP until cleanup, since the Leaflet path still exists. Old note: Switch `MAP_DISPLAY=leaflet|mapkit`, `/map/token/`; MapKit JS on `templates/ride-summary.html` and `booking-details.html` (line from `/map/route/?line=1`). Rewrite the CSP in `komute/middleware.py`: add `cdn.apple-mapkit.com` (script, connect, image; maybe `worker-src blob:`, unverified), remove `unpkg.com` (only these two pages use it), `tile.openstreetmap.org`, `router.project-osrm.org`. Update privacy-policy lines naming OSRM/OSM and the tests `PrivacyPolicyProcessorTests.CSP_HOSTS` and the CSP assertions in `tests_regression.py`. CSP is report-only by default; read `/csp-report/` before enforcing.
3. **Cleanup** after a stable week: remove the switches, the Photon autocomplete path, OSRM and Leaflet code.

**The user's gates, before Release 1 goes live:**
- Read Apple's Developer Program License Agreement, Schedule 6: may Komüte store ride coordinates, and show Apple results on a non-Apple map? (Only seen quoted in developer forums.)
- Decide whether the privacy-policy change needs a `PRIVACY_VERSION` bump (it makes every user accept again); get the wording reviewed. Not bumped.
- Deploy the node as a service, set `MAP_API_KEY`, run `python manage.py test komute --settings=config.settings_regression` on their normal database, then flip `MAP_AUTOCOMPLETE=apple`, `MAP_ROUTES=apple`, `MAP_DISPLAY=mapkit` in staging (one at a time is fine). Set `APPLE_MAPS_WEB_ORIGIN` on the node to the site's exact origin, with the scheme, or MapKit refuses the token.

## Measured results (live, 22-23 Nov 2026, one machine, a Sunday)
- **Door-level, 26 Canadian public buildings:** Apple geocode 24 right (Photon 23, OSM Nominatim 23). Autocomplete with a location sent: Apple 25 of 26 first (Photon 23). **Without any location Apple often returns nothing** (6 of 26 in the top five), which is why the node has a default bias. A fixed far default (Ottawa) still finds Vancouver, Calgary, Halifax addresses.
- **Real post-ride page, typing "100 queen":** Photon puts Hampton University (Virginia) first, Toronto City Hall second. Apple puts 100 Queen St W first.
- **Messy input (11 cases):** Apple 10, Photon 7. Apple finds Montréal streets typed in English; Photon returns the wrong number for a house number that does not exist.
- **Locate Me, 24 points:** same door Apple 15, Photon 22. This **favours Photon**: the reference points are OpenStreetMap's own data, which is Photon's data. A real-phone test is still needed. Apple often returns the landmark name ("City Hall, Toronto ON") without a street.
- **Routes (7):** distance within 5% of OSRM on 4; roads drawn match (90% of points within 200 m) on 5. Highway times 18-21% shorter than OSRM's; city times 60-250% longer. Driver detour by matrix and by route-with-stop agree within 0.1 km. Medians: Apple route 1.46 s (OSRM 0.78 s), autocomplete 454 ms (Photon 731), reverse 367 ms (Photon 725).
- **Map display:** Leaflet ready in 2.5 s (575 KB desktop); MapKit 5.2 s (3.4 MB desktop, 1.6 MB phone). Both no console errors.
- **OSM tiles:** 24 of 24 answered, median 108 ms.
- **US (N3), 11 buildings:** geocode 11 of 11 (one landed in the wrong borough: "350 5th Ave" gave Brooklyn). Autocomplete with only the default location: 8 first, 9 in the top five (misses: White House, Empire State, Boston City Hall). Reverse works in Brooklyn, Honolulu, Anchorage.
- **Policies read on 22 Nov 2026:** OSM tiles: best-effort, no SLA, commercial services may be blocked without notice. **OSRM demo server: restricted to non-commercial use, at most 1 request per second, no guarantees** (Komüte is a business: replace it whatever else is decided). Photon public server: reasonable use only, may throttle or ban, no guarantee; self-host for volume.
- **Apple limits:** 25,000 service calls a day per team (Server API and MapKit JS share it), 250,000 map views a day; over the limit Apple answers 429. Developer Program is $99 a year. Price of more capacity unknown (quota request form).

Reports: `docs/Map_Stack_Test_Report.pdf` (12 pages, git-ignored); artifacts `https://claude.ai/artifact/9j3tK9iPptPC8ae44tJhyh` (dashboard) and `https://claude.ai/artifact/HASM2AbZDgsCjPegWdLxF8` (Apple vs TomTom results; it still shows TomTom results).

## Gotchas learned
- `APPLE_MAPS_WEB_ORIGIN` needs the scheme (`https://komute.ca`); MapKit rejects a bare domain with "Origin does not match".
- The node's coordinate box is loose (it also covers Mexico). The country Apple returns decides (`OUTSIDE_SERVICE_AREA` on reverse).
- Tests: always `--settings=config.settings_regression`. A hand-made SQLite setup without it left 31 unrelated tests failing (axes, social apps, queued email). A clean `git archive` copy needs an empty `logs/` folder to start.
- `make_driver_with_vehicle` builds the plate from the first four letters of the email; two drivers with the same prefix collide on the unique plate.
- Apple has no waypoint parameter and no traffic-delay field on `/v1/directions`; `/v1/etas` returns expected and static times (traffic delay = the difference).
- Only the shell tool is needed to drive Chrome: `map/qa/cdp.mjs` talks to headless Chrome over the DevTools protocol (Node 24). Chrome's `--remote-debugging-port` is used; no Playwright installed. A PDF page is rendered by loading it in Chrome (`qa/pdfshot.mjs`).
- The Apple `.p8` key file sits in the repo root. `*.p8` is git-ignored, but the folder is under OneDrive Desktop, so the file syncs to the cloud. Consider moving it outside the synced folder and updating `APPLE_MAPS_PRIVATE_KEY_PATH`.

## How to run things (from the repo root; prefix Python with `uv run --no-project --python .venv`)
```text
python -m uvicorn backend.app.main:create_app --factory --app-dir map --host 127.0.0.1 --port 8081 --workers 1   # the node
python map/qa/lab_server.py                      # test pages + node on :8765 (MapKit origin http://localhost:8765)
python map/qa/seed_real_pages.py                 # REAL site on throwaway SQLite at :8770 (MAP_AUTOCOMPLETE/ROUTES/DISPLAY env vars to test the switches)
node map/qa/e2e_apple.mjs                        # from map/: e2e of the real pages with all switches on Apple; node needs APPLE_MAPS_WEB_ORIGIN=http://127.0.0.1:8770
node map/qa/real_pages.mjs                       # screenshots of the real pages (SHOT_PREFIX=after for the Apple run)
python map/qa/t2_tiles.py; python map/qa/t3_search.py; python map/qa/t3_rules.py; python map/qa/t4_routes.py; python map/qa/n3_us.py
node map/qa/t1_map_display.mjs; node map/qa/t3_ui.mjs                      # need the lab server running
python map/qa/summarize.py; python map/qa/build_dashboard.py; python map/qa/build_pdf.py   # reports from qa/out/*.json
```
Each Apple test run uses roughly 40 to 250 calls of the 25,000 daily allowance.

## Test status when this was written
- `map`: 91 passed. Release 1 Django tests (`komute.tests_map`): 23 passed.
- **Full run on the project's own settings** (`python manage.py test komute api --settings=config.settings_regression`, throwaway secrets exported): **735 tests, all OK** (23 Nov 2026, about 10 minutes). The 31 failures seen earlier were only an artifact of a homemade SQLite setup.

## Next session, in order
1. Read `CLAUDE.md`, this file. Ask whether the gates are settled and whether Release 1 is live.
2. Start Release 2 with a plan and the switch off by default. Re-run the real-page check (`real_pages.mjs`) before and after.
3. Then Release 3, then cleanup.
