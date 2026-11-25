import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.core.config import Settings
from backend.core.errors import UpstreamError

KEY = "test-map-key-123"
H = {"X-API-Key": KEY}


class FakeProvider:
    def __init__(self):
        self.calls, self.raises = [], None

    def _hit(self, name, *a):
        self.calls.append((name, *a))
        if self.raises:
            raise self.raises

    def suggest(self, q, lat, lon, limit):
        self._hit("suggest", q, lat, lon, limit)
        return [{"ref": "addresses/A1", "type": "address", "title": "100 Queen St W", "subtitle": "Toronto"}]

    def details(self, ref):
        self._hit("details", ref)
        return None if ref.endswith("MISSING") else {"id": "A1", "lat": 43.65, "lon": -79.38, "address": {}}

    def mapkit_token(self):
        self._hit("mapkit_token")
        return "jwt.token.value", 1800

    def geocode(self, q, lat, lon, limit):
        self._hit("geocode", q)
        return [{"id": "A1", "lat": 43.65, "lon": -79.38}]

    def reverse(self, lat, lon):
        self._hit("reverse", lat, lon)
        if lat > 80:
            return None
        return {"id": "A1", "lat": lat, "lon": lon, "address": {"country": "MX" if lat < 30 else ("US" if lat < 43 else "CA")}}

    def route(self, o, d, w):
        self._hit("route", o, d, w)
        return {"distance_m": 1, "duration_s": 2, "traffic_delay_s": 0, "legs": [], "geometry": []}

    def matrix(self, o, d):
        self._hit("matrix", o, d)
        return [[None] * len(d) for _ in o]


@pytest.fixture
def prov():
    return FakeProvider()


@pytest.fixture
def api(prov):
    return TestClient(create_app(Settings(_env_file=None, api_key=KEY, apple_web_origin="https://komute.test"), prov))


def pt(lat=43.65, lon=-79.38):
    return {"lat": lat, "lon": lon}


def test_every_data_route_needs_the_key(api):
    for method, url in [("get", "/v1/places/suggest?q=a"), ("get", "/v1/places/details?ref=abcdefgh1"), ("get", "/v1/mapkit-token"),
                        ("get", "/v1/places/geocode?q=a"), ("get", "/v1/places/reverse?lat=43&lon=-79"),
                        ("post", "/v1/route"), ("post", "/v1/matrix")]:
        r = getattr(api, method)(url)
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED", url
        assert getattr(api, method)(url, headers={"X-API-Key": "wrong"}).status_code == 401
    assert api.get("/health").status_code == 200  # health needs no key


def test_suggest_ok_and_cached(api, prov):
    for _ in range(3):
        r = api.get("/v1/places/suggest", params={"q": " 100 Queen ", "lat": 43.65, "lon": -79.38}, headers=H)
        assert r.status_code == 200 and r.json()["results"][0]["ref"] == "addresses/A1"
    assert [c[0] for c in prov.calls] == ["suggest"]  # one upstream call for three identical requests
    assert prov.calls[0][1] == "100 Queen"


@pytest.mark.parametrize("url", [
    "/v1/places/suggest",                                     # q missing
    "/v1/places/suggest?q=",                                  # empty
    "/v1/places/suggest?q=%20%20",                            # blank
    "/v1/places/suggest?q=" + "a" * 201,                      # too long
    "/v1/places/suggest?q=a&lat=43.6",                        # lat without lon
    "/v1/places/suggest?q=a&lat=51.5&lon=-0.12",              # London: outside the box
    "/v1/places/suggest?q=a&limit=11",
    "/v1/places/details?ref=../../etc/passwd",
    "/v1/places/details?ref=pois/a%2Fb",
    "/v1/places/reverse?lat=abc&lon=-79",
])
def test_bad_input_is_422_and_never_echoed(api, prov, url):
    r = api.get(url, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_FIELD"
    assert "passwd" not in r.text
    assert prov.calls == []


def test_details_found_and_missing(api):
    assert api.get("/v1/places/details", params={"ref": "refGoodRef1"}, headers=H).json()["id"] == "A1"
    r = api.get("/v1/places/details", params={"ref": "refMissingMISSING"}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_reverse_not_found(api):
    assert api.get("/v1/places/reverse", params={"lat": 43.6, "lon": -79.4}, headers=H).status_code == 200
    assert api.get("/v1/places/reverse", params={"lat": 83.0, "lon": -79.4}, headers=H).status_code == 404


def test_route_ok_and_waypoint_limit(api, prov):
    body = {"origin": pt(), "destination": pt(43.7, -79.4), "waypoints": [pt()]}
    assert api.post("/v1/route", json=body, headers=H).status_code == 200
    assert prov.calls[-1][3] == [pt()]
    body["waypoints"] = [pt()] * 6
    r = api.post("/v1/route", json=body, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "TOO_MANY_WAYPOINTS"


@pytest.mark.parametrize("body", [
    {"origin": pt(), "destination": pt(51.5, -0.12)},                  # London: outside the box
    {"origin": pt(), "destination": pt(), "extra": 1},                  # unknown field
    {"origin": pt()},                                                   # missing destination
    {"origin": {"lat": "x", "lon": 1}, "destination": pt()},
])
def test_route_rejects_bad_bodies(api, body):
    assert api.post("/v1/route", json=body, headers=H).status_code == 422


def test_matrix_size_cap(api):
    ok = {"origins": [pt()] * 10, "destinations": [pt()] * 10}
    assert api.post("/v1/matrix", json=ok, headers=H).json()["rows"][0] == [None] * 10
    big = {"origins": [pt()] * 11, "destinations": [pt()] * 10}
    r = api.post("/v1/matrix", json=big, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "MATRIX_TOO_LARGE"
    assert api.post("/v1/matrix", json={"origins": [], "destinations": [pt()]}, headers=H).status_code == 422


@pytest.mark.parametrize("err,status,code", [
    (UpstreamError(503, "UPSTREAM_BUSY", "busy", retry_after=7), 503, "UPSTREAM_BUSY"),
    (UpstreamError(504, "UPSTREAM_TIMEOUT", "slow"), 504, "UPSTREAM_TIMEOUT"),
    (UpstreamError(422, "NO_ROUTE", "none"), 422, "NO_ROUTE"),
])
def test_upstream_errors_pass_through_cleanly(api, prov, err, status, code):
    prov.raises = err
    r = api.post("/v1/route", json={"origin": pt(), "destination": pt()}, headers=H)
    assert r.status_code == status and r.json()["error"]["code"] == code
    if status == 503:
        assert r.headers["Retry-After"] == "7"


def test_unexpected_error_is_generic_500(prov):
    prov.raises = RuntimeError("secret internals")
    api = TestClient(create_app(Settings(_env_file=None, api_key=KEY, apple_web_origin="https://komute.test"), prov), raise_server_exceptions=False)
    r = api.get("/v1/places/geocode", params={"q": "x"}, headers=H)
    assert r.status_code == 500 and "secret" not in r.text


def test_no_provider_key_answers_503_not_configured():
    c = TestClient(create_app(Settings(_env_file=None, api_key=KEY)))
    r = c.get("/v1/places/geocode", params={"q": "x"}, headers=H)
    assert r.status_code == 503 and r.json()["error"]["code"] == "NOT_CONFIGURED"
    assert c.get("/health").status_code == 503


def test_open_when_no_api_key_set(prov):
    c = TestClient(create_app(Settings(_env_file=None, api_key=None, apple_web_origin="https://komute.test"), prov))
    assert c.get("/v1/places/geocode", params={"q": "x"}).status_code == 200


def test_security_headers_and_no_docs(api):
    r = api.get("/health")
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert api.get("/docs").status_code == 404 and api.get("/openapi.json").status_code == 404


def test_mapkit_token_route(api, prov):
    r = api.get("/v1/mapkit-token", headers=H)
    assert r.status_code == 200 and r.json() == {"token": "jwt.token.value", "expires_in": 1800}


def test_mapkit_token_needs_a_web_origin(prov):
    c = TestClient(create_app(Settings(_env_file=None, api_key=KEY), prov))
    r = c.get("/v1/mapkit-token", headers=H)
    assert r.status_code == 503 and r.json()["error"]["code"] == "NOT_CONFIGURED"


def test_matrix_origin_cap(api):
    r = api.post("/v1/matrix", json={"origins": [pt()] * 21, "destinations": [pt()]}, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "MATRIX_TOO_LARGE"


def test_reverse_refuses_a_point_apple_places_outside_the_service_area(api):
    r = api.get("/v1/places/reverse", params={"lat": 25.0, "lon": -100.0}, headers=H)  # inside the box, but Apple says Mexico
    assert r.status_code == 422 and r.json()["error"]["code"] == "OUTSIDE_SERVICE_AREA"
    assert api.get("/v1/places/reverse", params={"lat": 43.65, "lon": -79.38}, headers=H).status_code == 200  # Canada
    assert api.get("/v1/places/reverse", params={"lat": 40.7, "lon": -74.0}, headers=H).status_code == 200    # US


@pytest.mark.parametrize("lat,lon", [(40.71, -74.0), (47.6, -122.3), (21.3, -157.86), (61.2, -149.9), (43.65, -79.38)])  # New York, Seattle, Honolulu, Anchorage, Toronto
def test_us_and_canadian_points_are_accepted(api, lat, lon):
    assert api.get("/v1/places/suggest", params={"q": "a", "lat": lat, "lon": lon}, headers=H).status_code == 200
    assert api.post("/v1/route", json={"origin": pt(lat, lon), "destination": pt(lat, lon)}, headers=H).status_code == 200
