import base64
import time

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.core.errors import UpstreamError
from backend.core.providers import apple as apple_mod
from backend.core.providers.apple import AppleMaps, _decode_ref, _encode_ref
from tests.fixtures import samples

_KEY = ec.generate_private_key(ec.SECP256R1())
PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
PUB = _KEY.public_key()
TEAM, KID, ORIGIN = "TEAM123456", "KEY1234567", "https://komute.test"
ACCESS = "ACCESS-TOKEN-SECRET"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Answers by path. `script[path]` is a list of responses used in order; the last one repeats."""

    def __init__(self, script=None, exc=None):
        self.script = {"/v1/token": [Resp(200, {"accessToken": ACCESS, "expiresInSeconds": 1800})], **(script or {})}
        self.exc, self.calls = exc, []

    def request(self, method, url, **kw):
        path = url.removeprefix(apple_mod.BASE)
        self.calls.append({"method": method, "url": url, "path": path.split("?")[0], **kw})
        if self.exc and path != "/v1/token":
            raise self.exc
        queue = self.script[path.split("?")[0]]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def data_calls(self):
        return [c for c in self.calls if c["path"] != "/v1/token"]


class Clock:
    def __init__(self):
        self.t = time.time()  # a real clock, so the JWT library accepts the issued-at

    def __call__(self):
        return self.t


def client(script=None, exc=None, pem=PEM):
    s, clock = FakeSession(script, exc), Clock()
    return AppleMaps(TEAM, KID, pem, ORIGIN, session=s, clock=clock), s, clock


# ---- auth ---------------------------------------------------------------------------------------------------
def test_server_token_is_signed_and_exchanged_once():
    a, s, _ = client({"/v1/geocode": [Resp(200, samples.GEOCODE)]})
    a.geocode("x", None, None, 3)
    a.geocode("y", None, None, 3)
    tok = [c for c in s.calls if c["path"] == "/v1/token"]
    assert len(tok) == 1  # the access token is cached
    bearer = tok[0]["headers"]["Authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(bearer) == {"alg": "ES256", "kid": KID, "typ": "JWT"}
    claims = jwt.decode(bearer, PUB, algorithms=["ES256"])
    assert claims["iss"] == TEAM and claims["scope"] == "server_api" and claims["exp"] - claims["iat"] == 1800
    assert all(c["headers"]["Authorization"] == f"Bearer {ACCESS}" for c in s.data_calls())


def test_access_token_is_refreshed_before_it_expires():
    a, s, clock = client({"/v1/geocode": [Resp(200, samples.GEOCODE)]})
    a.geocode("x", None, None, 3)
    clock.t += 1800 - 30  # inside the 60 s safety margin
    a.geocode("x", None, None, 3)
    assert len([c for c in s.calls if c["path"] == "/v1/token"]) == 2


def test_a_401_refreshes_the_token_once_and_retries():
    a, s, _ = client({"/v1/geocode": [Resp(401, {}), Resp(200, samples.GEOCODE)]})
    assert a.geocode("x", None, None, 3)
    assert len([c for c in s.calls if c["path"] == "/v1/token"]) == 2


def test_two_401s_in_a_row_is_an_auth_error():
    a, _, _ = client({"/v1/geocode": [Resp(401, {})]})
    with pytest.raises(UpstreamError) as e:
        a.geocode("x", None, None, 3)
    assert (e.value.status, e.value.code) == (502, "UPSTREAM_AUTH")


def test_a_403_is_an_auth_error_without_retry():
    a, s, _ = client({"/v1/geocode": [Resp(403, {})]})
    with pytest.raises(UpstreamError) as e:
        a.geocode("x", None, None, 3)
    assert e.value.code == "UPSTREAM_AUTH" and len(s.data_calls()) == 1


def test_token_endpoint_refusal_is_an_auth_error_not_a_crash():
    a, _, _ = client({"/v1/token": [Resp(401, {})]})
    with pytest.raises(UpstreamError) as e:
        a.geocode("x", None, None, 3)
    assert (e.value.status, e.value.code) == (502, "UPSTREAM_AUTH")


def test_unreadable_token_answer_is_a_502():
    a, _, _ = client({"/v1/token": [Resp(200, {"nope": 1})]})
    with pytest.raises(UpstreamError) as e:
        a.geocode("x", None, None, 3)
    assert e.value.status == 502


def test_bad_private_key_is_an_auth_error_that_does_not_echo_the_key():
    a, _, _ = client(pem="-----BEGIN PRIVATE KEY-----\nSECRETBITS\n-----END PRIVATE KEY-----")
    with pytest.raises(UpstreamError) as e:
        a.geocode("x", None, None, 3)
    assert e.value.code == "UPSTREAM_AUTH" and "SECRETBITS" not in e.value.message


def test_browser_token_is_locked_to_our_domain_and_has_no_server_scope():
    a, _, _ = client()
    token, ttl = a.mapkit_token()
    claims = jwt.decode(token, PUB, algorithms=["ES256"])
    assert ttl == 1800 and claims["origin"] == ORIGIN and claims["iss"] == TEAM and "scope" not in claims
    assert jwt.get_unverified_header(token)["kid"] == KID


# ---- places -------------------------------------------------------------------------------------------------
def test_suggest_builds_the_request_and_keeps_only_resolvable_rows():
    a, s, _ = client({"/v1/searchAutocomplete": [Resp(200, samples.AUTOCOMPLETE)]})
    out = a.suggest("100 queen", 43.65, -79.38, 5)
    p = s.data_calls()[0]["params"]
    assert p["q"] == "100 queen" and p["limitToCountries"] == "CA,US" and p["userLocation"] == p["searchLocation"] == "43.65,-79.38"
    assert [r["title"] for r in out] == ["100 Queen St W", "Tim Hortons"]  # no url / foreign host / wrong api: dropped
    assert out[0]["subtitle"] == "Toronto, ON, Canada" and (out[0]["lat"], out[0]["lon"]) == (43.6528, -79.3836)
    assert out[1]["lat"] is None  # a row without a location is still offered; details resolves it
    assert _decode_ref(out[0]["ref"]) == samples.COMPLETION


def test_suggest_without_location_sends_no_hint_and_respects_limit():
    a, s, _ = client({"/v1/searchAutocomplete": [Resp(200, samples.AUTOCOMPLETE)]})
    assert len(a.suggest("x", None, None, 1)) == 1
    assert "userLocation" not in s.data_calls()[0]["params"]


def test_details_calls_only_the_encoded_search_url():
    a, s, _ = client({"/v1/search": [Resp(200, samples.SEARCH)]})
    place = a.details(_encode_ref(samples.COMPLETION))
    assert s.data_calls()[0]["url"] == apple_mod.BASE + samples.COMPLETION
    assert place["address"] == {"house_number": "100", "street": "Queen St W", "city": "Toronto", "province": "ON",
                                "postal_code": "M5H 2N1", "country": "CA"}
    assert place["type"] == "address"


@pytest.mark.parametrize("url", ["/v1/token", "https://evil.example/v1/search?q=x", "//evil.example/v1/search?q=x",
                                 "/v1/search?q=a b", "/v1/search?q=a\\b", "/v1/search?q=a#frag", "/v1/search?" + "a" * 1600])
def test_details_refuses_refs_that_could_aim_elsewhere(url):
    a, s, _ = client()
    with pytest.raises(UpstreamError) as e:
        a.details(_encode_ref(url))
    assert e.value.status == 404 and s.calls == []  # nothing was sent, not even a token request


def test_details_refuses_garbage_refs():
    a, s, _ = client()
    for ref in ("!!!notbase64!!!", base64.urlsafe_b64encode(b"\xff\xfe").decode(), ""):
        with pytest.raises(UpstreamError):
            a.details(ref)
    assert s.calls == []


def test_geocode_normalises_and_drops_rows_without_a_coordinate():
    a, s, _ = client({"/v1/geocode": [Resp(200, samples.GEOCODE)]})
    out = a.geocode("100 queen", 43.65, -79.38, 5)
    p = s.data_calls()[0]["params"]
    assert p == {"q": "100 queen", "limitToCountries": "CA,US", "searchLocation": "43.65,-79.38"}  # latitude first
    assert len(out) == 1 and (out[0]["lat"], out[0]["lon"]) == (43.6528, -79.3836) and out[0]["title"] == "100 Queen St W"


def test_geocode_respects_limit():
    many = {"results": [samples.PLACE] * 4}
    a, _, _ = client({"/v1/geocode": [Resp(200, many)]})
    assert len(a.geocode("x", None, None, 2)) == 2


def test_reverse_uses_loc_and_maps_a_400_to_not_found():
    a, s, _ = client({"/v1/reverseGeocode": [Resp(200, samples.REVERSE)]})
    assert a.reverse(43.65, -79.38)["id"] == "I1" and s.data_calls()[0]["params"] == {"loc": "43.65,-79.38"}
    b, _, _ = client({"/v1/reverseGeocode": [Resp(400, {})]})
    with pytest.raises(UpstreamError) as e:
        b.reverse(43.65, -79.38)
    assert (e.value.status, e.value.code) == (404, "NOT_FOUND")
    c, _, _ = client({"/v1/reverseGeocode": [Resp(200, {"results": []})]})
    assert c.reverse(43.65, -79.38) is None


# ---- routing ------------------------------------------------------------------------------------------------
O, W, D = {"lat": 43.6, "lon": -79.4}, {"lat": 43.62, "lon": -79.38}, {"lat": 43.63, "lon": -79.37}


def test_route_chains_one_leg_per_stop_and_joins_the_line():
    a, s, _ = client({"/v1/directions": [Resp(200, samples.DIRECTIONS_A), Resp(200, samples.DIRECTIONS_B)]})
    out = a.route(O, D, [W])
    calls = s.data_calls()
    assert [(c["params"]["origin"], c["params"]["destination"]) for c in calls] == [("43.6,-79.4", "43.62,-79.38"), ("43.62,-79.38", "43.63,-79.37")]
    assert out["distance_m"] == 5000 and out["duration_s"] == 600 and out["traffic_delay_s"] is None
    assert out["legs"] == [{"distance_m": 2000, "duration_s": 240}, {"distance_m": 3000, "duration_s": 360}]
    assert out["geometry"] == [[-79.4, 43.6], [-79.39, 43.61], [-79.38, 43.62], [-79.37, 43.63]]  # [lon, lat], no repeated joints


def test_route_without_stops_is_one_call():
    a, s, _ = client({"/v1/directions": [Resp(200, samples.DIRECTIONS_A)]})
    a.route(O, W, [])
    assert len(s.data_calls()) == 1


def test_route_with_no_route_is_422():
    a, _, _ = client({"/v1/directions": [Resp(200, {"routes": []})]})
    with pytest.raises(UpstreamError) as e:
        a.route(O, D, [])
    assert (e.value.status, e.value.code) == (422, "NO_ROUTE")
    b, _, _ = client({"/v1/directions": [Resp(400, {})]})
    with pytest.raises(UpstreamError) as e2:
        b.route(O, D, [])
    assert e2.value.code == "NO_ROUTE"


def test_route_survives_a_missing_step_path():
    broken = {"routes": [{"distanceMeters": 10, "durationSeconds": 5, "stepIndexes": [0, 7]}],
              "steps": [{"stepPathIndex": 0}], "stepPaths": [[{"latitude": 1.0, "longitude": 2.0}]]}
    a, _, _ = client({"/v1/directions": [Resp(200, broken)]})
    assert a.route(O, D, [])["geometry"] == [[2.0, 1.0]]


# ---- matrix -------------------------------------------------------------------------------------------------
def test_matrix_sends_destinations_in_chunks_of_ten_per_origin():
    dests = [{"lat": 43.0 + i / 100, "lon": -79.0} for i in range(12)]
    a, s, _ = client({"/v1/etas": [Resp(200, samples.ETAS_FULL), Resp(200, {"etas": []})]})
    rows = a.matrix([O], dests)
    calls = s.data_calls()
    assert len(calls) == 2 and calls[0]["params"]["destinations"].count("|") == 9 and calls[1]["params"]["destinations"].count("|") == 1
    assert calls[0]["params"]["origin"] == "43.6,-79.4"
    assert len(rows) == 1 and len(rows[0]) == 12


def test_matrix_reads_traffic_as_expected_minus_static_and_leaves_gaps_empty():
    a, _, _ = client({"/v1/etas": [Resp(200, samples.ETAS_FULL)]})
    row = a.matrix([O], [W, D])[0]
    assert row[0] == {"distance_m": 1000, "duration_s": 125, "traffic_delay_s": 5}
    assert row[1] is None  # Apple gave nothing for that destination


# ---- errors -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,expected", [(429, (503, "UPSTREAM_BUSY")), (500, (502, "UPSTREAM_ERROR")), (404, (502, "UPSTREAM_ERROR"))])
def test_upstream_status_mapping(status, expected):
    a, _, _ = client({"/v1/geocode": [Resp(status, {"error": {"message": ACCESS}})]})
    with pytest.raises(UpstreamError) as e:
        a.geocode("x", None, None, 1)
    assert (e.value.status, e.value.code) == expected and ACCESS not in e.value.message


@pytest.mark.parametrize("exc,expected", [(requests.Timeout(), (504, "UPSTREAM_TIMEOUT")),
                                          (requests.ConnectionError(f"https://x/?token={ACCESS}"), (502, "UPSTREAM_UNAVAILABLE"))])
def test_transport_errors_never_leak_secrets(exc, expected):
    a, _, _ = client(exc=exc)
    with pytest.raises(UpstreamError) as e:
        a.geocode("x", None, None, 1)
    assert (e.value.status, e.value.code) == expected
    assert ACCESS not in e.value.message and ACCESS not in repr(e.value.__cause__)


class FlakySession(FakeSession):
    """The first data call fails with `exc`, as a keep-alive connection the far end has closed does; the rest answer."""

    def request(self, method, url, **kw):
        if self.exc and url.removeprefix(apple_mod.BASE) != "/v1/token":
            exc, self.exc = self.exc, None
            self.calls.append({"method": method, "url": url, "path": "failed"})
            raise exc
        return super().request(method, url, **kw)


def test_a_dropped_connection_is_retried_once():
    s = FlakySession({"/v1/geocode": [Resp(200, samples.GEOCODE)]}, exc=requests.ConnectionError("connection reset"))
    a = AppleMaps(TEAM, KID, PEM, ORIGIN, session=s, clock=Clock())
    assert a.geocode("x", None, None, 1)
    assert [c["path"] for c in s.calls if c["path"] != "/v1/token"] == ["failed", "/v1/geocode"]


def test_a_timeout_is_not_retried():
    a, s, _ = client(exc=requests.Timeout())
    with pytest.raises(UpstreamError):
        a.geocode("x", None, None, 1)
    assert len(s.data_calls()) == 1


def test_unreadable_answer_is_a_502():
    a, _, _ = client({"/v1/geocode": [Resp(200, None)]})
    with pytest.raises(UpstreamError) as e:
        a.geocode("x", None, None, 1)
    assert e.value.status == 502


def test_place_has_a_ready_label_without_the_country():
    a, _, _ = client({"/v1/geocode": [Resp(200, samples.GEOCODE)]})
    assert a.geocode("x", None, None, 1)[0]["label"] == "100 Queen St W, Toronto ON M5H 2N1"
    landmark = {"results": [{"name": "City Hall", "country": "Canada", "coordinate": {"latitude": 43.65, "longitude": -79.38},
                             "formattedAddressLines": ["City Hall", "Toronto ON", "Canada"],
                             "structuredAddress": {"locality": "Toronto", "administrativeAreaCode": "ON"}}]}
    b, _, _ = client({"/v1/reverseGeocode": [Resp(200, landmark)]})
    assert b.reverse(43.65, -79.38)["label"] == "City Hall, Toronto ON"


# ---- N1: default bias when the caller sends no location ---------------------------------------------------------------
def test_suggest_uses_the_default_bias_when_no_location_is_sent():
    s = FakeSession({"/v1/searchAutocomplete": [Resp(200, samples.AUTOCOMPLETE)]})
    a = AppleMaps(TEAM, KID, PEM, ORIGIN, session=s, clock=Clock(), default_bias=(45.4215, -75.6972))
    a.suggest("100 queen street west toronto", None, None, 5)
    p = s.data_calls()[0]["params"]
    assert p["userLocation"] == p["searchLocation"] == "45.4215,-75.6972"


def test_a_real_location_beats_the_default_bias():
    s = FakeSession({"/v1/searchAutocomplete": [Resp(200, samples.AUTOCOMPLETE)]})
    a = AppleMaps(TEAM, KID, PEM, ORIGIN, session=s, clock=Clock(), default_bias=(45.4215, -75.6972))
    a.suggest("100 queen", 49.28, -123.12, 5)
    assert s.data_calls()[0]["params"]["searchLocation"] == "49.28,-123.12"


def test_geocode_does_not_use_the_default_bias():
    s = FakeSession({"/v1/geocode": [Resp(200, samples.GEOCODE)]})
    a = AppleMaps(TEAM, KID, PEM, ORIGIN, session=s, clock=Clock(), default_bias=(45.4215, -75.6972))
    a.geocode("100 queen street west toronto", None, None, 3)
    assert "searchLocation" not in s.data_calls()[0]["params"]  # a typed full address needs no hint, and a wrong hint could mislead it


# ---- N2: the country list is a setting ----------------------------------------------------------------------------------
def test_countries_come_from_the_setting():
    s = FakeSession({"/v1/geocode": [Resp(200, samples.GEOCODE)], "/v1/searchAutocomplete": [Resp(200, samples.AUTOCOMPLETE)]})
    a = AppleMaps(TEAM, KID, PEM, ORIGIN, session=s, clock=Clock(), countries="CA")
    a.geocode("x", None, None, 1)
    a.suggest("x", None, None, 1)
    assert [c["params"]["limitToCountries"] for c in s.data_calls()] == ["CA", "CA"]
