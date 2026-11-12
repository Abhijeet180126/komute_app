"""Places, distances, dates and time zones for rides (API module 3).
Moved unchanged from komute/views.py, which keeps the old names as aliases."""
import calendar
import math
from datetime import date, datetime, timedelta

from django.db.models import Q

from komute.service_area import in_quebec

# Service area gate: rides can only be posted/searched with an origin and
# destination inside the US or Canada. Two boxes because Hawaii sits well
# outside the CONUS+Canada+Alaska box and would otherwise be excluded.
US_CANADA_BBOX = (-168.0, 24.0, -52.0, 72.0)   # (min_lon, min_lat, max_lon, max_lat)
HAWAII_BBOX    = (-161.0, 18.0, -154.0, 23.0)


def in_us_canada(lat, lng):
    for min_lon, min_lat, max_lon, max_lat in (US_CANADA_BBOX, HAWAII_BBOX):
        if min_lat <= lat <= max_lat and min_lon <= lng <= max_lon:
            return True
    return False


def service_area_error(lat, lng, kind):
    """Why a picked point can't be used, or None. `kind` names it in the
    message: "pickup", "drop-off" or "stop". Quebec is left out at launch
    (audit P-11, see service_area.py)."""
    if not in_us_canada(lat, lng):
        return f"We currently only support {kind} locations in the US and Canada."
    if in_quebec(lat, lng):
        return f"Komüte isn't available in Quebec yet. Please choose a {kind} location outside Quebec."
    return None


MAX_RIDE_LEAD_MONTHS = 4


def max_ride_date(today=None):
    """Today + 4 calendar months (day clamped to the target month's length),
    the furthest-out a ride can be posted or searched for. Pass `today` to
    count from a user's local date instead of the server's."""
    today = today or date.today()
    m = today.month - 1 + MAX_RIDE_LEAD_MONTHS
    y = today.year + m // 12
    m = m % 12 + 1
    day = min(today.day, calendar.monthrange(y, m)[1])
    return today.replace(year=y, month=m, day=day)


def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def geo_box_q(lat, lng, radius_km, lat_field, lng_field):
    """Q for the lat/lng box around a circle of radius_km, so SQL can drop
    far-away rows before haversine_km checks the rest (audit F-2). The box
    holds the whole circle, so it never drops a row the haversine test would
    keep. The longitude half-width is the exact one, asin(sin(r/R)/cos(lat)),
    not the smaller r/(R*cos(lat))."""
    ang = radius_km / 6371.0
    dlat = math.degrees(ang)
    q = Q(**{f"{lat_field}__gte": lat - dlat, f"{lat_field}__lte": lat + dlat})
    ratio = math.sin(ang) / max(math.cos(math.radians(lat)), 1e-12)
    if ratio < 1:  # otherwise the circle reaches a pole: any longitude
        dlng = math.degrees(math.asin(ratio))
        q &= Q(**{f"{lng_field}__gte": lng - dlng, f"{lng_field}__lte": lng + dlng})
    return q


def zone_or_utc(name):
    """ZoneInfo for an IANA timezone name sent by the browser (the `tz` form
    field); UTC when it's missing or not a known zone."""
    from zoneinfo import ZoneInfo
    if name:
        try:
            return ZoneInfo(name)
        except (ValueError, KeyError):   # ZoneInfoNotFoundError is a KeyError
            pass
    return ZoneInfo('UTC')


def local_day_bounds(day, tz):
    """[start, end) of calendar day `day` in `tz`, as aware datetimes, for
    filtering a DateTimeField. `depart_at__date` would use the UTC date, which
    puts a Toronto ride at 8 pm on the next day (ISSUES.md H5)."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start, end
