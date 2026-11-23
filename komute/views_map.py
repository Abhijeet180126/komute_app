"""JSON endpoints the pages call for maps: address suggestions, place details, routes and the browser map's token.
They check the caller, limit the rate, check the input, and pass the question to the map node through
komute.services.map_client. Nothing here talks to Apple directly."""
import math

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from komute.services import map_client
from komute.services.geo import in_us_canada
from komute.views import _rate_limited

RATE_MSG = "You're sending map requests faster than we can answer. Please wait a moment."
QUERY_MAX_CHARS = 200


def _json(payload, status=200):
    resp = JsonResponse(payload, status=status)
    resp['Cache-Control'] = 'no-store'
    return resp


def _error(message, status, code):
    return _json({'error': message, 'code': code}, status)


def _coordinate(raw):
    """A finite float, or None."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _point(raw):
    """"lat,lng" inside the US or Canada, as a (lat, lng) tuple; None otherwise."""
    parts = (raw or '').split(',')
    if len(parts) != 2:
        return None
    lat, lng = _coordinate(parts[0]), _coordinate(parts[1])
    if lat is None or lng is None or not in_us_canada(lat, lng):
        return None
    return lat, lng


def _guard(request, bucket):
    """The shared front door: signed in, and not over the per-user limit. Returns an error response or None."""
    if not request.user.is_authenticated:
        return _error('Please sign in.', 401, 'auth')
    if _rate_limited(request, bucket, extra_key=str(request.user.pk), per_ip=False):
        return _error(RATE_MSG, 429, 'rate_limited')
    return None


@require_GET
def map_suggest_view(request):
    if blocked := _guard(request, 'map-suggest'):
        return blocked
    q = request.GET.get('q', '').strip()
    if not 1 <= len(q) <= QUERY_MAX_CHARS:
        return _error('Type a place to search for.', 400, 'invalid')
    lat_raw, lon_raw = request.GET.get('lat'), request.GET.get('lon')
    lat = lon = None
    if lat_raw is not None or lon_raw is not None:
        lat, lon = _coordinate(lat_raw), _coordinate(lon_raw)
        if lat is None or lon is None or not in_us_canada(lat, lon):
            lat = lon = None   # only a hint: one outside the service area is dropped, never sent on
    try:
        return _json({'results': map_client.suggest(q, lat, lon)})
    except map_client.MapError as e:
        return _error(e.message, e.status, e.code)


@require_GET
def map_details_view(request):
    if blocked := _guard(request, 'map-details'):
        return blocked
    ref = request.GET.get('ref', '')
    if not map_client.REF_RE.match(ref):
        return _error('That place is not valid.', 400, 'invalid')
    try:
        place = map_client.details(ref)
    except map_client.MapError as e:
        return _error(e.message, e.status, e.code)
    if not in_us_canada(place['lat'], place['lon']):
        return _error('That location is outside our service area.', 422, 'outside_area')
    return _json(place)


@require_GET
def map_route_view(request):
    """GET /map/route/?o=lat,lng&d=lat,lng[&s=lat,lng;lat,lng][&direct=1][&line=1]

    `s` lists the stops in driving order. `direct=1` also returns the start-to-end distance (the post-ride detour
    warning). `line=1` returns the route line, for the Apple map on the trip pages."""
    if blocked := _guard(request, 'map-route'):
        return blocked
    origin, dest = _point(request.GET.get('o')), _point(request.GET.get('d'))
    raw_stops = request.GET.get('s', '')
    stops = [_point(s) for s in raw_stops.split(';')] if raw_stops else []
    if origin is None or dest is None or None in stops:
        return _error('Pick each place from the suggestions first.', 400, 'invalid')
    if len(stops) > map_client.MAX_STOPS:
        return _error(f'A trip can have at most {map_client.MAX_STOPS} stops.', 400, 'invalid')
    try:
        result = map_client.route([origin, *stops, dest], direct=request.GET.get('direct') == '1',
                                  line=request.GET.get('line') == '1')
    except map_client.MapError as e:
        return _error(e.message, e.status, e.code)
    return _json(result)


@require_GET
def map_token_view(request):
    """The browser map's token. Served only while the Apple map is switched on (MAP_DISPLAY=mapkit)."""
    if settings.MAP_DISPLAY != 'mapkit':
        return _error('Not found.', 404, 'not_found')
    if blocked := _guard(request, 'map-token'):
        return blocked
    try:
        return _json(map_client.mapkit_token())
    except map_client.MapError as e:
        return _error(e.message, e.status, e.code)
