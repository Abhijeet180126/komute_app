"""Apple Maps Server API client. One class, six calls, plus the browser (MapKit JS) token.

The only module that knows Apple's URLs, auth and field names. Every call returns plain dicts in Komute's own shape.
It never logs a URL, a token or an upstream body. Docs: developer.apple.com/documentation/applemapsserverapi
"""
from __future__ import annotations

import base64
import binascii
import logging
import threading
import time
from typing import Any

import jwt
import requests

from backend.core.errors import UpstreamError

log = logging.getLogger("komute.map.apple")

BASE = "https://maps-api.apple.com"
DEFAULT_COUNTRIES = "CA,US"  # overridden by MAP_COUNTRIES
TRANSPORT = "Automobile"
ETA_CHUNK = 10  # /v1/etas takes at most 10 destinations per call
REF_PREFIX = "/v1/search?"
SERVER_TOKEN_TTL_S = 1800
BROWSER_TOKEN_TTL_S = 1800
TOKEN_SKEW_S = 60


def _ll(p: dict[str, float]) -> str:
    return f'{p["lat"]},{p["lon"]}'  # Apple wants latitude first


def _place(r: dict[str, Any]) -> dict[str, Any] | None:
    c = r.get("coordinate") or r.get("location") or {}
    if not isinstance(c.get("latitude"), (int, float)) or not isinstance(c.get("longitude"), (int, float)):
        return None
    sa = r.get("structuredAddress") or {}
    lines = r.get("formattedAddressLines") or r.get("displayLines") or []
    address = {
        "house_number": sa.get("subThoroughfare"), "street": sa.get("thoroughfare"), "city": sa.get("locality"),
        "province": sa.get("administrativeAreaCode") or sa.get("administrativeArea"), "postal_code": sa.get("postCode"),
        "country": r.get("countryCode"),
    }
    # Ready-to-show text for the address box: Apple's own lines without the country ("City Hall, Toronto ON").
    label = ", ".join(x for x in lines if x and x != r.get("country"))
    return {"id": r.get("id"), "type": "address" if sa.get("subThoroughfare") else "place",
            "title": r.get("name") or (lines[0] if lines else None), "label": label or None,
            "lat": c["latitude"], "lon": c["longitude"], "address": {k: v for k, v in address.items() if v}}


def _encode_ref(completion_url: str) -> str:
    return base64.urlsafe_b64encode(completion_url.encode()).decode().rstrip("=")


def _decode_ref(ref: str) -> str | None:
    try:
        url = base64.urlsafe_b64decode(ref + "=" * (-len(ref) % 4)).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    # Only a query on Apple's own /v1/search is allowed: the host is fixed, so the ref can never aim a request elsewhere.
    if not url.startswith(REF_PREFIX) or any(c in url for c in " \r\n\\#") or len(url) > 1500:
        return None
    return url


class AppleMaps:
    def __init__(self, team_id: str, key_id: str, private_key_pem: str, web_origin: str | None = None,
                 session: Any | None = None, timeout: tuple[float, float] = (3.05, 10.0), clock=time.time,
                 countries: str = DEFAULT_COUNTRIES, default_bias: tuple[float, float] | None = None) -> None:
        self._team, self._kid, self._pem, self._origin = team_id, key_id, private_key_pem, web_origin
        self._s, self._timeout, self._clock = session or requests.Session(), timeout, clock
        self._countries, self._bias = countries, default_bias
        self._lock = threading.Lock()
        self._access: str | None = None
        self._access_exp = 0.0

    # ---- auth -------------------------------------------------------------------------------------------------
    def _sign(self, claims: dict[str, Any]) -> str:
        now = int(self._clock())
        try:
            return jwt.encode({"iss": self._team, "iat": now, **claims}, self._pem, algorithm="ES256", headers={"kid": self._kid})
        except Exception as e:  # a bad .p8 file; never log the message, it can echo key material
            log.error("apple token signing failed type=%s", type(e).__name__)
            raise UpstreamError(502, "UPSTREAM_AUTH", "The map provider refused the request.") from None

    def mapkit_token(self) -> tuple[str, int]:
        """Short-lived token for MapKit JS in the browser, locked to our domain. Never the server token."""
        now = int(self._clock())
        return self._sign({"exp": now + BROWSER_TOKEN_TTL_S, "origin": self._origin}), BROWSER_TOKEN_TTL_S

    def _access_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._access and self._clock() < self._access_exp - TOKEN_SKEW_S:
                return self._access
            token = self._sign({"exp": int(self._clock()) + SERVER_TOKEN_TTL_S, "scope": "server_api"})
            try:
                data = self._send("GET", "/v1/token", headers={"Authorization": f"Bearer {token}"})
            except _Unauthorized:
                log.error("apple refused our signed token: check team id, key id and that the key has the Maps service")
                raise UpstreamError(502, "UPSTREAM_AUTH", "The map provider refused the request.") from None
            access, ttl = data.get("accessToken"), data.get("expiresInSeconds")
            if not isinstance(access, str) or not isinstance(ttl, (int, float)):
                raise UpstreamError(502, "UPSTREAM_ERROR", "The map provider returned an unreadable answer.")
            self._access, self._access_exp = access, self._clock() + ttl
            return access

    # ---- transport --------------------------------------------------------------------------------------------
    def _send(self, method: str, path: str, *, headers: dict[str, str], params: Any = None, raw: str | None = None,
              on_400: UpstreamError | None = None) -> dict[str, Any]:
        url = BASE + (raw if raw is not None else path)
        for attempt in (0, 1):
            try:
                r = self._s.request(method, url, params=params, headers={**headers, "Accept": "application/json"}, timeout=self._timeout)
                break
            except requests.Timeout:
                raise UpstreamError(504, "UPSTREAM_TIMEOUT", "The map provider took too long to answer.") from None
            except requests.RequestException as e:
                # A pooled keep-alive connection Apple has already closed fails at once. Every call here is a GET, so one retry is safe.
                if attempt == 0 and isinstance(e, requests.ConnectionError):
                    log.warning("apple connection dropped, retrying once type=%s", type(e).__name__)
                    continue
                log.error("apple transport error type=%s", type(e).__name__)
                raise UpstreamError(502, "UPSTREAM_UNAVAILABLE", "The map provider could not be reached.") from None
        status = r.status_code
        if status == 429:
            raise UpstreamError(503, "UPSTREAM_BUSY", "The map provider is busy. Try again shortly.", retry_after=5)
        if status in (401, 403):
            raise _Unauthorized(status)
        if status in (400, 404) and on_400 is not None:
            raise on_400
        if status != 200:
            log.error("apple error status=%s path=%s", status, path)
            raise UpstreamError(502, "UPSTREAM_ERROR", "The map provider returned an error.")
        try:
            data = r.json()
        except ValueError:
            raise UpstreamError(502, "UPSTREAM_ERROR", "The map provider returned an unreadable answer.") from None
        return data if isinstance(data, dict) else {}

    def _call(self, path: str, params: dict[str, Any] | None = None, *, raw: str | None = None,
              on_400: UpstreamError | None = None) -> dict[str, Any]:
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._access_token(force=attempt == 1)}"}
            try:
                return self._send("GET", path, headers=headers, params=params, raw=raw, on_400=on_400)
            except _Unauthorized as e:
                if attempt == 1 or e.status == 403:
                    log.error("apple rejected the token or the key lacks the Maps service: status=%s path=%s", e.status, path)
                    raise UpstreamError(502, "UPSTREAM_AUTH", "The map provider refused the request.") from None
        raise AssertionError("unreachable")

    # ---- places -----------------------------------------------------------------------------------------------
    def suggest(self, query: str, lat: float | None, lon: float | None, limit: int) -> list[dict]:
        params: dict[str, Any] = {"q": query, "limitToCountries": self._countries}
        if (lat is None or lon is None) and self._bias:
            lat, lon = self._bias  # Apple answers a full typed address with nothing when it has no location at all
        if lat is not None and lon is not None:
            params["userLocation"] = params["searchLocation"] = f"{lat},{lon}"
        out = []
        for r in (self._call("/v1/searchAutocomplete", params).get("results") or [])[:limit]:
            url, lines = r.get("completionUrl"), r.get("displayLines") or []
            if not url or not lines or _decode_ref(_encode_ref(url)) is None:
                continue  # a row we could not resolve later is not offered
            place = _place(r)
            out.append({"ref": _encode_ref(url), "type": place["type"] if place else "place", "title": lines[0],
                        "subtitle": ", ".join(lines[1:]), "lat": place["lat"] if place else None,
                        "lon": place["lon"] if place else None})
        return out

    def details(self, ref: str) -> dict | None:
        url = _decode_ref(ref)
        if url is None:
            raise UpstreamError(404, "NOT_FOUND", "That place was not found.")
        found = [p for p in map(_place, self._call("/v1/search", raw=url).get("results") or []) if p]
        return found[0] if found else None

    def geocode(self, query: str, lat: float | None, lon: float | None, limit: int) -> list[dict]:
        params: dict[str, Any] = {"q": query, "limitToCountries": self._countries}
        if lat is not None and lon is not None:
            params["searchLocation"] = f"{lat},{lon}"
        results = self._call("/v1/geocode", params).get("results") or []
        return [p for p in map(_place, results) if p][:limit]

    def reverse(self, lat: float, lon: float) -> dict | None:
        data = self._call("/v1/reverseGeocode", {"loc": f"{lat},{lon}"},
                          on_400=UpstreamError(404, "NOT_FOUND", "No address was found near that point."))
        found = [p for p in map(_place, data.get("results") or []) if p]
        return found[0] if found else None

    # ---- routing ----------------------------------------------------------------------------------------------
    def _leg(self, a: dict, b: dict) -> tuple[int, int, list[list[float]]]:
        no_route = UpstreamError(422, "NO_ROUTE", "No drivable route was found between those points.")
        data = self._call("/v1/directions", {"origin": _ll(a), "destination": _ll(b), "transportType": TRANSPORT}, on_400=no_route)
        routes = data.get("routes") or []
        if not routes:
            raise no_route
        route, steps, paths = routes[0], data.get("steps") or [], data.get("stepPaths") or []
        line: list[list[float]] = []
        for i in route.get("stepIndexes") or []:
            try:
                path = paths[steps[i]["stepPathIndex"]]
            except (IndexError, KeyError, TypeError):
                continue
            pts = [[p["longitude"], p["latitude"]] if isinstance(p, dict) else [p[1], p[0]] for p in path]
            line.extend(pts[1:] if line and pts and line[-1] == pts[0] else pts)
        return int(route.get("distanceMeters") or 0), int(route.get("durationSeconds") or 0), line

    def route(self, origin: dict, destination: dict, waypoints: list[dict]) -> dict:
        # Apple's directions call takes one origin and one destination, so each stop is its own leg.
        stops = [origin, *waypoints, destination]
        legs, line = [], []
        for a, b in zip(stops, stops[1:]):
            dist, dur, pts = self._leg(a, b)
            legs.append({"distance_m": dist, "duration_s": dur})
            line.extend(pts[1:] if line and pts and line[-1] == pts[0] else pts)
        return {"distance_m": sum(x["distance_m"] for x in legs), "duration_s": sum(x["duration_s"] for x in legs),
                "traffic_delay_s": None, "legs": legs, "geometry": line}  # shortcut: no traffic figure from /v1/directions

    def matrix(self, origins: list[dict], destinations: list[dict]) -> list[list[dict | None]]:
        rows: list[list[dict | None]] = []
        for o in origins:
            row: list[dict | None] = []
            for i in range(0, len(destinations), ETA_CHUNK):
                chunk = destinations[i:i + ETA_CHUNK]
                data = self._call("/v1/etas", {"origin": _ll(o), "destinations": "|".join(_ll(d) for d in chunk),
                                               "transportType": TRANSPORT})
                etas = data.get("etas") or []
                for k in range(len(chunk)):  # answers come back in the order the destinations were sent
                    e = etas[k] if k < len(etas) and isinstance(etas[k], dict) else {}
                    exp, static = e.get("expectedTravelTimeSeconds"), e.get("staticTravelTimeSeconds")
                    row.append(None if exp is None or e.get("distanceMeters") is None else {
                        "distance_m": e["distanceMeters"], "duration_s": exp,
                        "traffic_delay_s": max(0, exp - static) if static is not None else None})
            rows.append(row)
        return rows


class _Unauthorized(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status
