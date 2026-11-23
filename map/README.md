# map (Apple Maps node)

Second process next to `verification_node`. The main app calls it over HTTP with `X-API-Key`; it holds the Apple Maps key.
Serves the US and Canada (`MAP_COUNTRIES`, default `CA,US`). When a request carries no location, autocomplete uses `MAP_DEFAULT_BIAS` (default Ottawa, `45.4215,-75.6972`); without any location Apple often answers a full typed address with nothing.
The main app calls it through `komute/services/map_client.py`; see the "Map node" section of the root README.

Run:  `uvicorn backend.app.main:create_app --factory --app-dir map --port 8081 --workers 1`
Test: `cd map && python -m pytest -q`  (no network; Apple is faked)

| Call | Does |
|---|---|
| `GET /v1/places/suggest?q=&lat=&lon=&limit=` | Autocomplete. Send the rider's lat/lon. Each row has an opaque `ref`. |
| `GET /v1/places/details?ref=` | The picked suggestion: coordinates and structured address. |
| `GET /v1/places/geocode?q=` | Typed address to coordinates. |
| `GET /v1/places/reverse?lat=&lon=` | Nearest address ("Locate Me", dragged pin). |
| `POST /v1/route` | `{origin, destination, waypoints[]}` to distance, duration, legs, line. One Apple call per leg. |
| `POST /v1/matrix` | `{origins[], destinations[]}` to `rows[i][j]`. One Apple call per origin per 10 destinations. |
| `GET /v1/mapkit-token` | Short-lived browser token for MapKit JS, locked to `APPLE_MAPS_WEB_ORIGIN`. |
| `GET /health` | 200 when Apple credentials are loaded, else 503. No key needed. |

## Apple credentials (developer.apple.com, signed in with the paid account)
Steps follow Apple's guide "Creating a Maps identifier and a private key".
1. **Team ID:** Account, Membership details. 10 characters. Goes in `APPLE_MAPS_TEAM_ID`.
2. **Maps ID:** Certificates, Identifiers & Profiles, Identifiers, the + button, tick **Maps IDs**, Continue. Description: `Komute`. Identifier: reverse-domain style that **starts with `maps.`**, for example `maps.ca.komute`. Continue, Register.
3. **Key:** Keys, the + button. Name it `komute-maps`. Tick **MapKit JS** (it stays greyed out until a Maps ID exists), then Configure, pick the Maps ID from step 2, Save, Continue, Register. Click **Download** once: Apple shows the `.p8` only one time.
4. **Key ID:** shown on the key's page, 10 characters. Goes in `APPLE_MAPS_KEY_ID`.
5. Put the `.p8` outside the repo. Set `APPLE_MAPS_PRIVATE_KEY_PATH` to its full path. Never commit it.
6. Set `APPLE_MAPS_WEB_ORIGIN` to the site origin **with the scheme and no trailing slash**, for example `https://komute.ca`. MapKit compares it with the page's origin and rejects a bare domain.

Use a separate Maps ID and key for each environment (dev, production), so revoking one key leaves the other working.

## Not verified against live Apple yet
Shape of `stepPaths`, `transportType=Automobile`, whether autocomplete rows always carry a location, how Apple reports traffic in `/v1/etas`,
and whether Apple's license allows saving ride coordinates (see Schedule 6 of the Developer Program License Agreement).
