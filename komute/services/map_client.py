"""Calls the map node (map/), which holds the Apple Maps credentials.

The browser never talks to the node or to Apple's web services. Pages call komute/views_map.py, which calls this
module. Same pattern as komute/services/verification.py: a shared key in X-API-Key, a timeout, and a small error class
that carries the website's own message and status, never the node's or Apple's text.

What is here: address suggestions and place details (autocomplete), routes (drive time, distance, the detour check and
the line drawn on the map) and the short-lived token the browser map (MapKit JS) needs.
"""
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import requests as http_client
from django.conf import settings
from django.core.cache import cache

from komute.services.geo import in_us_canada

logger = logging.getLogger('komute.views')

TIMEOUT = (2.0, 8.0)  # connect, read. A slow node should fail the suggestion box, not hang the page.
# Apple has no waypoint parameter, so the node asks for one route per leg, one after another: a ride with five stops is
# six Apple calls. The read timeout leaves room for that.
ROUTE_TIMEOUT = (2.0, 25.0)
ROUTE_CACHE_S = 300   # short on purpose: Apple's terms on keeping its results are not settled
MAX_STOPS = 5         # the post-ride form allows five; the node refuses more
# What the node's "ref" looks like: URL-safe base64, opaque to us. The node re-checks it before using it.
REF_RE = re.compile(r'^[A-Za-z0-9_-]{8,2200}$')
SUGGEST_KEYS = ('ref', 'type', 'title', 'subtitle', 'lat', 'lon')


class MapError(Exception):
    def __init__(self, message, status=502, code='error'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _node_code(resp):
    """The node's own error code ({"error": {"code": ...}}), or None."""
    try:
        body = resp.json()
    except ValueError:
        return None
    err = body.get('error') if isinstance(body, dict) else None
    return err.get('code') if isinstance(err, dict) else None


def _call(path, params=None, body=None, timeout=TIMEOUT):
    """GET (or POST when `body` is given) to the node; the parsed answer, or MapError with the website's own words."""
    headers = {'X-API-Key': settings.MAP_API_KEY} if settings.MAP_API_KEY else {}
    url = settings.MAP_API_URL.rstrip('/') + path
    try:
        if body is None:
            resp = http_client.get(url, params=params, headers=headers, timeout=timeout)
        else:
            resp = http_client.post(url, json=body, headers=headers, timeout=timeout)
    except http_client.exceptions.Timeout:
        raise MapError('The map service took too long. Please try again.', 504, 'timeout') from None
    except http_client.exceptions.RequestException:
        raise MapError('The map service is unavailable. Please try again later.', 503, 'unavailable') from None
    status = resp.status_code
    if status == 200:
        try:
            data = resp.json()
        except ValueError:
            raise MapError('The map service sent an unreadable answer.', 502, 'bad_answer') from None
        if not isinstance(data, dict):
            raise MapError('The map service sent an unreadable answer.', 502, 'bad_answer')
        return data
    code = _node_code(resp)
    if status == 503 and code == 'NOT_CONFIGURED':
        logger.error('map node is not configured (Apple credentials or web origin missing)')
        raise MapError('The map service is not set up correctly.', 503, 'config')
    if status == 503:
        raise MapError('The map service is busy. Please try again shortly.', 503, 'busy')
    if status == 504:
        raise MapError('The map service took too long. Please try again.', 504, 'timeout')
    if status == 404:
        raise MapError('That place was not found.', 404, 'not_found')
    if status == 422 and code == 'NO_ROUTE':
        raise MapError('No drivable route was found between those points.', 422, 'no_route')
    if status == 422 and code == 'OUTSIDE_SERVICE_AREA':
        raise MapError('That location is outside our service area.', 422, 'outside_area')
    if status == 422:
        logger.error('map node refused a request we built (%s) for %s', code, path)
        raise MapError('The map service could not use that request.', 400, 'invalid')
    if status in (401, 403):
        logger.error('map node refused our key (HTTP %s): check MAP_API_KEY on both sides', status)
        raise MapError('The map service is not set up correctly.', 502, 'config')
    logger.error('map node answered HTTP %s for %s', status, path)
    raise MapError('The map service had a problem. Please try again.', 502, 'error')


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---- addresses -------------------------------------------------------------------------------------------------------
def suggest(query, lat=None, lon=None):
    """Up to five suggestions for what the person has typed so far, each with only the fields the page uses.
    `lat`/`lon` (where the rider probably is) make the answer much better; the node adds its own default when they are absent."""
    params = {'q': query, 'limit': 5}
    if lat is not None and lon is not None:
        params.update(lat=lat, lon=lon)
    rows = []
    for r in _call('/v1/places/suggest', params).get('results') or []:
        if isinstance(r, dict) and isinstance(r.get('ref'), str) and REF_RE.match(r['ref']) and isinstance(r.get('title'), str):
            row = {k: r.get(k) for k in SUGGEST_KEYS}
            row['lat'], row['lon'] = _number(row['lat']), _number(row['lon'])
            rows.append(row)
    return rows[:5]


def details(ref):
    """The place a suggestion points to: text to show, coordinates and a structured address."""
    data = _call('/v1/places/details', {'ref': ref})
    lat, lon = _number(data.get('lat')), _number(data.get('lon'))
    if lat is None or lon is None:
        raise MapError('That place has no location.', 404, 'not_found')
    return {'label': data.get('label') or data.get('title'), 'lat': lat, 'lon': lon, 'address': data.get('address') or {}}


def bias_for(user):
    """Where to point the suggestions for `user`: the origin of their latest search (riders) or ride (drivers).
    None when there is nothing usable, and the node then uses its default. Never raises."""
    from komute.models import RideSearch, Rides
    try:
        if user.role == 'driver':
            row = Rides.objects.filter(driver=user).order_by('-created_at', '-id').values_list('origin_lat', 'origin_lng').first()
        else:
            row = RideSearch.objects.filter(user=user).order_by('-created_at', '-id').values_list('origin_lat', 'origin_lng').first()
    except Exception:
        return None
    if not row or (row[0] == 0 and row[1] == 0) or not in_us_canada(row[0], row[1]):
        return None
    return row


# ---- routes ----------------------------------------------------------------------------------------------------------
def _route_body(points):
    to = lambda p: {'lat': p[0], 'lon': p[1]}
    return {'origin': to(points[0]), 'destination': to(points[-1]), 'waypoints': [to(p) for p in points[1:-1]]}


def _one_route(points):
    data = _call('/v1/route', body=_route_body(points), timeout=ROUTE_TIMEOUT)
    distance, duration = _number(data.get('distance_m')), _number(data.get('duration_s'))
    if distance is None or duration is None:
        raise MapError('The map service sent an unreadable answer.', 502, 'bad_answer')
    return data


def _line(geometry):
    """The node's [[lon, lat], ...] rounded to 5 decimals (about a metre) with repeated points dropped, for a smaller page."""
    line = []
    for p in geometry if isinstance(geometry, list) else []:
        if isinstance(p, list) and len(p) >= 2 and _number(p[0]) is not None and _number(p[1]) is not None:
            point = [round(p[0], 5), round(p[1], 5)]
            if not line or line[-1] != point:
                line.append(point)
    return line


def route(points, direct=False, line=False):
    """Drive route through `points` [(lat, lng), ...] (start, stops in order, end).

    Returns {distance_m, duration_s}, plus direct_distance_m when `direct` and there are stops (the start-to-end route
    the detour warning compares against; None if only that second route failed), plus `line` ([[lng, lat], ...]) when
    asked. Answers are cached briefly, so the post-ride form re-asking for the same trip costs no Apple call."""
    points = [(round(float(lat), 5), round(float(lng), 5)) for lat, lng in points]
    want_direct = direct and len(points) > 2
    key = 'map-route:' + hashlib.sha256(repr((points, want_direct, line)).encode()).hexdigest()
    try:
        hit = cache.get(key)
    except Exception:
        hit = None
    if hit is not None:
        return hit
    if want_direct:
        # Both routes at once: the page waits for the slower one, not the sum.
        with ThreadPoolExecutor(max_workers=2) as pool:
            main_job = pool.submit(_one_route, points)
            direct_job = pool.submit(_one_route, [points[0], points[-1]])
            data = main_job.result()  # its MapError reaches the caller
            try:
                direct_distance = direct_job.result()['distance_m']
            except MapError:
                direct_distance = None  # the detour warning is a hint; the route itself still stands
    else:
        data = _one_route(points)
    result = {'distance_m': data['distance_m'], 'duration_s': data['duration_s']}
    if want_direct:
        result['direct_distance_m'] = direct_distance
    if line:
        result['line'] = _line(data.get('geometry'))
    try:
        cache.set(key, result, ROUTE_CACHE_S)
    except Exception:
        pass
    return result


# ---- browser map -----------------------------------------------------------------------------------------------------
def mapkit_token():
    """A short-lived token for MapKit JS, locked by the node to this site's own address (APPLE_MAPS_WEB_ORIGIN)."""
    data = _call('/v1/mapkit-token')
    token, ttl = data.get('token'), _number(data.get('expires_in'))
    if not isinstance(token, str) or not token or ttl is None:
        raise MapError('The map service sent an unreadable answer.', 502, 'bad_answer')
    return {'token': token, 'expires_in': ttl}
