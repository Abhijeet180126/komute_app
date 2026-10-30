import base64
import calendar
import io
import ipaddress
import json
import logging
import math
import os
import re
import uuid
from datetime import date, datetime, timedelta
from email.utils import formataddr
from urllib.parse import quote, urlsplit


from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadhandler import MemoryFileUploadHandler
from django.http import FileResponse, JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.http import Http404
from django.contrib import messages
from django.contrib.auth import get_user_model, authenticate, login, logout, update_session_auth_hash
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import ExtractHour, Least
from django.utils import timezone
from django.core.paginator import Paginator
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from allauth.account.models import EmailAddress
from allauth.account.models import EmailConfirmationHMAC
from allauth.account.views import ConfirmEmailView as AllauthConfirmEmailView
from django.core.mail import EmailMultiAlternatives
from django.core import signing
from django.utils.crypto import salted_hmac, constant_time_compare
from django.utils.http import url_has_allowed_host_and_scheme
from django.template.loader import render_to_string
from django.urls import Resolver404, resolve, reverse

from .client_ip import client_ip as _client_ip
from .models import Rides, RideStop, Vehicle, RideBooking, PriceOffer, Message, RideSearch, VerificationLog, Review, RideInvite, UserBlock, UserReport, BiometricConsent, LegalAcceptance, EmailLog
from .middleware import LegalAcceptanceMiddleware, legal_versions_key
from .hashid_utils import encode_id, decode_id
from .notifications import booking_fingerprint, notify_bookings_changed
from .tasks import send_later
from . import private_media
from .private_media import delete_stored_file, open_stored_file, save_private_image
from .service_area import in_quebec

User = get_user_model()
logger = logging.getLogger(__name__)


_UNNAMED_USER = 'Komüte user'


def _display_name(user):
    """How another person is named on pages and in emails: their name, never
    their email address (some older accounts have no name)."""
    return (user.full_name or '').strip().title() or _UNNAMED_USER




def _consent_request_details(request):
    """IP address and user agent stored with a consent or acceptance record.
    An address that doesn't parse is stored as null."""
    ip = _client_ip(request)
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        ip = None
    return {"ip_address": ip, "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300]}


def _record_legal_acceptance(request, user, source):
    """Stores that `user` accepted the current Terms and Privacy Policy, and
    remembers it in the session so LegalAcceptanceMiddleware lets them on."""
    LegalAcceptance.objects.create(
        user=user,
        terms_version=settings.TERMS_VERSION,
        privacy_version=settings.PRIVACY_VERSION,
        source=source,
        **_consent_request_details(request),
    )
    if request.user.is_authenticated and request.user.pk == user.pk:
        request.session[LegalAcceptanceMiddleware.SESSION_KEY] = legal_versions_key()


def _rate_limited(request, name, extra_key='', per_ip=True):
    """Fixed-window throttle backed by the shared cache (Redis in production
    — see CACHES in settings.py — so the count holds across worker
    processes). `name` must be a key in settings.RATE_LIMITS, which supplies
    the (max requests, window in seconds) — tunable per deployment via .env.
    Returns True (and counts this call towards the bucket) if the caller
    should be blocked; the caller decides how to respond.

    `extra_key` narrows the bucket beyond client IP — e.g. the submitted
    email on a login attempt, or a user ID for an authenticated endpoint.
    per_ip=False drops the IP from the bucket, so the limit follows
    `extra_key` (a user or a target address) across networks.

    Fails open (never blocks) if the cache backend itself is unreachable —
    a Redis blip shouldn't turn into a login/signup outage on top of it."""
    limit, window_seconds = settings.RATE_LIMITS[name]
    ip = _client_ip(request) if per_ip else '*'
    bucket = f'ratelimit:{name}:{ip}:{extra_key}'
    try:
        cache.add(bucket, 0, window_seconds)
        count = cache.incr(bucket)
    except Exception:
        return False
    return count > limit


_TOO_MANY_ATTEMPTS_MSG = "Too many attempts. Please try again later."


def _fmt_ride_local(ride):
    """Return depart_at formatted in the timezone the driver used when posting.
    Falls back to UTC if no tz is stored on the ride."""
    if ride.tz:
        try:
            from zoneinfo import ZoneInfo
            local = ride.depart_at.astimezone(ZoneInfo(ride.tz))
            tzname = local.tzname() or ''
            fmt = local.strftime('%A, %B %d, %Y at %I:%M %p') + (f' {tzname}' if tzname else '')
            return fmt.replace(' 0', ' ')
        except Exception:
            pass
    return ride.depart_at.strftime('%A, %B %d, %Y at %I:%M %p UTC')


def _local_dt(ride, dt):
    """Convert a UTC datetime to the ride's stored timezone. Returns naive UTC on failure."""
    if ride.tz and dt:
        try:
            from zoneinfo import ZoneInfo
            return dt.astimezone(ZoneInfo(ride.tz))
        except Exception:
            pass
    return dt


def _zone_or_utc(name):
    """ZoneInfo for an IANA timezone name sent by the browser (the `tz` form
    field); UTC when it's missing or not a known zone."""
    from zoneinfo import ZoneInfo
    if name:
        try:
            return ZoneInfo(name)
        except (ValueError, KeyError):   # ZoneInfoNotFoundError is a KeyError
            pass
    return ZoneInfo('UTC')


def _local_day_bounds(day, tz):
    """[start, end) of calendar day `day` in `tz`, as aware datetimes, for
    filtering a DateTimeField. `depart_at__date` would use the UTC date, which
    puts a Toronto ride at 8 pm on the next day (ISSUES.md H5)."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start, end


def _send_booking_confirmation_emails(booking):
    try:
        ride    = booking.ride
        rider   = booking.passenger
        driver  = ride.driver
        vehicle = ride.vehicle

        depart_display = _fmt_ride_local(ride)
        amount         = f"${booking.agreed_price_cents / 100:.2f}"
        booking_ref    = f"KMT-{ride.depart_at.year}-{ride.id:04d}-B{booking.id}"
        vehicle_display = f"{vehicle.year} {vehicle.make} {vehicle.model}" if vehicle else "N/A"
        vehicle_plate   = vehicle.plate if vehicle else "N/A"
        seats_remaining = ride.seats_available

        rider_ctx = {
            'rider_name':      _display_name(rider),
            'driver_name':     _display_name(driver),
            'origin':          ride.origin,
            'destination':     ride.destination,
            'depart_display':  depart_display,
            'vehicle_display': vehicle_display,
            'vehicle_plate':   vehicle_plate,
            'seats':           booking.seats,
            'amount':          amount,
            'booking_ref':     booking_ref,
        }
        driver_ctx = {
            'driver_name':     _display_name(driver),
            'rider_name':      _display_name(rider),
            'origin':          ride.origin,
            'destination':     ride.destination,
            'depart_display':  depart_display,
            'seats':           booking.seats,
            'seats_remaining': seats_remaining,
            'seats_total':     ride.seats_total,
            'amount':          amount,
            'booking_ref':     booking_ref,
        }

        rider_html  = render_to_string('account/email/booking_confirmed_rider.html',  rider_ctx)
        driver_html = render_to_string('account/email/booking_confirmed_driver.html', driver_ctx)

        rider_msg = EmailMultiAlternatives(
            subject=f"Your ride to {ride.destination.split(',')[0].strip()} is confirmed! 🎉",
            body=f"Hi {rider_ctx['rider_name']}, your booking is confirmed. Ref: {booking_ref}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[rider.email],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_CONFIRMATION],
        )
        rider_msg.attach_alternative(rider_html, 'text/html')
        send_later(rider_msg, EmailLog.KIND_BOOKING_CONFIRMED, rider)

        driver_msg = EmailMultiAlternatives(
            subject=f"You accepted {rider_ctx['rider_name']}'s ride request 🚗",
            body=f"Hi {driver_ctx['driver_name']}, you accepted {rider_ctx['rider_name']}'s request for {booking.seats} seat(s). Ref: {booking_ref}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[driver.email],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_CONFIRMATION],
        )
        driver_msg.attach_alternative(driver_html, 'text/html')
        send_later(driver_msg, EmailLog.KIND_BOOKING_CONFIRMED, driver)
    except Exception:
        logger.exception('_send_booking_confirmation_emails could not build or queue an email')


def _send_booking_cancelled_emails(booking):
    """Passenger cancelled their booking — notify the driver."""
    try:
        ride   = booking.ride
        rider  = booking.passenger
        driver = ride.driver

        depart_display = _fmt_ride_local(ride)
        booking_ref    = f"KMT-{ride.depart_at.year}-{ride.id:04d}-B{booking.id}"

        ctx = {
            'driver_name':    _display_name(driver),
            'rider_name':     (rider.full_name  or rider.email).title(),
            'origin':         ride.origin,
            'destination':    ride.destination,
            'depart_display': depart_display,
            'seats':          booking.seats,
            'booking_ref':    booking_ref,
        }
        html = render_to_string('account/email/booking_cancelled_driver.html', ctx)
        msg  = EmailMultiAlternatives(
            subject=f"{ctx['rider_name']} cancelled their booking on your ride",
            body=f"Hi {ctx['driver_name']}, {ctx['rider_name']} has cancelled their {booking.seats} seat(s). Ref: {booking_ref}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[driver.email],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_CANCELLED],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_BOOKING_CANCELLED, driver)
    except Exception:
        logger.exception('_send_booking_cancelled_emails could not build or queue an email')


def _send_booking_request_email(booking):
    """Rider requested a booking — notify the driver."""
    try:
        ride   = booking.ride
        rider  = booking.passenger
        driver = ride.driver

        depart_display = _fmt_ride_local(ride)
        booking_ref    = f"KMT-{ride.depart_at.year}-{ride.id:04d}-B{booking.id}"

        ctx = {
            'driver_name':    _display_name(driver),
            'rider_name':     (rider.full_name  or rider.email).title(),
            'origin':         ride.origin,
            'destination':    ride.destination,
            'depart_display': depart_display,
            'seats':          booking.seats,
            'booking_ref':    booking_ref,
        }
        html = render_to_string('account/email/booking_requested_driver.html', ctx)
        msg  = EmailMultiAlternatives(
            subject=f"New booking request from {ctx['rider_name']} 🔔",
            body=f"Hi {ctx['driver_name']}, {ctx['rider_name']} has requested {booking.seats} seat(s) on your ride. Ref: {booking_ref}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[driver.email],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_REQUEST],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_BOOKING_REQUEST, driver)
    except Exception:
        logger.exception('_send_booking_request_email could not build or queue an email')


def _send_booking_rejected_email(booking):
    """Driver rejected a booking request — notify the rider."""
    try:
        ride   = booking.ride
        rider  = booking.passenger
        driver = ride.driver

        depart_display = _fmt_ride_local(ride)
        booking_ref    = f"KMT-{ride.depart_at.year}-{ride.id:04d}-B{booking.id}"

        ctx = {
            'rider_name':     (rider.full_name  or rider.email).title(),
            'driver_name':    _display_name(driver),
            'origin':         ride.origin,
            'destination':    ride.destination,
            'depart_display': depart_display,
            'booking_ref':    booking_ref,
        }
        html = render_to_string('account/email/booking_rejected_rider.html', ctx)
        msg  = EmailMultiAlternatives(
            subject="Your booking request was not accepted",
            body=f"Hi {ctx['rider_name']}, unfortunately the driver could not accommodate your booking request. Ref: {booking_ref}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[rider.email],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_REJECTED],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_BOOKING_REJECTED, rider)
    except Exception:
        logger.exception('_send_booking_rejected_email could not build or queue an email')


def _send_ride_cancelled_emails(ride, affected_bookings):
    """Driver cancelled the whole ride — notify every affected passenger."""
    try:
        driver         = ride.driver
        depart_display = _fmt_ride_local(ride)

        for booking in affected_bookings:
            try:
                rider = booking.passenger
                ctx   = {
                    'rider_name':     (rider.full_name  or rider.email).title(),
                    'driver_name':    _display_name(driver),
                    'origin':         ride.origin,
                    'destination':    ride.destination,
                    'depart_display': depart_display,
                    'seats':          booking.seats,
                }
                html = render_to_string('account/email/ride_cancelled_rider.html', ctx)
                msg  = EmailMultiAlternatives(
                    subject=f"Your ride to {ride.destination.split(',')[0].strip()} has been cancelled",
                    body=f"Hi {ctx['rider_name']}, your ride with {ctx['driver_name']} on {depart_display} has been cancelled.",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[rider.email],
                    reply_to=[settings.REPLY_TO_EMAIL_RIDE_CANCELLED],
                )
                msg.attach_alternative(html, 'text/html')
                send_later(msg, EmailLog.KIND_RIDE_CANCELLED, rider)
            except Exception:
                logger.exception('_send_ride_cancelled_emails could not build or queue an email')
    except Exception:
        logger.exception('_send_ride_cancelled_emails could not build or queue an email')


def _send_welcome_email(request, user):
    try:
        login_url = request.build_absolute_uri(reverse('login'))
        ctx = {
            'first_name': (user.full_name or user.email).title().split()[0],
            'login_url':  login_url,
        }
        html = render_to_string('account/email/welcome.html', ctx)
        msg  = EmailMultiAlternatives(
            subject="Welcome to Komüte! 🌿",
            body=f"Hi {ctx['first_name']}, welcome to Komüte! Your account is ready — log in at {login_url} to find or offer your first ride.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
            reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_SIGNUP],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_WELCOME, user)
    except Exception:
        logger.exception('_send_welcome_email could not build or queue an email')


_EMAIL_UNVERIFIED_MSG = (
    "Please verify your email address first: click \"Verify Now\" next to "
    "your email on your profile page."
)

_DOB_MISSING_MSG = (
    "Please add your date of birth on your profile page first. "
    "KOMUTE is for adults aged 18 and over."
)


def _latest_adult_dob():
    """The latest date of birth that is 18 today (Feb 29 -> Feb 28)."""
    today = date.today()
    try:
        return today.replace(year=today.year - 18)
    except ValueError:
        return today.replace(year=today.year - 18, day=28)


def _parse_dob(dob_str):
    """(dob, error) for a submitted date of birth. Ages 18 to 100 only
    (audit L-5); an empty value is an error, so every caller requires it."""
    if not dob_str:
        return None, "Date of birth is required."
    try:
        dob = date.fromisoformat(dob_str)
    except ValueError:
        return None, "Invalid date of birth."
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < 18:
        return None, "You must be at least 18 years old."
    if age > 100:
        return None, "Please enter a valid date of birth (age must be 100 or under)."
    return dob, None


_PHONE_RE = re.compile(r'^\+?[1-9]\d{6,14}$')
_PHONE_NUMBER_RE = re.compile(r'^\d{4,14}$')

# Service area gate: rides can only be posted/searched with an origin and
# destination inside the US or Canada. Two boxes because Hawaii sits well
# outside the CONUS+Canada+Alaska box and would otherwise be excluded.
_US_CANADA_BBOX = (-168.0, 24.0, -52.0, 72.0)   # (min_lon, min_lat, max_lon, max_lat)
_HAWAII_BBOX    = (-161.0, 18.0, -154.0, 23.0)


def _in_us_canada(lat, lng):
    for min_lon, min_lat, max_lon, max_lat in (_US_CANADA_BBOX, _HAWAII_BBOX):
        if min_lat <= lat <= max_lat and min_lon <= lng <= max_lon:
            return True
    return False


def _service_area_error(lat, lng, kind):
    """Why a picked point can't be used, or None. `kind` names it in the
    message: "pickup", "drop-off" or "stop". Quebec is left out at launch
    (audit P-11, see service_area.py)."""
    if not _in_us_canada(lat, lng):
        return f"We currently only support {kind} locations in the US and Canada."
    if in_quebec(lat, lng):
        return f"Komüte isn't available in Quebec yet. Please choose a {kind} location outside Quebec."
    return None


def _clean_stops(post):
    """The ride form's named stops, each with the coordinates of the
    suggestion the driver picked. Returns (stops, error): stops is a list of
    dicts with the stop's form index, location, lat and lng; error is a
    message for the first stop that wasn't picked or is out of area. A typed
    but unpicked stop used to be saved at 0,0 and never matched a search
    (ISSUES.md M2)."""
    lats = post.getlist('stop_lat')
    lngs = post.getlist('stop_lng')
    stops = []
    for idx, loc in enumerate(post.getlist('stop_location')):
        loc = loc.strip()
        if not loc:
            continue
        try:
            lat, lng = float(lats[idx]), float(lngs[idx])
        except (IndexError, ValueError, TypeError):
            lat = lng = 0.0
        if lat == 0 and lng == 0:
            return stops, f'Pick "{loc}" from the suggestions list, or remove that stop.'
        if not _in_us_canada(lat, lng):
            return stops, f'"{loc}" is outside the US and Canada. We currently only support stops there.'
        if in_quebec(lat, lng):
            return stops, f'"{loc}" is in Quebec. Komüte isn\'t available in Quebec yet, so please choose a stop outside Quebec.'
        stops.append({'idx': idx, 'location': loc, 'lat': lat, 'lng': lng})
    return stops, None


_MAX_RIDE_LEAD_MONTHS = 4


def _max_ride_date(today=None):
    """Today + 4 calendar months (day clamped to the target month's length),
    the furthest-out a ride can be posted or searched for. Pass `today` to
    count from a user's local date instead of the server's."""
    today = today or date.today()
    m = today.month - 1 + _MAX_RIDE_LEAD_MONTHS
    y = today.year + m // 12
    m = m % 12 + 1
    day = min(today.day, calendar.monthrange(y, m)[1])
    return today.replace(year=y, month=m, day=day)


# Bounds on numbers posted by forms and scripts (audit L-10): without them
# "inf" or a huge value raised OverflowError (a 500) in float/timedelta maths
# or overflowed the database's integer columns.
_MAX_PRICE_CENTS = 100_000                 # $1,000 a seat, the price-offer cap too
_MAX_DURATION_SECONDS = 7 * 24 * 3600      # longer than any US/Canada drive
_MAX_TZ_OFFSET_MINUTES = 14 * 60           # real UTC offsets run -12:00..+14:00

# Ride preference tags, in the order post-ride.html shows them. Edit Ride
# uses this list and search filters match these exact names, so keep all
# three in step (RidePreferenceOptionsTests checks post-ride.html).
RIDE_PREFERENCE_OPTIONS = [
    'Music OK', 'Chatty Driver', 'Quiet Ride', 'Pet Friendly', 'No Smoking',
    'Luggage OK', 'Verified Only', 'On-Time Driver', 'Winter Tires',
    'Flexible with Time',
]


def _parse_dollars_to_cents(value):
    """Dollar string → whole cents. Raises ValueError for text, NaN or
    infinity; the caller checks the range."""
    amount = float(value)
    if not math.isfinite(amount):
        raise ValueError('price must be a finite number')
    return int(round(amount * 100))


def _parse_duration_seconds(value):
    """The route duration a page script posts, or 0 (unknown) when it's
    missing, malformed or out of range."""
    try:
        seconds = int(value or 0)
    except (ValueError, TypeError):
        return 0
    return seconds if 0 < seconds <= _MAX_DURATION_SECONDS else 0


def _has_valid_vehicle_row(post):
    """True if the submitted vehicle rows contain at least one complete
    (make + plate + year) entry. Used to stop a driver's vehicles from
    being silently wiped by a form submission that carries no vehicle
    fields at all (JS failure, a stale page, all rows removed by hand)."""
    makes  = post.getlist("vehicle_make")
    years  = post.getlist("vehicle_year")
    plates = post.getlist("vehicle_plate")
    for i, make in enumerate(makes):
        plate = plates[i] if i < len(plates) else ""
        yr    = years[i] if i < len(years) else ""
        if make.strip() and plate.strip() and yr.strip():
            return True
    return False

_country_codes_cache = None

def _load_country_codes():
    global _country_codes_cache
    if _country_codes_cache is None:
        path = os.path.join(settings.BASE_DIR, "static", "data", "country_codes.json")
        with open(path, encoding="utf-8") as f:
            _country_codes_cache = json.load(f)
    return _country_codes_cache


def _valid_dial_codes():
    return {c["dial_code"] for c in _load_country_codes()}


_PRIORITY_COUNTRY_NAMES = ("Canada", "United States")

def _dropdown_countries():
    """Country list split into a priority block shown first in the
    country-code dropdown, and the remaining countries below. Canada and the
    United States share dial code +1, and a stored E.164 phone number can't
    tell them apart again on redisplay, so they're merged into one entry
    instead of being offered (and silently re-picked) as two."""
    codes = _load_country_codes()
    priority = [{"name": "United States / Canada", "dial_code": "+1", "iso2": "US"}]
    others = [c for c in codes if c["name"] not in _PRIORITY_COUNTRY_NAMES]
    return priority, others


def _split_phone(phone):
    """Split a stored E.164 phone string back into (country_code, phone_number)
    for re-populating the separate form fields."""
    if not phone:
        return "", ""
    for code in sorted(_valid_dial_codes(), key=len, reverse=True):
        if phone.startswith(code):
            return code, phone[len(code):]
    return "", phone

def _haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _geo_box_q(lat, lng, radius_km, lat_field, lng_field):
    """Q for the lat/lng box around a circle of radius_km, so SQL can drop
    far-away rows before _haversine_km checks the rest (audit F-2). The box
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


upload_logger = logging.getLogger("komute.uploads")


def _resize_image(file_obj, max_dim=1200, quality=85, label="image"):
    """Validate that file_obj is a decodable image under the size cap, and
    return a ContentFile with it resized to max_dim on its longest side.
    Raises django.core.exceptions.ValidationError if the upload is too large
    or isn't a valid image (including Pillow's decompression-bomb guard
    tripping) — callers must reject the upload, never fall back to storing
    the unverified raw bytes. Every upload is logged under `label`
    (ISSUES.md H2)."""
    max_bytes = settings.IMAGE_UPLOAD_MAX_MB * 1024 * 1024
    desc = (f"{label} name={getattr(file_obj, 'name', '')!r} size={file_obj.size} "
            f"type={getattr(file_obj, 'content_type', '')!r}")
    if file_obj.size > max_bytes:
        upload_logger.warning("upload rejected (too large): %s", desc)
        raise ValidationError(f"File is too large (max {settings.IMAGE_UPLOAD_MAX_MB} MB).")

    from PIL import Image, ImageOps
    try:
        file_obj.seek(0)
        img = Image.open(file_obj)
        original_dims = img.size
        # JPEGs decode at a reduced scale (no smaller than max_dim), which
        # keeps memory down for high-resolution phone photos.
        img.draft('RGB', (max_dim, max_dim))
        img = ImageOps.exif_transpose(img)  # fix camera rotation
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
    except Exception as e:
        upload_logger.warning("upload rejected (not a readable image: %s): %s", type(e).__name__, desc)
        raise ValidationError("That file isn't a valid image.")
    upload_logger.info("upload accepted: %s dims=%dx%d -> %dx%d, %d bytes",
                       desc, *original_dims, *img.size, buf.tell())
    return ContentFile(buf.getvalue(), name='image.jpg')


def _safe_next(request):
    """The `next` page to return to after signup, login or role selection,
    if it's on this site; otherwise "". Carried through all three so someone
    opening a shared ride link without an account lands back on that ride."""
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                       require_https=request.is_secure()):
        return next_url
    return ""


def _with_next(url_name, next_url):
    url = reverse(url_name)
    # safe='/', as Django's own login redirect and the |urlencode filter do.
    return f"{url}?next={quote(next_url, safe='/')}" if next_url else url


@never_cache
def signup_view(request):
    if request.user.is_authenticated:
        messages.info(request, "You are already logged in.")
        return redirect("profile")

    form_data = {}
    errors = {}
    next_url = _safe_next(request)

    if request.method == "POST":

        full_name        = request.POST.get("full_name", "").strip()
        email            = request.POST.get("email", "").strip().lower()
        country_code     = request.POST.get("country_code", "").strip()
        phone_number     = request.POST.get("phone_number", "").strip()
        dob_str          = request.POST.get("dob", "").strip()
        password         = request.POST.get("password", "")
        confirm_password = request.POST.get("confirm_password", "")
        agreed_to_terms  = request.POST.get("agreed_to_terms", "")

        form_data = {"full_name": full_name, "email": email,
                     "country_code": country_code, "phone_number": phone_number,
                     "dob": dob_str, "agreed_to_terms": agreed_to_terms}

        if _rate_limited(request, 'signup'):
            errors["rate_limit"] = "Too many signup attempts from this connection. Please try again later."
            priority_countries, countries = _dropdown_countries()
            return render(request, "signup.html", {"form": form_data, "errors": errors,
                                                     "dob_max": _latest_adult_dob().isoformat(),
                                                     "priority_countries": priority_countries, "countries": countries})

        if not full_name:
            errors["full_name"] = "Full name is required."

        if not email:
            errors["email"] = "Email address is required."
        else:
            try:
                validate_email(email)
            except ValidationError:
                errors["email"] = "Enter a valid email address (e.g. user@example.com)."
            else:
                if User.objects.filter(email__iexact=email).exists():
                    errors["email"] = "An account with this email already exists."

        phone = ""
        if not country_code:
            errors["country_code"] = "Please select a country code."
        elif country_code not in _valid_dial_codes():
            errors["country_code"] = "Please select a valid country code."

        if not phone_number:
            errors["phone_number"] = "Phone number is required."
        elif not _PHONE_NUMBER_RE.match(phone_number):
            errors["phone_number"] = "Enter a valid phone number (digits only)."

        if not errors.get("country_code") and not errors.get("phone_number"):
            phone = f"{country_code}{phone_number}"
            if not _PHONE_RE.match(phone):
                errors["phone_number"] = "Phone must be in E.164 format (e.g. +14155552671)."

        # 18+ only (audit L-5): checked here, not first at booking.
        dob, dob_error = _parse_dob(dob_str)
        if dob_error:
            errors["dob"] = dob_error

        if not password:
            errors["password"] = "Password is required."
        else:
            try:
                # Unsaved instance so UserAttributeSimilarityValidator can
                # compare the password against this signup's own email/name,
                # without a real row existing yet.
                validate_password(password, user=User(username=email, email=email, full_name=full_name))
            except ValidationError as e:
                errors["password"] = " ".join(e.messages)

        if password != confirm_password:
            errors["confirm_password"] = "Passwords do not match."

        if agreed_to_terms != "1":
            errors["agreed_to_terms"] = "You must accept the Terms & Conditions and the Privacy Policy to sign up."

        if not errors:
            with transaction.atomic():
                new_user = User.objects.create_user(
                    username=email,
                    email=email,
                    password=password,
                    full_name=full_name,
                    phone=phone or None,
                    dob=dob,
                )
                _record_legal_acceptance(request, new_user, LegalAcceptance.SOURCE_SIGNUP)
            _send_welcome_email(request, new_user)
            messages.success(request, "You have successfully signed up. Now Login to the account.")
            return redirect(_with_next("login", next_url))

    priority_countries, countries = _dropdown_countries()
    return render(request, "signup.html", {"form": form_data, "errors": errors, "next": next_url,
                                             "dob_max": _latest_adult_dob().isoformat(),
                                             "priority_countries": priority_countries, "countries": countries})

@never_cache
def login_view(request):
    if request.user.is_authenticated:
        messages.info(request, "You are already logged in.")
        return redirect("profile")
    next_url = _safe_next(request)
    if request.method == "POST":
        email = request.POST.get("email")
        password = request.POST.get("password")

        # Two independent buckets: per-IP (stops a script trying many
        # accounts from one source) and per-account (stops one account being
        # credential-stuffed from many IPs) — a combined key would catch
        # neither on its own.
        blocked = _rate_limited(request, 'login-ip')
        if email:
            blocked = _rate_limited(request, 'login-acct', extra_key=email.lower()) or blocked
        if blocked:
            messages.error(request, "Too many login attempts. Please wait a few minutes and try again.")
            return render(request, "login.html", {"next": next_url})

        # forgot_password_view resolves accounts case-insensitively
        # (email__iexact), but ModelBackend's username lookup is an exact
        # match -- so look the account up the same way here first and
        # authenticate with its actual stored casing, or a user whose email
        # was typed/auto-capitalized differently than it was stored (or than
        # a mixed-case signup produced) could reset their password
        # successfully via email yet still be told "Invalid email or
        # password" when logging in with it.
        lookup_email = email
        if email:
            matched_user = User.objects.filter(email__iexact=email).first()
            if matched_user:
                lookup_email = matched_user.email
        user = authenticate(request, username=lookup_email, password=password)
        if user:
            is_first_login = user.last_login is None
            login(request, user)
            if is_first_login:
                return redirect(_with_next("role-select", next_url))
            return redirect(next_url or "profile")
        else:
            messages.error(request, "Invalid email or password")
    return render(request, "login.html", {"next": next_url})




def role_select_view(request):
    user = request.user
    if not user.is_authenticated:
        return redirect("login")

    next_url = _safe_next(request)
    if request.method == "POST":
        role = request.POST.get("role")
        if role in ["rider", "driver"]:
            user.role = role
            user.save(update_fields=["role"])
            return redirect(next_url or "profile")

    return render(request, "role-select.html", {"role": user.role, "next": next_url})


def home_view(request):
    return render(request, "home.html")


def profile_view(request):
    # request.user only: a session-stored user id outlived deactivation
    # (closed or banned accounts kept access to this view).
    user = request.user if request.user.is_authenticated else None

    if not user:
        messages.error(request, "Please log in to view your profile.")
        return redirect("login")

    # Default values from saved user data (used for GET and re-render on error)
    errors           = {}
    full_name        = user.full_name
    country_code, phone_number = _split_phone(user.phone)
    dob_str          = user.dob.isoformat() if user.dob else ""
    gender           = user.gender or ""
    role             = user.role or "rider"
    bio              = user.bio or ""
    license_number   = user.license_number or ""
    license_province = user.license_province or ""
    license_expiry_str = user.license_expiry.isoformat() if user.license_expiry else ""

    vehicles = list(user.vehicles.values("id", "make", "model", "year", "color", "plate", "vehicle_verified", "photo_url"))

    if request.method == "POST":
        full_name          = request.POST.get("full_name", "").strip()
        country_code       = request.POST.get("country_code", "").strip()
        phone_number       = request.POST.get("phone_number", "").strip()
        dob_str            = request.POST.get("dob", "").strip()
        gender             = request.POST.get("gender", "").strip()
        role               = request.POST.get("role", "").strip()
        bio                = request.POST.get("bio", "").strip()
        license_number     = request.POST.get("license_number", "").strip()
        license_province   = request.POST.get("license_province", "").strip()
        license_expiry_str = request.POST.get("license_expiry", "").strip()

        if not full_name:
            errors["full_name"] = "Full name is required."
        if role not in ("rider", "driver"):
            errors["role"] = "Invalid role selected."

        phone = ""
        if phone_number or country_code:
            if not country_code or country_code not in _valid_dial_codes():
                errors["country_code"] = "Please select a valid country code."
            if not phone_number:
                errors["phone_number"] = "Phone number is required."
            elif not _PHONE_NUMBER_RE.match(phone_number):
                errors["phone_number"] = "Enter a valid phone number (digits only)."
            if not errors.get("country_code") and not errors.get("phone_number"):
                phone = f"{country_code}{phone_number}"
                if not _PHONE_RE.match(phone):
                    errors["phone_number"] = "Phone must be in E.164 format (e.g. +14155552671)."

        # Required for riders too: booking needs it (audit L-5).
        dob, dob_error = _parse_dob(dob_str)
        if dob_error:
            errors["dob"] = dob_error

        license_expiry = None
        if license_expiry_str:
            try:
                license_expiry = date.fromisoformat(license_expiry_str)
            except ValueError:
                errors["license_expiry"] = "Invalid license expiry date."
            else:
                # The date picker already blocks past dates; this enforces it
                # server-side. DL-Verification cross-checks this value against
                # the license, so it must be a real, current expiry.
                if role == "driver" and license_expiry < timezone.localdate():
                    errors["license_expiry"] = "Your license has expired. Please enter the expiry date of a valid license."

        if role == "driver":
            submitted_plates = [p.strip() for p in request.POST.getlist("vehicle_plate") if p.strip()]
            if len(submitted_plates) != len({p.upper() for p in submitted_plates}):
                errors["vehicles"] = "Duplicate plate numbers are not allowed."
            else:
                for plate in submitted_plates:
                    if Vehicle.objects.filter(plate=plate).exclude(owner=user).exists():
                        errors["vehicles"] = f"Plate \"{plate}\" is already registered to another vehicle."
                        break

            if not errors.get("vehicles") and not _has_valid_vehicle_row(request.POST):
                errors["vehicles"] = "You must add at least one vehicle to be a driver."

        if not errors:
            # Details DL-Verification compared against the license. Changing
            # any of them means the driver is no longer verified as entered
            # (C3). Comparison mirrors the service: case and hyphens ignored.
            identity_changed = role == "driver" and user.driver_verified and (
                _norm_name(full_name) != _norm_name(user.full_name)
                or dob != user.dob
                or _norm_code(license_number) != _norm_code(user.license_number)
                or license_province.casefold() != (user.license_province or "").casefold()
                or license_expiry != user.license_expiry
            )

            user.full_name = full_name
            user.phone     = phone or None
            user.dob       = dob
            user.gender    = gender or None
            user.role      = role
            user.bio       = bio or None

            update_fields = ["full_name", "phone", "dob", "gender", "role", "bio"]
            # Replaced image files, deleted only once the new URLs are saved.
            stale_urls = []
            # A rejected photo must not end in "Profile saved successfully".
            image_rejected = False

            if role == "driver":
                user.license_number   = license_number or None
                user.license_province = license_province or None
                user.license_expiry   = license_expiry
                update_fields += ["license_number", "license_province", "license_expiry"]

                # The licence photo isn't saved with the profile: it is taken
                # in the verification step and never stored (audit P-5/P-13).

                if identity_changed and user.driver_verified:
                    user.driver_verified  = False
                    user.face_match_score = None
                    update_fields += ["driver_verified", "face_match_score"]
                    messages.warning(request, "Your name, date of birth or license details changed. Please verify your identity again before posting rides.")

            # selfie / profile picture upload — all roles
            selfie_file = request.FILES.get("selfie_file")
            if selfie_file and selfie_file.size > 0:
                try:
                    resized = _resize_image(selfie_file, label=f"user={user.id} profile-photo")
                except ValidationError as e:
                    image_rejected = True
                    messages.error(request, f"Profile photo: {e.messages[0]}")
                else:
                    stale_urls += [user.selfie_url, user.avatar_url]
                    url = save_private_image(private_media.KIND_SELFIE, user.id, resized)
                    user.selfie_url = url
                    user.avatar_url = url
                    update_fields += ["selfie_url", "avatar_url"]
                    # No re-verification needed: the profile photo (face or
                    # vehicle) is never compared with the license.

            user.save(update_fields=update_fields)
            for old_url in set(stale_urls):
                delete_stored_file(old_url)

            if role == "driver":
                makes       = request.POST.getlist("vehicle_make")
                models_list = request.POST.getlist("vehicle_model")
                years       = request.POST.getlist("vehicle_year")
                colors      = request.POST.getlist("vehicle_color")
                plates      = request.POST.getlist("vehicle_plate")
                ids         = request.POST.getlist("vehicle_id")

                # Update rows in place (matched by id, falling back to plate
                # for rows without one) instead of delete-then-recreate, so
                # a vehicle's primary key -- and anything referencing it,
                # like a driver's already-posted Rides -- survives a save.
                existing_by_id    = {v.id: v for v in user.vehicles.all()}
                existing_by_plate = {v.plate: v for v in existing_by_id.values()}
                matched_ids = set()

                for i, make in enumerate(makes):
                    make = make.strip()
                    plate = plates[i].strip() if i < len(plates) else ""
                    if not make or not plate:
                        continue
                    try:
                        yr = int(years[i]) if i < len(years) and years[i] else None
                    except ValueError:
                        yr = None
                    if not yr:
                        continue

                    row_id_str = ids[i] if i < len(ids) else ""
                    row_id = int(row_id_str) if row_id_str.isdigit() else None

                    vehicle = existing_by_id.get(row_id) if row_id is not None else None
                    if vehicle is None:
                        vehicle = existing_by_plate.get(plate)
                    if vehicle is not None and vehicle.id in matched_ids:
                        vehicle = None  # already claimed by an earlier row this submission

                    is_new = vehicle is None
                    if is_new:
                        # Verified only by a DL-Verification pass (C3).
                        vehicle = Vehicle(owner=user, vehicle_verified=False)

                    model = models_list[i] if i < len(models_list) else ""
                    color = colors[i] if i < len(colors) else ""
                    # DL-Verification checked make, model, year, colour and
                    # plate against the photo; any real change voids that.
                    details_changed = not is_new and (
                        make.casefold() != (vehicle.make or "").casefold()
                        or model.strip().casefold() != (vehicle.model or "").strip().casefold()
                        or yr != vehicle.year
                        or color.strip().casefold() != (vehicle.color or "").strip().casefold()
                        or _norm_code(plate) != _norm_code(vehicle.plate)
                    )

                    vehicle.make      = make
                    vehicle.model     = model
                    vehicle.year      = yr
                    vehicle.color     = color
                    vehicle.plate     = plate
                    vehicle.is_primary = (i == 0)

                    # One field per row (named by the page on submit): the
                    # browser leaves out empty file inputs, so a shared
                    # field name would hand row 2's photo to row 1.
                    img_file = request.FILES.get(f"vehicle_image_file_{i}")
                    new_photo = None
                    if img_file and img_file.size > 0:
                        try:
                            new_photo = _resize_image(img_file, label=f"user={user.id} vehicle={plate!r}")
                        except ValidationError as e:
                            image_rejected = True
                            messages.error(request, f"Vehicle photo ({plate}): {e.messages[0]}")

                    was_verified = vehicle.vehicle_verified
                    if was_verified and (details_changed or new_photo is not None):
                        vehicle.vehicle_verified = False
                    if (is_new and user.driver_verified) or (was_verified and not vehicle.vehicle_verified):
                        # Verification checks the first vehicle in the list.
                        messages.warning(request, f"Vehicle {plate} needs to be verified before you can use it for rides. Put it first in your vehicle list and click Verify Identity.")

                    vehicle.save()
                    matched_ids.add(vehicle.id)

                    if new_photo is not None:
                        old_url = vehicle.photo_url
                        vehicle.photo_url = save_private_image(private_media.KIND_VEHICLE, user.id, new_photo)
                        vehicle.save(update_fields=["photo_url"])
                        delete_stored_file(old_url)

                # Any vehicle that existed before but wasn't in this
                # submission was explicitly removed by the user in the form.
                for vid, v in existing_by_id.items():
                    if vid not in matched_ids:
                        photo_url = v.photo_url
                        v.delete()
                        delete_stored_file(photo_url)

            if image_rejected:
                messages.warning(request, "Your other changes were saved.")
            else:
                messages.success(request, "Profile saved successfully.")
            return redirect("profile")

    priority_countries, countries = _dropdown_countries()
    reverify_due_at = user.driver_reverify_due_at
    verification_current = user.driver_verification_current
    context = {
        "user": user,
        "full_name": full_name,
        "email": user.email,
        "country_code": country_code,
        "phone_number": phone_number,
        "priority_countries": priority_countries,
        "countries": countries,
        "dob": dob_str,
        "gender": gender,
        "role": role,
        "bio": bio,
        "email_verified": user.email_verified,
        "license_number": license_number,
        "license_province": license_province,
        "license_expiry": license_expiry_str,
        "selfie_url": user.selfie_url or "",
        "avatar_url": user.avatar_url or "",
        "vehicles": vehicles,
        "errors": errors,
        "driver_verified": verification_current,
        "face_match_score": user.face_match_score,
        "reverify_due_at": reverify_due_at,
        # Verified against a license that has since expired.
        "license_expired": user.driver_verified and user.license_expired,
        # Verified driver with a vehicle that still needs a verification run.
        "has_unverified_vehicle": verification_current and any(
            not v["vehicle_verified"] for v in vehicles
        ),
        # Verified, but inside the reminder window before it lapses.
        "reverify_soon": verification_current and reverify_due_at is not None and (
            reverify_due_at - timezone.now() <= timedelta(days=settings.DRIVER_REVERIFY_REMINDER_DAYS)
        ),
        # Uploading a file as the verification selfie is a development
        # convenience only; in production the selfie must come from the
        # live camera.
        "allow_verify_selfie_upload": settings.DEBUG,
        "biometric_notice_version": settings.BIOMETRIC_NOTICE_VERSION,
        # Human review of the latest attempt (audit P-10): "pending" while
        # it's in the queue, "available" if a rejection can still be sent.
        "verification_review": _verification_review_state(user),
        "image_upload_max_mb": settings.IMAGE_UPLOAD_MAX_MB,
        "upload_request_max_mb": settings.UPLOAD_REQUEST_MAX_MB,
        # Was verified, and that verification has run out.
        "reverify_expired": not verification_current and reverify_due_at is not None
                            and timezone.now() >= reverify_due_at,
        "account_deletion_grace_days": settings.ACCOUNT_DELETION_GRACE_DAYS,
        # Users this user has blocked, each with an Unblock button (ISSUES.md A1).
        "blocked_users": [
            {
                "key":   encode_id(b.blocked_id),
                "name":  (b.blocked.full_name or "").title() or "Komüte user",
                "since": b.created_at,
            }
            for b in UserBlock.objects.filter(blocker=user).select_related("blocked").order_by("-created_at")
        ],
    }
    return render(request, "profile.html", context)


def post_ride_view(request):
    # request.user only: a session-stored user id outlived deactivation
    # (closed or banned accounts kept access to this view).
    user = request.user if request.user.is_authenticated else None

    if not user:
        return redirect("login")

    if user.role != "driver":
        messages.error(request, "Only drivers can post rides.")
        return redirect("profile")

    # Ride emails go to this address (audit S-10).
    if not user.email_verified:
        messages.error(request, _EMAIL_UNVERIFIED_MSG)
        return redirect("profile")

    if not user.dob:
        messages.error(request, "Please add your date of birth in your profile before posting a ride.")
        return redirect("profile")

    if not user.driver_verification_current:
        if user.driver_verified:
            messages.error(request, "Your identity verification has expired. Please re-verify to post a ride.")
        else:
            messages.error(request, "You must be verified to post a ride.")
        return redirect("profile")

    if not user.vehicles.filter(vehicle_verified=True).exists():
        messages.error(request, "You need at least one verified vehicle to post a ride.")
        return redirect("profile")

    vehicles = user.vehicles.all()
    errors = {}
    form = {}
    form_js = None

    if request.method == "POST":
        origin      = request.POST.get("origin", "").strip()
        destination = request.POST.get("destination", "").strip()
        date_str    = request.POST.get("date", "").strip()
        time_str    = request.POST.get("depart_time", "").strip()
        seats_str      = request.POST.get("seats", "").strip()
        vehicle_id     = request.POST.get("vehicle", "").strip()
        price_str        = request.POST.get("price", "").strip()
        notes            = request.POST.get("notes", "").strip()
        tz_name          = request.POST.get("tz", "").strip() or None
        driver_tz        = _zone_or_utc(tz_name)
        _prefs_raw = request.POST.get("preferences_json", "").strip()
        try:
            preferences_json = json.loads(_prefs_raw) if _prefs_raw else None
        except (ValueError, TypeError):
            preferences_json = None
        passenger_gender_pref = request.POST.get("passenger_gender_pref", "any").strip()
        if passenger_gender_pref not in ("any", "women_only", "men_only"):
            passenger_gender_pref = "any"
        is_recurring        = request.POST.get("is_recurring", "0") == "1"
        recurring_end_str   = request.POST.get("recurring_end_date", "").strip()
        recurring_days_str  = request.POST.get("recurring_days", "").strip()

        try:
            origin_lat = float(request.POST.get("origin_lat") or 0)
            origin_lng = float(request.POST.get("origin_lng") or 0)
            dest_lat   = float(request.POST.get("destination_lat") or 0)
            dest_lng   = float(request.POST.get("destination_lng") or 0)
        except (ValueError, TypeError):
            origin_lat = origin_lng = dest_lat = dest_lng = 0.0

        # The form's single-line value attributes need the raw submitted
        # strings, so a server-side error doesn't hand the driver back an
        # empty form; stops and other JS-managed fields go in form_js.
        form = {
            "origin": origin,
            "origin_lat": request.POST.get("origin_lat", "").strip(),
            "origin_lng": request.POST.get("origin_lng", "").strip(),
            "destination": destination,
            "destination_lat": request.POST.get("destination_lat", "").strip(),
            "destination_lng": request.POST.get("destination_lng", "").strip(),
            "depart_time": time_str,
            "vehicle": vehicle_id,
            "price": price_str,
            "notes": notes,
            "passenger_gender_pref": passenger_gender_pref,
        }
        form_js = {
            "date": date_str,
            "seats": seats_str,
            "prefs": [p for p in preferences_json if isinstance(p, str)] if isinstance(preferences_json, list) else [],
            "is_recurring": is_recurring,
            "recurring_end_date": recurring_end_str,
            "recurring_days": [d for d in recurring_days_str.split(",") if d.strip().isdigit()],
            "stops": [
                {"location": loc, "type": stype, "lat": slat, "lng": slng}
                for loc, stype, slat, slng in zip(
                    request.POST.getlist("stop_location"), request.POST.getlist("stop_type"),
                    request.POST.getlist("stop_lat"), request.POST.getlist("stop_lng"),
                )
                if loc.strip() and stype in ("pickup", "destination")
            ],
        }

        # 0,0 means the hidden lat/lng were never filled, i.e. the driver typed
        # a place but didn't pick one of the suggestions -- telling them it's
        # outside the US/Canada would send them looking for the wrong problem.
        if not origin:
            errors["origin"] = "Starting point is required."
        elif origin_lat == 0 and origin_lng == 0:
            errors["origin"] = "Please pick a location from the suggestions."
        elif area_error := _service_area_error(origin_lat, origin_lng, "pickup"):
            errors["origin"] = area_error
        if not destination:
            errors["destination"] = "Final destination is required."
        elif dest_lat == 0 and dest_lng == 0:
            errors["destination"] = "Please pick a location from the suggestions."
        elif area_error := _service_area_error(dest_lat, dest_lng, "drop-off"):
            errors["destination"] = area_error
        clean_stops, stops_error = _clean_stops(request.POST)
        if stops_error:
            errors["stops"] = stops_error
        if not date_str:
            errors["date"] = "Date is required."
        if not time_str:
            errors["depart_time"] = "Departure time is required."
        if not vehicle_id:
            errors["vehicle"] = "Please select a vehicle."

        seats = None
        if seats_str:
            try:
                seats = int(seats_str)
                if seats < 1 or seats > 8:
                    errors["seats"] = "Seats must be between 1 and 8."
            except ValueError:
                errors["seats"] = "Invalid seat count."
        else:
            errors["seats"] = "Seat count is required."

        price_cents = 0
        if not price_str:
            errors["price"] = "Price per seat is required."
        else:
            try:
                price_cents = _parse_dollars_to_cents(price_str)
                if price_cents <= 0:
                    errors["price"] = "Price per seat must be greater than 0."
                elif price_cents > _MAX_PRICE_CENTS:
                    errors["price"] = "Price per seat cannot exceed $1,000."
            except ValueError:
                errors["price"] = "Invalid price."

        depart_at = None
        depart_utc_str = request.POST.get('depart_utc', '').strip()
        if depart_utc_str and "date" not in errors and "depart_time" not in errors:
            try:
                depart_at = datetime.fromisoformat(depart_utc_str.replace('Z', '+00:00'))
                if timezone.is_naive(depart_at):
                    from datetime import timezone as _utc
                    depart_at = depart_at.replace(tzinfo=_utc.utc)
                if depart_at < timezone.now() + timedelta(minutes=30):
                    errors["depart_time"] = "Departure must be at least 30 minutes from now."
                elif depart_at.date() > _max_ride_date():
                    errors["date"] = "Rides can only be posted up to 4 months in advance."
            except ValueError:
                errors["depart_time"] = "Invalid departure date or time."
        elif date_str and time_str and "date" not in errors and "depart_time" not in errors:
            try:
                # No depart_utc (script didn't run): the form's date and time
                # are the driver's local ones.
                depart_at = timezone.make_aware(datetime.fromisoformat(f"{date_str}T{time_str}"), driver_tz)
                if depart_at < timezone.now() + timedelta(minutes=30):
                    errors["depart_time"] = "Departure must be at least 30 minutes from now."
                elif depart_at.date() > _max_ride_date():
                    errors["date"] = "Rides can only be posted up to 4 months in advance."
            except ValueError:
                errors["depart_time"] = "Invalid departure date or time."

        vehicle = None
        if vehicle_id and "vehicle" not in errors:
            vehicle = Vehicle.objects.filter(id=vehicle_id, owner=user).first()
            if not vehicle:
                errors["vehicle"] = "Invalid vehicle selected."
            elif not vehicle.vehicle_verified:
                errors["vehicle"] = "This vehicle isn't verified yet. Choose a verified vehicle."
                vehicle = None

        recurring_end = None
        recurring_days = []
        if is_recurring:
            if not recurring_end_str:
                errors["recurring_end_date"] = "End date is required for recurring rides."
            else:
                try:
                    recurring_end = date.fromisoformat(recurring_end_str)
                    if depart_at and recurring_end <= depart_at.astimezone(driver_tz).date():
                        errors["recurring_end_date"] = "End date must be after the departure date."
                    # Same 4-month limit as a single ride (audit D-8). One ride
                    # row per matching day is created below, so this also
                    # stops a one-request database flood.
                    elif recurring_end > _max_ride_date():
                        errors["recurring_end_date"] = "Recurring rides can repeat up to 4 months in advance."
                except ValueError:
                    errors["recurring_end_date"] = "Invalid end date."

            if recurring_days_str:
                try:
                    recurring_days = [int(d) for d in recurring_days_str.split(",") if d.strip()]
                except ValueError:
                    pass
            if not recurring_days:
                errors["recurring_days"] = "Select at least one day of the week."

        if not errors:
            duration_seconds = _parse_duration_seconds(request.POST.get("duration_seconds"))

            def _create_ride(depart_dt, group_id=None):
                arrive_dt = depart_dt + timedelta(seconds=duration_seconds) if duration_seconds > 0 else None
                ride = Rides.objects.create(
                    driver=user,
                    vehicle=vehicle,
                    origin=origin,
                    origin_lat=origin_lat,
                    origin_lng=origin_lng,
                    destination=destination,
                    dest_lat=dest_lat,
                    dest_lng=dest_lng,
                    depart_at=depart_dt,
                    arrive_at=arrive_dt,
                    seats_total=seats,
                    seats_available=seats,
                    price_cents=price_cents,
                    passenger_gender_pref=passenger_gender_pref,
                    notes=notes or None,
                    preferences_json=preferences_json or None,
                    recurring_group_id=group_id,
                    tz=tz_name,
                    expected_duration_seconds=duration_seconds if duration_seconds > 0 else None,
                )
                for stop in clean_stops:
                    RideStop.objects.create(
                        ride=ride, stop_order=stop['idx'] + 1,
                        location=stop['location'], lat=stop['lat'], lng=stop['lng'],
                    )
                return ride

            if is_recurring:
                # Weekdays and the clock time are the driver's local ones
                # (ISSUES.md H5): in UTC an 8 pm Toronto ride lands on the
                # next weekday, and DST would shift it by an hour.
                group_id = str(uuid.uuid4())
                local_depart = depart_at.astimezone(driver_tz)
                current = local_depart.date()
                ride_time = local_depart.time().replace(tzinfo=None)
                first_ride = None
                while current <= recurring_end:
                    if current.weekday() in recurring_days:
                        dt = datetime.combine(current, ride_time, tzinfo=driver_tz)
                        r = _create_ride(dt, group_id)
                        if first_ride is None:
                            first_ride = r
                    current += timedelta(days=1)
            else:
                first_ride = _create_ride(depart_at)

            return redirect("ride-success", ride_id=encode_id(first_ride.id))

    context = {
        "full_name": user.full_name,
        "vehicles": vehicles,
        "errors": errors,
        "form": form,
        "form_js": form_js,
        "show_form": bool(errors),
        "stop_detour_max_km":  settings.STOP_DETOUR_MAX_KM,
        "stop_detour_max_pct": settings.STOP_DETOUR_MAX_PCT,
    }
    return render(request, "post-ride.html", context)

@login_required
def search_rides_view(request):
    if request.user.role != "rider":
        messages.error(request, "Only riders can search rides.")
        return redirect("profile")

    search_from = request.POST.get('from', '').strip()
    search_to   = request.POST.get('to', '').strip()
    search_date = request.POST.get('date', '').strip()
    # Dates and time slots are the rider's local ones, matching how results
    # are displayed (the page converts depart_at to browser time).
    rider_tz    = _zone_or_utc(request.POST.get('tz', '').strip())
    rider_today = timezone.localdate(timezone=rider_tz)

    parsed_search_date = None
    if search_date:
        try:
            parsed_search_date = date.fromisoformat(search_date)
        except ValueError:
            search_date = ''  # treat as no date filter

    filter_seats      = request.POST.get('seats', '').strip()
    filter_max_price  = request.POST.get('max_price', '').strip()
    filter_time_slot  = request.POST.get('depart_time_slot', '').strip()
    filter_preference = request.POST.get('preference', '').strip()
    # Clamped so a huge value can't overflow an integer column in the query
    # or the RideSearch row (audit L-10); no ride has more than 8 seats.
    try:
        seats_needed = min(max(int(filter_seats), 1), 8) if filter_seats else None
    except ValueError:
        seats_needed = None

    search_performed = bool(search_from or search_to or search_date)
    rides_data = []
    errors = {}

    if parsed_search_date:
        if parsed_search_date < rider_today:
            errors['date'] = 'Search date cannot be in the past.'
        elif parsed_search_date > _max_ride_date(rider_today):
            errors['date'] = 'You can only search up to 4 months ahead.'

    _TIME_SLOT_HOURS = {
        'early_morning': (5, 8),
        'morning':       (8, 12),
        'afternoon':     (12, 17),
        'evening':       (17, 21),
    }

    if search_performed:
        try:
            s_origin_lat = float(request.POST.get('origin_lat') or 0)
            s_origin_lng = float(request.POST.get('origin_lng') or 0)
            s_dest_lat   = float(request.POST.get('dest_lat') or 0)
            s_dest_lng   = float(request.POST.get('dest_lng') or 0)
        except (ValueError, TypeError):
            s_origin_lat = s_origin_lng = s_dest_lat = s_dest_lng = 0.0

        if search_from and (s_origin_lat or s_origin_lng):
            if area_error := _service_area_error(s_origin_lat, s_origin_lng, 'pickup'):
                errors['from'] = area_error
        if search_to and (s_dest_lat or s_dest_lng):
            if area_error := _service_area_error(s_dest_lat, s_dest_lng, 'drop-off'):
                errors['to'] = area_error

    if search_performed and not errors:
        if parsed_search_date:
            try:
                RideSearch.objects.create(
                    user=request.user if request.user.is_authenticated else None,
                    origin=search_from,
                    origin_lat=s_origin_lat,
                    origin_lng=s_origin_lng,
                    destination=search_to,
                    dest_lat=s_dest_lat,
                    dest_lng=s_dest_lng,
                    search_date=parsed_search_date,
                    passenger_count=seats_needed or 1,
                )
            except (ValueError, TypeError):
                pass

        qs = (
            Rides.objects
            .filter(status__in=['posted', 'full'], depart_at__gte=timezone.now())
            # Blocked either way: neither sees the other's rides (ISSUES.md A1).
            .exclude(driver_id__in=UserBlock.objects.hidden_ids(request.user.id))
            .select_related('driver', 'vehicle')
            .prefetch_related('stops')
            .order_by('depart_at')
        )

        # Filter out gender-restricted rides that don't match the rider's gender
        user_gender = request.user.gender or ''
        if user_gender == 'female':
            qs = qs.exclude(passenger_gender_pref='men_only')
        elif user_gender == 'male':
            qs = qs.exclude(passenger_gender_pref='women_only')
        else:
            qs = qs.filter(passenger_gender_pref='any')

        # Coordinate-based proximity search (falls back to text if no coords)
        use_origin_coords = bool(s_origin_lat and s_origin_lng)
        use_dest_coords   = bool(s_dest_lat and s_dest_lng)

        if search_from and not use_origin_coords:
            qs = qs.filter(
                Q(origin__icontains=search_from) | Q(stops__location__icontains=search_from)
            ).distinct()
        if search_to and not use_dest_coords:
            qs = qs.filter(
                Q(destination__icontains=search_to) | Q(stops__location__icontains=search_to)
            ).distinct()

        if parsed_search_date:
            day_start, day_end = _local_day_bounds(parsed_search_date, rider_tz)
            qs = qs.filter(depart_at__gte=day_start, depart_at__lt=day_end)

        if seats_needed:
            qs = qs.filter(seats_available__gte=seats_needed)
        # Clamped like seats_needed: no ride costs over $1,000 a seat.
        if filter_max_price:
            try:
                max_cents = _parse_dollars_to_cents(filter_max_price)
                qs = qs.filter(price_cents__lte=min(max(max_cents, 0), _MAX_PRICE_CENTS))
            except ValueError:
                pass
        if filter_time_slot in _TIME_SLOT_HOURS:
            start_h, end_h = _TIME_SLOT_HOURS[filter_time_slot]
            qs = (qs.annotate(local_hour=ExtractHour('depart_at', tzinfo=rider_tz))
                    .filter(local_hour__gte=start_h, local_hour__lt=end_h))
        if filter_preference:
            qs = qs.filter(preferences_json__icontains=filter_preference)

        # Proximity: the SQL box drops far-away rides (audit F-2), then the
        # haversine test below keeps those within the radius of the ride's
        # endpoint or one of its stops.
        radius = settings.SEARCH_RADIUS_KM
        if use_origin_coords:
            qs = qs.filter(
                _geo_box_q(s_origin_lat, s_origin_lng, radius, 'origin_lat', 'origin_lng')
                | _geo_box_q(s_origin_lat, s_origin_lng, radius, 'stops__lat', 'stops__lng')
            ).distinct()
        if use_dest_coords:
            qs = qs.filter(
                _geo_box_q(s_dest_lat, s_dest_lng, radius, 'dest_lat', 'dest_lng')
                | _geo_box_q(s_dest_lat, s_dest_lng, radius, 'stops__lat', 'stops__lng')
            ).distinct()

        def _near(lat, lng, ride_lat, ride_lng, stops):
            return (_haversine_km(lat, lng, ride_lat, ride_lng) <= radius
                    or any(_haversine_km(lat, lng, st.lat, st.lng) <= radius for st in stops))

        if use_origin_coords or use_dest_coords:
            rides = [
                ride for ride in qs
                if (not use_origin_coords or _near(s_origin_lat, s_origin_lng,
                                                   ride.origin_lat, ride.origin_lng, ride.stops.all()))
                and (not use_dest_coords or _near(s_dest_lat, s_dest_lng,
                                                  ride.dest_lat, ride.dest_lng, ride.stops.all()))
            ]
        else:
            rides = qs

    else:
        rides = []

    # Only the shown page is turned into cards; the rest stay as rows.
    paginator = Paginator(rides, 10)
    page_obj  = paginator.get_page(request.POST.get('page', 1))
    total_count = paginator.count

    for ride in page_obj.object_list:
        name   = (ride.driver.full_name or '').title()
        parts  = name.split()
        initials = ''.join(p[0].upper() for p in parts[:2]) if parts else '?'

        rating       = ride.driver.avg_rating or 0.0
        rating_count = ride.driver.total_rides or 0
        filled       = round(rating)
        rating_stars = '★' * filled + '☆' * (5 - filled)

        price_display = f"${ride.price_cents / 100:.2f}" if ride.price_cents else "Free"

        prefs = []
        if ride.preferences_json:
            prefs = ride.preferences_json if isinstance(ride.preferences_json, list) else []

        v = ride.vehicle
        rides_data.append({
            'id':                  encode_id(ride.id),
            'origin':              ride.origin,
            'destination':         ride.destination,
            'depart_at':           ride.depart_at,
            'price_display':       price_display,
            'seats_available':     ride.seats_available,
            'seats_total':         ride.seats_total,
            'preferences':         prefs,
            'stops':               list(ride.stops.all()),
            'driver_name':         name,
            'driver_initials':     initials,
            'driver_is_verified':  ride.driver.driver_verified,
            'driver_rating':       round(rating, 1),
            'driver_rating_count': rating_count,
            'driver_rating_stars': rating_stars,
            'vehicle_color':       v.color or 'silver',
            'vehicle_display':     f"{v.year} {v.make} {v.model}",
            'vehicle_plate':       v.plate,
        })

    page_obj.object_list = rides_data

    return render(request, 'search-rides.html', {
        'rides':             page_obj,
        'total_count':       total_count,
        'page_obj':          page_obj,
        'search_from':       search_from,
        'search_to':         search_to,
        'search_date':       search_date,
        'search_performed':  search_performed,
        'errors':            errors,
        'filter_seats':      filter_seats,
        'filter_max_price':  filter_max_price,
        'filter_time_slot':  filter_time_slot,
        'filter_preference': filter_preference,
    })


def about_view(request):
    return render(request, "about.html")


def how_it_works_view(request):
    return render(request, "how-it-works.html")


def _gender_pref_allows(ride, user):
    """Whether `user` may join `ride` under its passenger gender preference.
    Same rule as the search filter in search_rides_view, which only hides
    the ride; booking and invites check it here (audit L-8)."""
    if ride.passenger_gender_pref == 'women_only':
        return user.gender == 'female'
    if ride.passenger_gender_pref == 'men_only':
        return user.gender == 'male'
    return True


_GENDER_PREF_MSG = "This ride is limited to passengers of a different gender."


@login_required
def create_ride_booking(request, ride_id):
    real_ride_id = decode_id(ride_id)
    if not real_ride_id:
        raise Http404

    if request.user.role != "rider":
        messages.error(request, "Only riders can book rides.")
        return redirect("profile")

    ride = get_object_or_404(
        Rides.objects.select_related('driver', 'vehicle').prefetch_related('stops'),
        id=real_ride_id,
    )

    if ride.driver == request.user:
        messages.error(request, "You cannot book your own ride.")
        return redirect("my-rides")

    # Hidden from search when either has blocked the other (ISSUES.md A1),
    # so a direct link doesn't show it either.
    if UserBlock.objects.between(ride.driver_id, request.user.id):
        if request.method == "POST":
            return JsonResponse({'ok': False, 'error': "You can't book this ride."}, status=403)
        raise Http404

    if request.method == "POST":
        # Both checks cover "Chat with driver" too: a draft can later be
        # confirmed in chat or used to leave a review. Booking emails go to
        # the rider's address, so it must be verified (audit S-10).
        if not request.user.email_verified:
            return JsonResponse({'ok': False, 'error': _EMAIL_UNVERIFIED_MSG}, status=403)
        # Accounts from before DOB was asked at signup (audit L-5).
        if not request.user.dob:
            return JsonResponse({'ok': False, 'error': _DOB_MISSING_MSG}, status=403)
        if ride.status not in ('posted', 'full') or ride.depart_at <= timezone.now():
            return JsonResponse({'ok': False, 'error': 'This ride is no longer available.'}, status=400)
        if not _gender_pref_allows(ride, request.user):
            return JsonResponse({'ok': False, 'error': _GENDER_PREF_MSG}, status=403)
        intent = request.POST.get("intent", "book")
        try:
            seats = int(request.POST.get("seats", 1))
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': 'Invalid seat count.'}, status=400)
        if seats < 1 or seats > 8:
            return JsonResponse({'ok': False, 'error': 'Seats must be between 1 and 8.'}, status=400)
        pickup_stop_id   = request.POST.get("pickup_stop_id")
        dropoff_stop_id  = request.POST.get("dropoff_stop_id")

        pickup_stop  = RideStop.objects.filter(id=pickup_stop_id,  ride=ride).first() if pickup_stop_id  else None
        dropoff_stop = RideStop.objects.filter(id=dropoff_stop_id, ride=ride).first() if dropoff_stop_id else None

        existing = RideBooking.objects.filter(
            ride=ride,
            passenger=request.user,
            status__in=[RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED],
        ).first()

        if existing and existing.status in (RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
            return JsonResponse({'ok': False, 'error': 'You have already requested this ride.'}, status=400)

        from .chat_tokens import make_chat_token

        if intent == "chat":
            # Pre-request negotiation: open a chat without reserving a seat or notifying the driver.
            if existing and existing.status == RideBooking.STATUS_DRAFT:
                return JsonResponse({'ok': True, 'chat_token': make_chat_token(existing.id)})
            draft = RideBooking.objects.create(
                ride=ride,
                passenger=request.user,
                pickup_stop=pickup_stop,
                dropoff_stop=dropoff_stop,
                seats=seats,
                agreed_price_cents=ride.price_cents * seats + settings.SERVICE_FEE_CENTS,
                status=RideBooking.STATUS_DRAFT,
            )
            return JsonResponse({'ok': True, 'chat_token': make_chat_token(draft.id)})

        updated = Rides.objects.filter(
            id=ride.id, seats_available__gte=seats
        ).update(seats_available=F('seats_available') - seats)
        if not updated:
            return JsonResponse({'ok': False, 'error': 'Not enough seats available.'}, status=400)
        Rides.objects.filter(id=ride.id, seats_available=0).update(status='full')

        if existing and existing.status == RideBooking.STATUS_DRAFT:
            # Promote the draft in place — carries over its PriceOffer/Message history
            # and keeps the chat token (which encodes only the booking id) still valid.
            booking = existing
            booking.pickup_stop  = pickup_stop
            booking.dropoff_stop = dropoff_stop
            booking.seats        = seats
            booking.status       = RideBooking.STATUS_REQUESTED
            booking.save(update_fields=['pickup_stop', 'dropoff_stop', 'seats', 'status'])
        else:
            booking = RideBooking.objects.create(
                ride=ride,
                passenger=request.user,
                pickup_stop=pickup_stop,
                dropoff_stop=dropoff_stop,
                seats=seats,
                agreed_price_cents=ride.price_cents * seats + settings.SERVICE_FEE_CENTS,
                status=RideBooking.STATUS_REQUESTED,
                confirmed_at=None,
            )

        token = make_chat_token(booking.id)
        notify_bookings_changed([ride.driver_id], 'You have a new booking request.')
        _send_booking_request_email(booking)
        return JsonResponse({'ok': True, 'chat_token': token})

    # GET — build context
    driver = ride.driver
    name   = (driver.full_name or '').title()
    parts  = name.split()
    initials     = ''.join(p[0].upper() for p in parts[:2]) if parts else '?'
    rating       = driver.avg_rating or 0.0
    rating_count = driver.total_rides or 0
    filled       = round(rating)
    rating_stars = '★' * filled + '☆' * (5 - filled)

    prefs = []
    if ride.preferences_json:
        prefs = ride.preferences_json if isinstance(ride.preferences_json, list) else []

    price_display         = f"${ride.price_cents / 100:.2f}" if ride.price_cents else "Free"
    service_fee_display   = f"${settings.SERVICE_FEE_CENTS / 100:.2f}"
    initial_total_display = f"${(ride.price_cents + settings.SERVICE_FEE_CENTS) / 100:.2f}"

    v = ride.vehicle
    ride_ctx = {
        'id':                  encode_id(ride.id),
        'origin':              ride.origin,
        'origin_lat':          ride.origin_lat,
        'origin_lng':          ride.origin_lng,
        'destination':         ride.destination,
        'dest_lat':            ride.dest_lat,
        'dest_lng':            ride.dest_lng,
        'depart_at':           ride.depart_at,
        'arrive_at':           ride.arrive_at,
        'price_display':       price_display,
        'price_cents':         ride.price_cents,
        'seats_available':     ride.seats_available,
        'preferences':         prefs,
        'stops':               list(ride.stops.all()),
        'notes':               ride.notes,
        'driver_name':         name,
        'driver_initials':     initials,
        'driver_is_verified':  driver.driver_verified,
        'driver_rating':       round(rating, 1),
        'driver_rating_count': rating_count,
        'driver_rating_stars': rating_stars,
        # Filled in below only for a confirmed booking (privacy policy s.10).
        'driver_phone':        '',
        'driver_gender':       driver.get_gender_display() if driver.gender else '',
        'vehicle':             v,
        'vehicle_color':       v.color or 'silver',
        'vehicle_plate':       v.plate,
    }

    from .chat_tokens import make_chat_token
    existing_booking = RideBooking.objects.filter(
        ride=ride,
        passenger=request.user,
        status__in=[RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED],
    ).first()
    existing_chat_token    = make_chat_token(existing_booking.id) if existing_booking else None
    existing_booking_status = existing_booking.status if existing_booking else None
    # The driver's phone is shared only to coordinate a confirmed trip.
    if existing_booking_status == RideBooking.STATUS_CONFIRMED:
        ride_ctx['driver_phone'] = driver.phone or ''

    return render(request, 'booking-details.html', {
        'ride':                    ride_ctx,
        'service_fee_cents':       settings.SERVICE_FEE_CENTS,
        'service_fee_display':     service_fee_display,
        'initial_total_display':   initial_total_display,
        'existing_chat_token':     existing_chat_token,
        'existing_booking_status': existing_booking_status,
    })


BLOCKED_CHAT_MESSAGE = "You can't message this user."


def _chat_status(booking):
    """Header badge and lock state of a booking's chat page. Shared by
    chat_view and the live booking_status event (ISSUES.md H3), so a page
    updated over the socket matches a freshly loaded one."""
    ride_status = booking.ride.status
    closed_message = ''
    if booking.status == RideBooking.STATUS_CANCELLED:
        label, status_class = 'Cancelled', 'cancelled'
        closed_message = 'Booking was cancelled — messaging is closed.'
    elif booking.status == RideBooking.STATUS_REJECTED:
        label, status_class = 'Declined', 'cancelled'
        closed_message = 'Booking request was declined — messaging is closed.'
    elif booking.status == RideBooking.STATUS_COMPLETED or ride_status == 'completed':
        label, status_class = 'Completed', 'completed'
        closed_message = 'Ride completed — messaging is closed.'
    elif ride_status == 'cancelled':
        label, status_class = 'Ride Cancelled', 'cancelled'
        closed_message = 'Ride was cancelled — messaging is closed.'
    elif booking.status == RideBooking.STATUS_CONFIRMED:
        label, status_class = 'Confirmed', 'active'
    elif booking.status == RideBooking.STATUS_DRAFT:
        label, status_class = 'Negotiating', 'active'
    else:
        label, status_class = 'Pending', 'pending'
    # A block in either direction closes the chat (ISSUES.md A1). The badge
    # keeps the booking's own status: the booking itself is left alone.
    blocked = UserBlock.objects.between(booking.ride.driver_id, booking.passenger_id)
    if blocked and not closed_message:
        closed_message = BLOCKED_CHAT_MESSAGE
    agreed_price = None
    if booking.status in (RideBooking.STATUS_CONFIRMED, RideBooking.STATUS_COMPLETED) and booking.agreed_price_cents:
        agreed_price = booking.agreed_price_cents // 100
    return {
        'agreed_price':   agreed_price,
        'label':          label,
        'status_class':   status_class,
        'chat_locked':    status_class in ('cancelled', 'completed') or blocked,
        # Negotiation requires a confirmed booking — blocked while approval is pending
        'nego_locked':    booking.status == RideBooking.STATUS_REQUESTED,
        'closed_message': closed_message,
    }


def _notify_booking_status(booking):
    """Push a booking's new status to its open chat pages (ISSUES.md H3), so
    they lock or unlock without a refresh. Best effort: a channel-layer
    failure is logged, never raised, so the status change itself stands."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from .consumers import chat_group_name
    try:
        async_to_sync(get_channel_layer().group_send)(chat_group_name(booking.id), {
            'type':   'booking_status',
            'status': booking.status,
            **_chat_status(booking),
        })
    except Exception:
        logger.warning('Could not push status of booking %s to its chat', booking.id, exc_info=True)


@login_required
def cancel_booking_view(request, booking_id):
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)

    real_booking_id = decode_id(booking_id)
    if not real_booking_id:
        raise Http404
    booking = get_object_or_404(RideBooking, id=real_booking_id)

    if booking.passenger != request.user:
        return JsonResponse({"error": "Not authorized"}, status=403)

    if booking.status in (RideBooking.STATUS_CANCELLED, RideBooking.STATUS_REJECTED, RideBooking.STATUS_COMPLETED):
        return JsonResponse({"error": f"Booking is already {booking.status}."}, status=400)

    if booking.ride.status in ('completed', 'cancelled'):
        return JsonResponse({"error": f"Cannot cancel — ride is already {booking.ride.status}."}, status=400)

    if booking.ride.depart_at <= timezone.now():
        return JsonResponse({"error": "Cannot cancel after the ride has departed."}, status=400)

    ride = booking.ride
    cancelled_at = timezone.now()

    updated = RideBooking.objects.filter(id=booking.id).exclude(
        status__in=(RideBooking.STATUS_CANCELLED, RideBooking.STATUS_REJECTED, RideBooking.STATUS_COMPLETED),
    ).update(status=RideBooking.STATUS_CANCELLED, cancelled_at=cancelled_at)
    if not updated:
        return JsonResponse({"error": "Booking is already cancelled."}, status=400)
    booking.status = RideBooking.STATUS_CANCELLED
    booking.cancelled_at = cancelled_at

    Rides.objects.filter(id=ride.id).update(
        seats_available=Least(F('seats_available') + booking.seats, ride.seats_total)
    )
    Rides.objects.filter(id=ride.id, status='full').update(status='posted')
    _notify_booking_status(booking)
    notify_bookings_changed([ride.driver_id], 'A passenger cancelled their booking.')

    _send_booking_cancelled_emails(booking)

    return JsonResponse({"success": True})


@login_required
@require_POST
def accept_booking_view(request, booking_id):
    real_id = decode_id(booking_id)
    if not real_id:
        raise Http404
    booking = get_object_or_404(
        RideBooking.objects.select_related('ride__driver', 'passenger'),
        id=real_id,
    )

    if booking.ride.driver != request.user:
        return JsonResponse({'error': 'Forbidden.'}, status=403)

    if booking.status != RideBooking.STATUS_REQUESTED:
        return JsonResponse({'error': f'Cannot accept a booking that is already {booking.status}.'}, status=400)

    if not booking.ride.driver.driver_verification_current:
        return JsonResponse({'error': 'Please verify your identity on your profile page before accepting bookings.'}, status=403)

    # A request sent before a block stays pending; the driver can still
    # reject it and the rider can cancel it, but it can't be confirmed.
    if UserBlock.objects.between(booking.ride.driver_id, booking.passenger_id):
        return JsonResponse({'error': "You can't accept this booking request."}, status=403)

    # Conditional, like reject and cancel: a cancel that lands between the
    # check above and this write must not be overwritten to confirmed after
    # its seat was already released.
    confirmed_at = timezone.now()
    updated = RideBooking.objects.filter(
        id=booking.id, status=RideBooking.STATUS_REQUESTED,
    ).update(status=RideBooking.STATUS_CONFIRMED, confirmed_at=confirmed_at)
    if not updated:
        return JsonResponse({'error': 'This booking request is no longer pending.'}, status=400)
    booking.status = RideBooking.STATUS_CONFIRMED
    booking.confirmed_at = confirmed_at
    _notify_booking_status(booking)
    notify_bookings_changed([booking.passenger_id], 'Your booking request was accepted.')

    _send_booking_confirmation_emails(booking)

    return JsonResponse({'ok': True})


@login_required
@require_POST
def reject_booking_view(request, booking_id):
    real_id = decode_id(booking_id)
    if not real_id:
        raise Http404
    booking = get_object_or_404(
        RideBooking.objects.select_related('ride__driver', 'ride', 'passenger'),
        id=real_id,
    )

    if booking.ride.driver != request.user:
        return JsonResponse({'error': 'Forbidden.'}, status=403)

    if booking.status != RideBooking.STATUS_REQUESTED:
        return JsonResponse({'error': f'Cannot reject a booking that is already {booking.status}.'}, status=400)

    updated = RideBooking.objects.filter(
        id=booking.id, status=RideBooking.STATUS_REQUESTED,
    ).update(status=RideBooking.STATUS_REJECTED, rejected_at=timezone.now())
    if not updated:
        return JsonResponse({'error': f'Cannot reject a booking that is already {booking.status}.'}, status=400)
    booking.status = RideBooking.STATUS_REJECTED

    ride = booking.ride
    Rides.objects.filter(id=ride.id).update(
        seats_available=Least(F('seats_available') + booking.seats, ride.seats_total)
    )
    Rides.objects.filter(id=ride.id, status='full').update(status='posted')
    _notify_booking_status(booking)
    notify_bookings_changed([booking.passenger_id], 'Your booking request was declined.')

    _send_booking_rejected_email(booking)

    return JsonResponse({'ok': True})


@login_required
def cancel_ride_view(request, ride_id):
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)

    real_ride_id = decode_id(ride_id)
    if not real_ride_id:
        raise Http404
    ride = get_object_or_404(Rides, id=real_ride_id, driver=request.user)

    if ride.status in ('completed', 'cancelled'):
        return JsonResponse({"error": f"Cannot cancel — ride is already {ride.status}."}, status=400)

    if ride.depart_at <= timezone.now():
        return JsonResponse({"error": "Cannot cancel after the ride has departed."}, status=400)

    affected_bookings = list(
        ride.bookings
        .filter(status__in=(RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED))
        .select_related('passenger')
    )

    ride.bookings.filter(
        status__in=(RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED)
    ).update(status=RideBooking.STATUS_CANCELLED, cancelled_at=timezone.now())

    ride.status = 'cancelled'
    ride.cancelled_at = timezone.now()
    ride.save(update_fields=['status', 'cancelled_at'])
    for booking in affected_bookings:
        booking.status = RideBooking.STATUS_CANCELLED
        booking.ride = ride
        _notify_booking_status(booking)
    # Pre-request chats stay drafts, but their chat closes with the ride.
    drafts = list(ride.bookings.filter(status=RideBooking.STATUS_DRAFT))
    for booking in drafts:
        booking.ride = ride
        _notify_booking_status(booking)
    notify_bookings_changed(
        [b.passenger_id for b in affected_bookings + drafts],
        'The driver cancelled a ride you booked or asked about.',
    )

    _send_ride_cancelled_emails(ride, affected_bookings)

    return JsonResponse({"success": True})


@login_required
def my_rides_view(request):
    user = request.user
    from .chat_tokens import make_chat_token

    # ── DRIVER: posted rides ──────────────────────────────────────
    RIDE_STATUS_TO_UI = {
        'posted': 'active', 'full': 'upcoming',
        'completed': 'completed', 'cancelled': 'cancelled',
    }
    RIDE_UI_LABEL = {
        'active': 'Active', 'upcoming': 'Upcoming',
        'completed': 'Completed', 'cancelled': 'Cancelled',
    }

    rides_qs = (
        Rides.objects
        .filter(driver=user)
        .prefetch_related('stops', 'bookings__passenger')
        .order_by('-depart_at')
    )

    rides_data = []
    total_earned_cents = 0
    passengers_helped = 0
    MAX_AVATARS = 4

    for ride in rides_qs:
        booked = ride.seats_total - ride.seats_available
        ui_status = RIDE_STATUS_TO_UI.get(ride.status, 'active')

        if ride.status == 'completed':
            total_earned_cents += ride.price_cents * booked
            passengers_helped += booked

        stops = list(ride.stops.all())
        prefs = []
        if ride.preferences_json:
            prefs = ride.preferences_json if isinstance(ride.preferences_json, list) else []

        all_bookings       = list(ride.bookings.all())
        active_bookings    = [b for b in all_bookings if b.status not in ('cancelled', 'rejected', 'draft')]
        pending_bookings   = [b for b in all_bookings if b.status == 'requested']

        passenger_initials = []
        passengers = []
        for b in active_bookings[:MAX_AVATARS]:
            name = (b.passenger.full_name or '').title()
            parts = name.split()
            passenger_initials.append(''.join(p[0].upper() for p in parts[:2]) if parts else '?')

        for b in pending_bookings:
            name = (b.passenger.full_name or '').title()
            parts = name.split()
            passengers.append({
                'name':       name or _UNNAMED_USER,
                'initials':   ''.join(p[0].upper() for p in parts[:2]) if parts else '?',
                'chat_token': make_chat_token(b.id),
            })

        empty_count = min(ride.seats_available, MAX_AVATARS - len(passenger_initials))
        price_display = f"${ride.price_cents / 100:.2f}" if ride.price_cents else "Free"

        rides_data.append({
            'id':                encode_id(ride.id),
            'origin':            ride.origin,
            'destination':       ride.destination,
            'stops':             stops,
            'depart_at':         ride.depart_at,
            'price_display':     price_display,
            'seats_total':       ride.seats_total,
            'seats_available':   ride.seats_available,
            'booked':            booked,
            'status':            ride.status,
            'ui_status':         ui_status,
            'ui_label':          RIDE_UI_LABEL[ui_status],
            'recurring':         bool(ride.recurring_group_id),
            'preferences':       prefs,
            'passenger_initials':passenger_initials,
            'passengers':        passengers,
            'empty_slots':       list(range(max(0, empty_count))),
        })

    total_rides = len(rides_data)
    ride_counts = {'all': total_rides, 'active': 0, 'upcoming': 0, 'completed': 0, 'cancelled': 0}
    for r in rides_data:
        ride_counts[r['ui_status']] += 1

    # ── RIDER: booked rides ───────────────────────────────────────
    BOOKING_STATUS_TO_UI = {
        'requested':  'pending',
        'confirmed':  'confirmed',
        'rejected':   'cancelled',
        'cancelled':  'cancelled',
        'completed':  'completed',
    }
    BOOKING_UI_LABEL = {
        'pending':   'Pending',
        'confirmed': 'Confirmed',
        'cancelled': 'Cancelled',
        'completed': 'Completed',
    }

    bookings_qs = (
        RideBooking.objects
        .filter(passenger=user)
        .exclude(status=RideBooking.STATUS_DRAFT)
        .select_related('ride__driver', 'ride__vehicle', 'ride')
        .order_by('-booked_at')
    )

    bookings_data = []
    total_spent_cents = 0
    rides_taken = 0

    for b in bookings_qs:
        ride = b.ride
        driver = ride.driver
        driver_name = (driver.full_name or '').title()
        parts = driver_name.split()
        driver_initials = ''.join(p[0].upper() for p in parts[:2]) if parts else '?'

        ui_status = BOOKING_STATUS_TO_UI.get(b.status, 'pending')

        # Treat a confirmed booking on a completed ride as completed
        if ui_status == 'confirmed' and ride.status == 'completed':
            ui_status = 'completed'

        if ui_status == 'completed':
            total_spent_cents += b.agreed_price_cents
            rides_taken += 1

        price_display = f"${b.agreed_price_cents / 100:.2f}" if b.agreed_price_cents else "Free"

        bookings_data.append({
            'id':                encode_id(b.id),
            'booking_id':        encode_id(b.id),
            'ride_id':           encode_id(ride.id),
            'origin':            ride.origin,
            'destination':       ride.destination,
            'depart_at':         ride.depart_at,
            'seats':             b.seats,
            'price_display':     price_display,
            'status':            b.status,
            'ui_status':         ui_status,
            'ui_label':          BOOKING_UI_LABEL[ui_status],
            'booked_at':         b.booked_at,
            'driver_name':       driver_name,
            'driver_initials':   driver_initials,
            'driver_verified':   driver.driver_verified,
            'chat_token':        make_chat_token(b.id),
        })

    booking_counts = {'all': len(bookings_data), 'pending': 0, 'confirmed': 0, 'completed': 0, 'cancelled': 0}
    for b in bookings_data:
        if b['ui_status'] in booking_counts:
            booking_counts[b['ui_status']] += 1

    nav_unread_count = Message.objects.filter(
        ride__driver=user, receiver=user, read_at__isnull=True
    ).count()
    nav_pending_count = RideBooking.objects.filter(
        ride__driver=user, status=RideBooking.STATUS_REQUESTED
    ).count()

    rider_nav_unread_count = Message.objects.filter(
        ride__bookings__passenger=user, receiver=user, read_at__isnull=True
    ).distinct().count()
    rider_nav_pending_count = RideBooking.objects.filter(
        passenger=user, status=RideBooking.STATUS_REQUESTED
    ).count()

    return render(request, 'my-rides.html', {
        'bookings_fingerprint': booking_fingerprint(user),
        # driver
        'rides':             rides_data,
        'total_rides':       total_rides,
        'passengers_helped': passengers_helped,
        'total_earned':      f"${total_earned_cents / 100:.2f}",
        'co2_saved':         f"{passengers_helped * 8} kg",
        'counts':            ride_counts,
        'nav_unread_count':  nav_unread_count,
        'nav_inbox_count':   nav_unread_count + nav_pending_count,
        # rider
        'bookings':           bookings_data,
        'rides_taken':        rides_taken,
        'total_spent':        f"${total_spent_cents / 100:.2f}",
        'booking_counts':     booking_counts,
        'rider_inbox_count':  rider_nav_unread_count + rider_nav_pending_count,
    })


@login_required
def chat_view(request, token):
    from .chat_tokens import resolve_chat_token

    booking_id = resolve_chat_token(token)
    if booking_id is None:
        raise Http404

    booking = get_object_or_404(
        RideBooking.objects.select_related('ride__driver', 'passenger'),
        id=booking_id,
    )
    if request.user not in (booking.ride.driver, booking.passenger):
        raise Http404

    other_user = booking.ride.driver if request.user == booking.passenger else booking.passenger
    initials = lambda u: ''.join(p[0].upper() for p in u.full_name.split()[:2])

    history = (
        Message.objects
        .filter(ride=booking.ride)
        .filter(
            Q(sender=request.user, receiver=other_user) |
            Q(sender=other_user, receiver=request.user)
        )
        .select_related('sender')
    )

    # Mark unread messages (received by current user) as read
    history.filter(receiver=request.user, read_at__isnull=True).update(read_at=timezone.now())

    history_json = [
        {
            'body':      m.body,
            'sender_id': m.sender_id,
            'initials':  initials(m.sender),
            'time':      m.created_at.strftime('%I:%M %p').lstrip('0'),
        }
        for m in history
    ]

    status = _chat_status(booking)
    agreed_price = status['agreed_price']

    # Latest pending offer from the OTHER user (incoming — needs a response)
    pending_incoming_price = None
    pending_incoming_offer_id = None
    # Latest pending offer from the CURRENT user (outgoing — waiting for response)
    pending_outgoing_price = None
    if agreed_price is None:
        pending_incoming = (
            PriceOffer.objects
            .filter(booking=booking, proposed_by=other_user, status=PriceOffer.STATUS_PENDING)
            .order_by('-created_at')
            .first()
        )
        pending_incoming_price = pending_incoming.price_cents // 100 if pending_incoming else None
        pending_incoming_offer_id = pending_incoming.id if pending_incoming else None

        pending_outgoing = (
            PriceOffer.objects
            .filter(booking=booking, proposed_by=request.user, status=PriceOffer.STATUS_PENDING)
            .order_by('-created_at')
            .first()
        )
        pending_outgoing_price = pending_outgoing.price_cents // 100 if pending_outgoing else None

    return render(request, 'chat.html', {
        'chat_token':             token,
        'booking':                booking,
        'ride':                   booking.ride,
        'other_user':             other_user,
        'other_user_initials':    initials(other_user),
        'current_user_initials':  initials(request.user),
        'driver_price':           booking.ride.price_cents // 100,
        'history_json':           history_json,
        'is_driver':              request.user == booking.ride.driver,
        'agreed_price':           agreed_price,
        'pending_incoming_price': pending_incoming_price,
        'pending_incoming_offer_id': pending_incoming_offer_id,
        'pending_outgoing_price': pending_outgoing_price,
        'ride_status_label':      status['label'],
        'ride_status_class':      status['status_class'],
        'chat_is_locked':         status['chat_locked'],
        'nego_locked':            status['nego_locked'],
        'chat_closed_message':    status['closed_message'],
        # Report / block menu (ISSUES.md A1)
        'booking_key':            encode_id(booking.id),
        'blocked_by_me':          UserBlock.objects.filter(blocker=request.user, blocked=other_user).exists(),
    })


def privacy_policy_view(request):
    return render(request, 'privacy-policy.html')


def contact_view(request):
    if request.method != 'POST':
        return render(request, 'contact-us.html')

    if _rate_limited(request, 'contact'):
        return JsonResponse({'error': 'Too many messages sent. Please try again later.'}, status=429)

    name    = request.POST.get('name',    '').strip()[:100]
    email   = request.POST.get('email',   '').strip()
    role    = request.POST.get('role',    '').strip()
    topic   = request.POST.get('topic',   '').strip()
    subject = request.POST.get('subject', '').strip()
    message = request.POST.get('message', '').strip()

    if not all([name, email, topic, subject, message]):
        return JsonResponse({'error': 'Please fill in all required fields.'}, status=400)

    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({'error': 'Please enter a valid email address.'}, status=400)

    # Strip control/non-printable characters before this goes into the
    # Reply-To header value below.
    name = ''.join(ch for ch in name if ch.isprintable()).strip()
    if not name:
        return JsonResponse({'error': 'Please enter your name.'}, status=400)

    body = (
        f"New contact form submission from Komüte\n"
        f"{'─' * 45}\n\n"
        f"Name:     {name}\n"
        f"Email:    {email}\n"
        f"Role:     {role or '—'}\n"
        f"Topic:    {topic}\n"
        f"Subject:  {subject}\n\n"
        f"Message:\n{message}\n"
    )

    try:
        msg = EmailMultiAlternatives(
            subject=f"[Komüte Contact] {topic}: {subject}",
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[settings.CONTACT_EMAIL],
            reply_to=[formataddr((name, email))],
        )
        msg.send(fail_silently=False)
    except Exception:
        logger.exception('contact_view: could not send the contact email')
        return JsonResponse({'error': 'Could not send message. Please try again later.'}, status=500)

    return JsonResponse({'success': True})


def terms_view(request):
    return render(request, 'terms-condition.html')


def communityguide_view(request):
    return render(request, 'community-guidelines.html')


def accessibility_view(request):
    return render(request, 'accessibility.html')


# Signed-in areas: they redirect to login anyway, so crawlers skip them.
# The admin path is left out on purpose; listing it would publish it.
ROBOTS_DISALLOW = (
    '/profile/', '/my-rides/', '/driver-inbox/', '/rider-inbox/', '/driver-dashboard/',
    '/post-ride/', '/search-rides/', '/route-demand/', '/role-select/', '/accept-terms/',
    '/ride-', '/rides/', '/bookings/', '/reviews/', '/negotiations/', '/chat/', '/invite/',
    '/change-password/', '/reset-password/', '/delete-account/confirm/', '/deactivate/',
    '/accounts/', '/private-media/', '/logout/',
)


def robots_txt_view(request):
    """GET /robots.txt - keeps crawlers on the public pages and points them at
    the sitemap."""
    from django.contrib.sites.shortcuts import get_current_site
    lines = ['User-agent: *'] + [f'Disallow: {p}' for p in ROBOTS_DISALLOW]
    lines.append(f"Sitemap: https://{get_current_site(request).domain}{reverse('sitemap')}")
    return HttpResponse('\n'.join(lines) + '\n', content_type='text/plain')


@login_required
def accept_terms_view(request):
    """GET/POST /accept-terms/ - where LegalAcceptanceMiddleware sends a
    signed-in user who hasn't accepted the current Terms and Privacy Policy
    (audit P-4)."""
    user = request.user
    next_url = _safe_next(request)

    already = LegalAcceptance.objects.filter(
        user=user, terms_version=settings.TERMS_VERSION,
        privacy_version=settings.PRIVACY_VERSION).exists()
    if already:
        return redirect(next_url or "profile")

    error = None
    if request.method == "POST":
        if request.POST.get("agreed_to_terms") == "1":
            _record_legal_acceptance(request, user, LegalAcceptance.SOURCE_REACCEPT)
            return redirect(next_url or "profile")
        error = "Please tick the box to accept the Terms & Conditions and the Privacy Policy."

    return render(request, "accept-terms.html", {
        "next": next_url,
        "error": error,
        "is_update": user.legal_acceptances.exists(),
        "terms_version": settings.TERMS_VERSION,
        "privacy_version": settings.PRIVACY_VERSION,
    })



def logout_view(request):
    if not request.user.is_authenticated:
        messages.info(request, "You are not logged in.")
        return redirect("login")
    logout(request)
    return redirect('login')


@login_required
def edit_ride_view(request, ride_id):
    real_ride_id = decode_id(ride_id)
    if not real_ride_id:
        raise Http404
    ride = get_object_or_404(
        Rides.objects.prefetch_related('stops'),
        id=real_ride_id,
        driver=request.user,
    )

    if ride.status != 'posted':
        messages.error(request, "Cannot edit a ride that is cancelled or completed.")
        return redirect('my-rides')

    active_bookings = ride.bookings.filter(
        status__in=[RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED],
    ).count()
    if active_bookings > 0:
        messages.error(request, "Cannot edit a ride that has active bookings.")
        return redirect('my-rides')

    errors = {}
    stops = list(ride.stops.all().order_by('stop_order'))

    if request.method == 'POST':
        origin      = request.POST.get('origin', '').strip()
        destination = request.POST.get('destination', '').strip()
        date_str    = request.POST.get('date', '').strip()
        time_str    = request.POST.get('depart_time', '').strip()
        seats_str   = request.POST.get('seats', '').strip()
        price_str   = request.POST.get('price', '').strip()
        notes       = request.POST.get('notes', '').strip()
        tz_name     = request.POST.get('tz', '').strip() or None
        _prefs_raw = request.POST.get('preferences_json', '').strip()
        try:
            preferences_json = json.loads(_prefs_raw) if _prefs_raw else None
        except (ValueError, TypeError):
            preferences_json = None
        passenger_gender_pref = request.POST.get('passenger_gender_pref', 'any').strip()
        if passenger_gender_pref not in ('any', 'women_only', 'men_only'):
            passenger_gender_pref = 'any'

        try:
            origin_lat = float(request.POST.get('origin_lat') or 0)
            origin_lng = float(request.POST.get('origin_lng') or 0)
            dest_lat   = float(request.POST.get('destination_lat') or 0)
            dest_lng   = float(request.POST.get('destination_lng') or 0)
        except (ValueError, TypeError):
            origin_lat = origin_lng = dest_lat = dest_lng = 0.0

        # 0,0: typed but not picked from the suggestions (same as post_ride_view).
        if not origin:      errors['origin']      = 'Starting point is required.'
        elif origin_lat == 0 and origin_lng == 0:
            errors['origin'] = 'Please pick a location from the suggestions.'
        elif area_error := _service_area_error(origin_lat, origin_lng, 'pickup'):
            errors['origin'] = area_error
        if not destination: errors['destination'] = 'Final destination is required.'
        elif dest_lat == 0 and dest_lng == 0:
            errors['destination'] = 'Please pick a location from the suggestions.'
        elif area_error := _service_area_error(dest_lat, dest_lng, 'drop-off'):
            errors['destination'] = area_error
        clean_stops, stops_error = _clean_stops(request.POST)
        if stops_error:
            errors['stops'] = stops_error
        if not date_str:    errors['date']        = 'Date is required.'
        if not time_str:    errors['depart_time'] = 'Departure time is required.'

        seats = None
        if seats_str:
            try:
                seats = int(seats_str)
                if seats < 1 or seats > 8:
                    errors['seats'] = 'Seats must be between 1 and 8.'
            except ValueError:
                errors['seats'] = 'Invalid seat count.'
        else:
            errors['seats'] = 'Seat count is required.'

        price_cents = 0
        if not price_str:
            errors['price'] = 'Price per seat is required.'
        else:
            try:
                price_cents = _parse_dollars_to_cents(price_str)
                if price_cents <= 0:
                    errors['price'] = 'Price must be greater than 0.'
                elif price_cents > _MAX_PRICE_CENTS:
                    errors['price'] = 'Price per seat cannot exceed $1,000.'
            except ValueError:
                errors['price'] = 'Invalid price.'

        depart_at = None
        depart_utc_str = request.POST.get('depart_utc', '').strip()
        if depart_utc_str and 'date' not in errors and 'depart_time' not in errors:
            try:
                depart_at = datetime.fromisoformat(depart_utc_str.replace('Z', '+00:00'))
                # Same as post_ride_view: a value without an offset is UTC.
                # Comparing it naive with timezone.now() raised a 500.
                if timezone.is_naive(depart_at):
                    from datetime import timezone as _utc
                    depart_at = depart_at.replace(tzinfo=_utc.utc)
                if depart_at < timezone.now() + timedelta(minutes=30):
                    errors['depart_time'] = 'Departure must be at least 30 minutes from now.'
                elif depart_at.date() > _max_ride_date():
                    errors['date'] = 'Rides can only be scheduled up to 4 months in advance.'
            except ValueError:
                errors['depart_time'] = 'Invalid departure date or time.'
        elif date_str and time_str and 'date' not in errors and 'depart_time' not in errors:
            try:
                tz_offset = int(request.POST.get('timezone_offset', 0))
                if abs(tz_offset) > _MAX_TZ_OFFSET_MINUTES:
                    raise ValueError('timezone offset out of range')
                depart_at = timezone.make_aware(datetime.fromisoformat(f'{date_str}T{time_str}')) + timedelta(minutes=tz_offset)
                if depart_at < timezone.now() + timedelta(minutes=30):
                    errors['depart_time'] = 'Departure must be at least 30 minutes from now.'
                elif depart_at.date() > _max_ride_date():
                    errors['date'] = 'Rides can only be scheduled up to 4 months in advance.'
            except (ValueError, OverflowError):  # OverflowError: a date near year 9999
                errors['depart_time'] = 'Invalid departure date or time.'

        if not errors:
            duration_seconds = _parse_duration_seconds(request.POST.get('duration_seconds'))

            if duration_seconds > 0:
                arrive_at = depart_at + timedelta(seconds=duration_seconds)
            elif ride.arrive_at and ride.depart_at:
                arrive_at = depart_at + (ride.arrive_at - ride.depart_at)
            else:
                arrive_at = None

            ride.origin      = origin
            ride.origin_lat  = origin_lat
            ride.origin_lng  = origin_lng
            ride.destination = destination
            ride.dest_lat    = dest_lat
            ride.dest_lng    = dest_lng
            ride.depart_at   = depart_at
            ride.arrive_at   = arrive_at
            if tz_name:
                ride.tz = tz_name
            if duration_seconds > 0:
                ride.expected_duration_seconds = duration_seconds
            booked_seats = RideBooking.objects.filter(
                ride=ride,
                status__in=[RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED],
            ).aggregate(total=Sum('seats'))['total'] or 0
            ride.seats_total     = seats
            ride.seats_available = max(0, seats - booked_seats)
            ride.price_cents = price_cents
            ride.notes       = notes or None
            ride.preferences_json = preferences_json or None
            ride.passenger_gender_pref = passenger_gender_pref
            ride.save()

            ride.stops.all().delete()
            for stop in clean_stops:
                RideStop.objects.create(ride=ride, stop_order=stop['idx'] + 1,
                                        location=stop['location'], lat=stop['lat'], lng=stop['lng'])

            messages.success(request, 'Ride updated successfully.')
            return redirect('my-rides')

    # GET — pre-fill form values
    prefs = []
    if ride.preferences_json:
        prefs = ride.preferences_json if isinstance(ride.preferences_json, list) else []

    stops_data = [{'location': s.location, 'lat': s.lat, 'lng': s.lng} for s in stops]

    # Same tags as post-ride.html, so search filters match edited rides too.
    # Tags saved under an old name (e.g. "Pets OK") stay listed so the driver
    # can still see and remove them.
    pref_options = RIDE_PREFERENCE_OPTIONS + [p for p in prefs if p not in RIDE_PREFERENCE_OPTIONS]
    return render(request, 'edit-ride.html', {
        'ride':         ride,
        'depart_date':  ride.depart_at.strftime('%Y-%m-%d'),
        'depart_time':  ride.depart_at.strftime('%H:%M'),
        'price_display': f"{ride.price_cents / 100:.2f}",
        'preferences':  prefs,
        'pref_options': pref_options,
        'stops_json':   json.dumps(stops_data),
        'stops_data':   stops_data,
        'vehicles':     request.user.vehicles.filter(vehicle_verified=True),
        'errors':       errors,
    })


@login_required
def ride_success_view(request, ride_id):
    real_ride_id = decode_id(ride_id)
    if not real_ride_id:
        raise Http404
    ride = get_object_or_404(
        Rides.objects.prefetch_related('stops'),
        id=real_ride_id,
        driver=request.user,
    )
    price_display = f"${ride.price_cents / 100:.2f}" if ride.price_cents else "Free"
    co2_estimate  = ride.seats_total * 2
    return render(request, 'ride-success.html', {
        'ride':          ride,
        'price_display': price_display,
        'co2_estimate':  co2_estimate,
        # For the Share row. The link is the page riders book from: this
        # page's own URL is the driver's alone, so it 404s for anyone else.
        'share': {
            'url':         request.build_absolute_uri(reverse('create-ride-booking', args=[encode_id(ride.id)])),
            'origin':      ride.origin,
            'destination': ride.destination,
            'depart_utc':  ride.depart_at.isoformat(),
            'price':       price_display,
        },
    })


@login_required
def ride_summary_view(request, ride_id):
    real_ride_id = decode_id(ride_id)
    if not real_ride_id:
        raise Http404
    ride = get_object_or_404(
        Rides.objects.prefetch_related('stops', 'bookings__passenger', 'bookings__pickup_stop'),
        id=real_ride_id,
    )

    is_driver    = ride.driver == request.user
    is_passenger = ride.bookings.filter(
        passenger=request.user,
        status__in=('confirmed', 'completed'),
    ).exists()

    if not is_driver and not is_passenger:
        messages.error(request, "You don't have access to this ride summary.")
        return redirect('my-rides')

    if ride.status != 'completed':
        messages.error(request, "Ride summary is only available for completed rides.")
        return redirect('my-rides')

    origin_city = ride.origin.split(',')[0].strip()
    dest_city   = ride.destination.split(',')[0].strip()

    distance_km = round(_haversine_km(
        ride.origin_lat, ride.origin_lng,
        ride.dest_lat,   ride.dest_lng,
    ))

    duration = ''
    if ride.arrive_at and ride.depart_at:
        delta   = ride.arrive_at - ride.depart_at
        hours   = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    active_bookings = [
        b for b in ride.bookings.all()
        if b.status not in ('cancelled', 'rejected')
    ]
    passenger_count = sum(b.seats for b in active_bookings)

    CO2_PER_KM_KG = 0.12
    co2_saved_kg  = round(passenger_count * distance_km * CO2_PER_KM_KG, 1)
    trees_equiv   = round(co2_saved_kg / 21, 1)
    km_avoided    = passenger_count * distance_km

    # Sorted here so the prefetched stops are reused, not queried again.
    stops = sorted(ride.stops.all(), key=lambda s: s.stop_order)
    stop_pickup_counts = {}
    for b in active_bookings:
        if b.pickup_stop_id:
            stop_pickup_counts[b.pickup_stop_id] = stop_pickup_counts.get(b.pickup_stop_id, 0) + b.seats
    for stop in stops:
        stop.passengers_picked_up = stop_pickup_counts.get(stop.id, 0)

    stops_json = json.dumps([{'lng': s.lng, 'lat': s.lat} for s in stops])

    arrival_note = 'arrived on time'
    if ride.arrive_at and ride.depart_at and ride.expected_duration_seconds:
        actual_seconds   = (ride.arrive_at - ride.depart_at).total_seconds()
        expected_seconds = ride.expected_duration_seconds
        diff = int((expected_seconds - actual_seconds) / 60)
        if diff > 2:
            arrival_note = f'arrived {diff} min early'
        elif diff < -2:
            arrival_note = f'arrived {abs(diff)} min late'

    # One query for the viewer's reviews, not one per booking (audit F-5).
    reviewed_ids = set(
        Review.objects
        .filter(booking__in=[b.id for b in active_bookings], reviewer=request.user)
        .values_list('booking_id', flat=True)
    )

    total_cents = 0
    enriched_bookings = []
    for b in active_bookings:
        name   = _display_name(b.passenger)
        parts  = name.split()
        initials   = ''.join(p[0].upper() for p in parts[:2]) if parts else '?'
        first_name = parts[0] if parts else name

        pickup_name = (
            b.pickup_stop.location.split(',')[0].strip()
            if b.pickup_stop else origin_city
        )

        contribution_cents = b.agreed_price_cents
        total_cents += contribution_cents

        already_reviewed = b.id in reviewed_ids

        enriched_bookings.append({
            'booking_id':           encode_id(b.id),
            'initials':             initials,
            'passenger_name':       name,
            'passenger_first_name': first_name,
            'seats':                b.seats,
            'pickup_name':          pickup_name,
            'contribution':         f"${contribution_cents / 100:.2f}",
            'is_verified':          getattr(b.passenger, 'driver_verified', False),
            'already_reviewed':     already_reviewed,
            'driver_rated':         False,
            'passenger_rating':     0,
            'rating_stars':         '',
        })

    driver_name_full  = _display_name(ride.driver)
    driver_parts      = driver_name_full.split()
    driver_first_name = driver_parts[0] if driver_parts else driver_name_full

    viewer_booking_id = None
    viewer_already_reviewed = False
    if is_passenger:
        viewer_booking = ride.bookings.filter(
            passenger=request.user,
            status__in=('confirmed', 'completed'),
        ).first()
        if viewer_booking:
            viewer_booking_id = encode_id(viewer_booking.id)
            viewer_already_reviewed = Review.objects.filter(
                booking=viewer_booking, reviewer=request.user
            ).exists()

    return render(request, 'ride-summary.html', {
        'ride':                    ride,
        'ride_id':                 f"KMT-{ride.depart_at.year}-{ride.id:04d}",
        'ride_id_encoded':         encode_id(ride.id),
        'is_driver':               is_driver,
        'is_passenger':            is_passenger,
        'driver_name':             driver_name_full,
        'driver_first_name':       driver_first_name,
        'viewer_booking_id':       viewer_booking_id,
        'viewer_already_reviewed': viewer_already_reviewed,
        'origin_city':             origin_city,
        'dest_city':               dest_city,
        'distance_km':             distance_km,
        'duration':                duration,
        'co2_saved_kg':            co2_saved_kg,
        'trees_equiv':             trees_equiv,
        'km_avoided':              km_avoided,
        'passenger_count':         passenger_count,
        'arrival_note':            arrival_note,
        'stops':                   stops,
        'stops_json':              stops_json,
        'bookings':                enriched_bookings,
        'total_contributions':     f"${total_cents / 100:.2f}",
    })


@login_required
def leave_review_view(request, booking_id):
    real_booking_id = decode_id(booking_id)
    if not real_booking_id:
        raise Http404
    booking = get_object_or_404(
        RideBooking.objects.select_related('ride__driver', 'passenger'),
        id=real_booking_id,
    )

    user = request.user
    ride = booking.ride
    is_driver    = ride.driver == user
    is_passenger = booking.passenger == user

    if not is_driver and not is_passenger:
        messages.error(request, "You don't have access to this review.")
        return redirect('my-rides')

    if booking.status != RideBooking.STATUS_COMPLETED and ride.status != 'completed':
        messages.error(request, "You can only review completed rides.")
        return redirect('my-rides')

    if Review.objects.filter(booking=booking, reviewer=user).exists():
        messages.info(request, "You have already reviewed this trip.")
        return redirect('ride-summary', ride_id=encode_id(ride.id))

    if is_driver:
        reviewee      = booking.passenger
        review_type   = 'driver'
        role_label    = 'Your Passenger'
        eyebrow_text  = 'Rate your passenger'
    else:
        reviewee      = ride.driver
        review_type   = 'passenger'
        role_label    = 'Your Driver'
        eyebrow_text  = 'Rate your driver'

    reviewee_name       = _display_name(reviewee)
    parts               = reviewee_name.split()
    reviewee_initials   = ''.join(p[0].upper() for p in parts[:2]) if parts else '?'
    reviewee_first_name = parts[0] if parts else reviewee_name

    return render(request, 'review.html', {
        'booking_id':          encode_id(booking.id),
        'reviewee_id':         reviewee.id,
        'reviewee_name':       reviewee_name,
        'reviewee_first_name': reviewee_first_name,
        'reviewee_initials':   reviewee_initials,
        'reviewee_role_label': role_label,
        'eyebrow_text':        eyebrow_text,
        'review_type':         review_type,
        'origin_city':         ride.origin.split(',')[0].strip(),
        'dest_city':           ride.destination.split(',')[0].strip(),
        'ride_date':           ride.depart_at.strftime('%b %d, %Y'),
        'ride_id':             encode_id(ride.id),
    })


@login_required
def submit_review_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    booking_id  = decode_id(data.get('booking_id', ''))
    reviewee_id = data.get('reviewee_id')
    rating      = data.get('rating')
    body         = (data.get('comment') or '').strip() or None
    is_anonymous = bool(data.get('anonymous', False))
    tags         = data.get('tags') or []
    subratings   = data.get('subratings') or {}

    if not all([booking_id, reviewee_id, rating]):
        return JsonResponse({'error': 'booking_id, reviewee_id, and rating are required.'}, status=400)

    try:
        rating = int(rating)
        if not 1 <= rating <= 5:
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Rating must be between 1 and 5.'}, status=400)

    booking = get_object_or_404(
        RideBooking.objects.select_related('ride__driver', 'passenger'),
        id=booking_id,
    )

    user = request.user
    ride = booking.ride
    is_driver    = ride.driver == user
    is_passenger = booking.passenger == user

    if not is_driver and not is_passenger:
        return JsonResponse({'error': 'Not authorized.'}, status=403)

    # The trip must have happened for this booking: a completed booking, or
    # a confirmed one on a completed ride. A rejected, cancelled or draft
    # booking on a completed ride doesn't count.
    took_place = (
        booking.status == RideBooking.STATUS_COMPLETED
        or (booking.status == RideBooking.STATUS_CONFIRMED and ride.status == 'completed')
    )
    if not took_place:
        return JsonResponse({'error': 'Can only review completed rides.'}, status=400)

    try:
        reviewee_id = int(reviewee_id)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid reviewee_id.'}, status=400)

    expected_reviewee_id = booking.passenger_id if is_driver else ride.driver_id
    if reviewee_id != expected_reviewee_id:
        return JsonResponse({'error': 'Invalid reviewee.'}, status=400)

    if Review.objects.filter(booking=booking, reviewer=user).exists():
        return JsonResponse({'error': 'You have already reviewed this trip.'}, status=400)

    with transaction.atomic():
        Review.objects.create(
            booking=booking,
            reviewer=user,
            reviewee_id=expected_reviewee_id,
            rating=rating,
            body=body,
            tags_json=tags or None,
            subratings_json=subratings or None,
            is_anonymous=is_anonymous,
        )

        # Recompute the reviewee's avg_rating from scratch rather than folding
        # this one rating in -- cheap at this scale, and avoids drift from any
        # review that's ever edited/deleted out from under a running average.
        # select_for_update so two reviews landing on the same reviewee at
        # once can't race and have the second update clobber the first.
        reviewee = User.objects.select_for_update().get(id=expected_reviewee_id)
        avg = Review.objects.filter(reviewee_id=expected_reviewee_id).aggregate(Avg('rating'))['rating__avg']
        reviewee.avg_rating = round(avg, 2) if avg is not None else 0
        reviewee.save(update_fields=['avg_rating'])

    return JsonResponse({'success': True})


@login_required
def driver_inbox_view(request):
    user = request.user
    if user.role != 'driver':
        messages.error(request, "Only drivers can access the inbox.")
        return redirect("profile")

    from .chat_tokens import make_chat_token

    # Draft bookings (pre-request chats — "Chat with driver" without "Request to
    # book") are included here too: once the rider has actually sent a message
    # or price offer, the driver needs to see and respond to it, even though no
    # seat has been reserved yet. Untouched, empty drafts are filtered out below
    # so opening a draft chat alone still doesn't surface anything.
    bookings_qs = (
        RideBooking.objects
        .filter(ride__driver=user)
        .exclude(status__in=['cancelled', 'rejected'])
        .select_related('ride', 'passenger')
        .order_by('-booked_at')
    )

    # Deduplicate: one conversation per (ride, passenger) — keep the latest booking
    seen = {}
    for b in bookings_qs:
        key = (b.ride_id, b.passenger_id)
        if key not in seen:
            seen[key] = b
    unique_bookings = list(seen.values())
    booking_ids = [b.id for b in unique_bookings]
    ride_ids    = [b.ride_id for b in unique_bookings]

    # 1 query: last message per (ride_id, other_user_id)
    _msgs = (
        Message.objects
        .filter(ride_id__in=ride_ids)
        .filter(Q(sender=user) | Q(receiver=user))
        .order_by('-created_at')
        .only('id', 'ride_id', 'sender_id', 'receiver_id', 'body', 'created_at')
    )
    last_msg_map = {}
    for m in _msgs:
        other_id = m.receiver_id if m.sender_id == user.id else m.sender_id
        key = (m.ride_id, other_id)
        if key not in last_msg_map:
            last_msg_map[key] = m

    # 1 query: unread count per (ride_id, passenger_id)
    _unread = (
        Message.objects
        .filter(ride_id__in=ride_ids, receiver=user, read_at__isnull=True)
        .values('ride_id', 'sender_id')
        .annotate(cnt=Count('id'))
    )
    unread_map = {(r['ride_id'], r['sender_id']): r['cnt'] for r in _unread}

    # 1 query: latest offer per booking_id
    _offers = (
        PriceOffer.objects
        .filter(booking_id__in=booking_ids)
        .order_by('-created_at')
        .only('id', 'booking_id', 'status', 'price_cents', 'proposed_by_id', 'created_at')
    )
    latest_offer_map = {}
    for o in _offers:
        if o.booking_id not in latest_offer_map:
            latest_offer_map[o.booking_id] = o

    AVATAR_CLASSES = ['c1', 'c2', 'c3', 'c4', 'c5']
    now = timezone.now()
    my_blocks = set(UserBlock.objects.filter(blocker=user).values_list('blocked_id', flat=True))

    conversations = []
    for i, booking in enumerate(unique_bookings):
        ride = booking.ride
        passenger = booking.passenger

        last_msg     = last_msg_map.get((ride.id, passenger.id))
        unread_count = unread_map.get((ride.id, passenger.id), 0)
        latest_offer = latest_offer_map.get(booking.id)

        if booking.status == RideBooking.STATUS_DRAFT and last_msg is None and latest_offer is None:
            continue

        if booking.status == RideBooking.STATUS_CONFIRMED and booking.agreed_price_cents:
            nego_status = 'agreed'
            nego_label = f"Agreed ${booking.agreed_price_cents / 100:.2f}"
        elif latest_offer and latest_offer.status == PriceOffer.STATUS_PENDING:
            nego_status = 'pending'
            if latest_offer.proposed_by_id == passenger.id:
                nego_label = f"Offer ${latest_offer.price_cents / 100:.2f}"
            else:
                nego_label = f"Counter ${latest_offer.price_cents / 100:.2f}"
        else:
            nego_status = 'none'
            nego_label = ''

        if ride.status == 'completed' or booking.status == RideBooking.STATUS_COMPLETED:
            ui_status = 'completed'
        elif booking.status == RideBooking.STATUS_CONFIRMED:
            ui_status = 'active'
        else:
            ui_status = 'upcoming'

        ref_dt = last_msg.created_at if last_msg else booking.booked_at
        delta = now - ref_dt
        total_secs = delta.total_seconds()
        if total_secs < 60:
            time_display = "Just now"
        elif total_secs < 3600:
            time_display = f"{int(total_secs // 60)} min ago"
        elif total_secs < 86400:
            time_display = f"{int(total_secs // 3600)} hr ago"
        elif delta.days == 1:
            time_display = "Yesterday"
        elif delta.days < 7:
            time_display = ref_dt.strftime('%A')
        else:
            time_display = f"{ref_dt.strftime('%b')} {ref_dt.day}"

        if last_msg:
            is_your_message = (last_msg.sender_id == user.id)
            preview_text = last_msg.body
        elif latest_offer:
            is_your_message = (latest_offer.proposed_by_id == user.id)
            preview_text = f"Price offer: ${latest_offer.price_cents / 100:.2f}/seat"
        else:
            is_your_message = False
            preview_text = "New booking request"

        name = (passenger.full_name or '').title()
        parts = name.split()
        initials = ''.join(p[0].upper() for p in parts[:2]) if parts else '?'

        is_online = (
            passenger.last_login is not None and
            (now - passenger.last_login).total_seconds() < 300
        )

        depart_dt = ride.depart_at
        route = (
            f"{ride.origin} → {ride.destination} · "
            f"{depart_dt.strftime('%a')} {depart_dt.strftime('%I:%M %p').lstrip('0')}"
        )

        conversations.append({
            'booking_id':         booking.id,
            'booking_encoded_id': encode_id(booking.id),
            'booking_status':     booking.status,
            'chat_token':         make_chat_token(booking.id),
            'passenger_name':     name or _UNNAMED_USER,
            'blocked_by_me':      passenger.id in my_blocks,
            'initials':           initials,
            'avatar_class':       AVATAR_CLASSES[i % 5],
            'is_online':          is_online,
            'status':             ui_status,
            'is_unread':          unread_count > 0,
            'nego_status':        nego_status,
            'nego_label':         nego_label,
            'route':              route,
            'time_display':       time_display,
            'preview_text':       preview_text,
            'is_your_message':    is_your_message,
            'unread_badge_count': unread_count if unread_count > 0 else None,
        })

    conversations.sort(key=lambda c: (
        0 if c['booking_status'] == RideBooking.STATUS_REQUESTED else 1,
        0 if c['is_unread'] else 1,
    ))

    total_count              = len(conversations)
    filter_unread_count      = sum(1 for c in conversations if c['is_unread'])
    filter_pending_count     = sum(1 for c in conversations if c['booking_status'] == RideBooking.STATUS_REQUESTED)
    filter_negotiating_count = sum(1 for c in conversations if c['nego_status'] == 'pending')
    filter_active_count      = sum(1 for c in conversations if c['status'] in ('active', 'upcoming'))
    filter_completed_count   = sum(1 for c in conversations if c['status'] == 'completed')

    return render(request, 'driver-inbox.html', {
        'bookings_fingerprint': booking_fingerprint(user),
        'driver_name':              (user.full_name or user.email).title(),
        'nav_unread_count':         filter_unread_count,
        'total_count':              total_count,
        'filter_unread_count':      filter_unread_count,
        'filter_pending_count':     filter_pending_count,
        'filter_negotiating_count': filter_negotiating_count,
        'filter_active_count':      filter_active_count,
        'filter_completed_count':   filter_completed_count,
        'conversations':            conversations,
    })


@login_required
def rider_inbox_view(request):
    user = request.user
    if user.role != 'rider':
        messages.error(request, "Only riders can access this inbox.")
        return redirect("profile")

    from .chat_tokens import make_chat_token

    bookings_qs = (
        RideBooking.objects
        .filter(passenger=user)
        .exclude(status__in=['cancelled', 'rejected', 'draft'])
        .select_related('ride', 'ride__driver')
        .order_by('-booked_at')
    )

    # Deduplicate: one conversation per (ride, driver) — keep the latest booking
    seen = {}
    for b in bookings_qs:
        key = (b.ride_id, b.ride.driver_id)
        if key not in seen:
            seen[key] = b
    unique_bookings = list(seen.values())
    booking_ids = [b.id for b in unique_bookings]
    ride_ids    = [b.ride_id for b in unique_bookings]

    # 1 query: last message per (ride_id, other_user_id)
    _msgs = (
        Message.objects
        .filter(ride_id__in=ride_ids)
        .filter(Q(sender=user) | Q(receiver=user))
        .order_by('-created_at')
        .only('id', 'ride_id', 'sender_id', 'receiver_id', 'body', 'created_at')
    )
    last_msg_map = {}
    for m in _msgs:
        other_id = m.receiver_id if m.sender_id == user.id else m.sender_id
        key = (m.ride_id, other_id)
        if key not in last_msg_map:
            last_msg_map[key] = m

    # 1 query: unread count per (ride_id, driver_id)
    _unread = (
        Message.objects
        .filter(ride_id__in=ride_ids, receiver=user, read_at__isnull=True)
        .values('ride_id', 'sender_id')
        .annotate(cnt=Count('id'))
    )
    unread_map = {(r['ride_id'], r['sender_id']): r['cnt'] for r in _unread}

    # 1 query: latest offer per booking_id
    _offers = (
        PriceOffer.objects
        .filter(booking_id__in=booking_ids)
        .order_by('-created_at')
        .only('id', 'booking_id', 'status', 'price_cents', 'proposed_by_id', 'created_at')
    )
    latest_offer_map = {}
    for o in _offers:
        if o.booking_id not in latest_offer_map:
            latest_offer_map[o.booking_id] = o

    AVATAR_CLASSES = ['c1', 'c2', 'c3', 'c4', 'c5']
    now = timezone.now()
    my_blocks = set(UserBlock.objects.filter(blocker=user).values_list('blocked_id', flat=True))

    conversations = []
    for i, booking in enumerate(unique_bookings):
        ride = booking.ride
        driver = ride.driver

        last_msg     = last_msg_map.get((ride.id, driver.id))
        unread_count = unread_map.get((ride.id, driver.id), 0)
        latest_offer = latest_offer_map.get(booking.id)

        if booking.status == RideBooking.STATUS_CONFIRMED and booking.agreed_price_cents:
            nego_status = 'agreed'
            nego_label = f"Agreed ${booking.agreed_price_cents / 100:.2f}"
        elif latest_offer and latest_offer.status == PriceOffer.STATUS_PENDING:
            nego_status = 'pending'
            if latest_offer.proposed_by_id == driver.id:
                nego_label = f"Offer ${latest_offer.price_cents / 100:.2f}"
            else:
                nego_label = f"Counter ${latest_offer.price_cents / 100:.2f}"
        else:
            nego_status = 'none'
            nego_label = ''

        if ride.status == 'completed' or booking.status == RideBooking.STATUS_COMPLETED:
            ui_status = 'completed'
        elif booking.status == RideBooking.STATUS_CONFIRMED:
            ui_status = 'active'
        else:
            ui_status = 'upcoming'

        ref_dt = last_msg.created_at if last_msg else booking.booked_at
        delta = now - ref_dt
        total_secs = delta.total_seconds()
        if total_secs < 60:
            time_display = "Just now"
        elif total_secs < 3600:
            time_display = f"{int(total_secs // 60)} min ago"
        elif total_secs < 86400:
            time_display = f"{int(total_secs // 3600)} hr ago"
        elif delta.days == 1:
            time_display = "Yesterday"
        elif delta.days < 7:
            time_display = ref_dt.strftime('%A')
        else:
            time_display = f"{ref_dt.strftime('%b')} {ref_dt.day}"

        if last_msg:
            is_your_message = (last_msg.sender_id == user.id)
            preview_text = last_msg.body
        else:
            is_your_message = False
            preview_text = "Booking request sent"

        name = (driver.full_name or '').title()
        parts = name.split()
        initials = ''.join(p[0].upper() for p in parts[:2]) if parts else '?'

        is_online = (
            driver.last_login is not None and
            (now - driver.last_login).total_seconds() < 300
        )

        depart_dt = ride.depart_at
        route = (
            f"{ride.origin} → {ride.destination} · "
            f"{depart_dt.strftime('%a')} {depart_dt.strftime('%I:%M %p').lstrip('0')}"
        )

        conversations.append({
            'booking_id':         booking.id,
            'booking_encoded_id': encode_id(booking.id),
            'booking_status':     booking.status,
            'chat_token':         make_chat_token(booking.id),
            'driver_name':        name or _UNNAMED_USER,
            'driver_verified':    driver.driver_verified,
            'blocked_by_me':      driver.id in my_blocks,
            'initials':           initials,
            'avatar_class':       AVATAR_CLASSES[i % 5],
            'is_online':          is_online,
            'status':             ui_status,
            'is_unread':          unread_count > 0,
            'nego_status':        nego_status,
            'nego_label':         nego_label,
            'route':              route,
            'time_display':       time_display,
            'preview_text':       preview_text,
            'is_your_message':    is_your_message,
            'unread_badge_count': unread_count if unread_count > 0 else None,
        })

    conversations.sort(key=lambda c: not c['is_unread'])

    total_count              = len(conversations)
    filter_unread_count      = sum(1 for c in conversations if c['is_unread'])
    filter_pending_count     = sum(1 for c in conversations if c['booking_status'] == RideBooking.STATUS_REQUESTED)
    filter_negotiating_count = sum(1 for c in conversations if c['nego_status'] == 'pending')
    filter_active_count      = sum(1 for c in conversations if c['status'] in ('active', 'upcoming'))
    filter_completed_count   = sum(1 for c in conversations if c['status'] == 'completed')

    return render(request, 'rider-inbox.html', {
        'bookings_fingerprint': booking_fingerprint(user),
        'rider_name':               (user.full_name or user.email).title(),
        'nav_unread_count':         filter_unread_count,
        'total_count':              total_count,
        'filter_unread_count':      filter_unread_count,
        'filter_pending_count':     filter_pending_count,
        'filter_negotiating_count': filter_negotiating_count,
        'filter_active_count':      filter_active_count,
        'filter_completed_count':   filter_completed_count,
        'conversations':            conversations,
    })


def custom_404_view(request, exception=None):
    return render(request, 'error.html', status=404)

def custom_400_view(request, exception=None):
    return render(request, 'error.html', status=400)

def custom_403_view(request, exception=None):
    return render(request, 'error.html', status=403)

def custom_500_view(request):
    return render(request, 'error.html', status=500)

def upload_too_large_view(request):
    """Nginx's `error_page 413` target: the request body was over
    client_max_body_size, so it never reached Django (ISSUES.md H2)."""
    return render(request, 'upload-too-large.html',
                  {"upload_request_max_mb": settings.UPLOAD_REQUEST_MAX_MB}, status=413)


csp_logger = logging.getLogger("komute.csp")
CSP_REPORT_MAX_BYTES = 8192


def _csp_page(url):
    """The route name of the page a report came from. Never the URL itself:
    reset-password, chat and invite URLs carry tokens."""
    try:
        return resolve(urlsplit(str(url)).path).url_name or '?'
    except (Resolver404, ValueError):
        return '?'


def _csp_source(value):
    """What was blocked, without paths or query strings: an origin
    ('https://evil.example'), a scheme ('data') or a keyword ('inline',
    'eval'). Static files keep their path, since that is where to look."""
    value = str(value or '')[:500]
    parts = urlsplit(value)
    if parts.path.startswith(settings.STATIC_URL) and parts.scheme in ('', 'http', 'https'):
        return parts.path
    if parts.scheme and parts.netloc:
        return f'{parts.scheme}://{parts.netloc}'
    return (parts.scheme or value)[:40]


@csrf_exempt
@require_POST
def csp_report_view(request):
    """Browsers POST Content-Security-Policy violations here (the policy's
    report-uri; audit S-8). Logs one line per report to komute.csp and always
    answers 204: browsers ignore the reply, and an error would only tell a
    prober something. Oversized, malformed and rate-limited reports are
    dropped."""
    if _rate_limited(request, 'csp-report'):
        return HttpResponse(status=204)
    try:
        length = int(request.META.get('CONTENT_LENGTH') or 0)
    except ValueError:
        length = 0
    if not 0 < length <= CSP_REPORT_MAX_BYTES:
        return HttpResponse(status=204)
    try:
        report = json.loads(request.body)['csp-report']
    except (ValueError, KeyError, TypeError):
        return HttpResponse(status=204)
    if not isinstance(report, dict):
        return HttpResponse(status=204)
    csp_logger.warning(
        'CSP violation: directive=%r blocked=%r page=%r source=%r line=%r',
        str(report.get('effective-directive') or report.get('violated-directive') or '')[:60],
        _csp_source(report.get('blocked-uri')),
        _csp_page(report.get('document-uri')),
        _csp_source(report.get('source-file')),
        report.get('line-number') if isinstance(report.get('line-number'), int) else None,
    )
    return HttpResponse(status=204)


@login_required
def change_password_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed.'}, status=405)

    current  = request.POST.get('current_password', '')
    new_pwd  = request.POST.get('new_password', '')
    confirm  = request.POST.get('confirm_password', '')

    # Counted before the check, so an open session can't be used to guess
    # the current password (audit S-15).
    if _rate_limited(request, 'change-password', extra_key=str(request.user.id), per_ip=False):
        return JsonResponse({'error': _TOO_MANY_ATTEMPTS_MSG}, status=429)

    if not request.user.check_password(current):
        return JsonResponse({'error': 'Current password is incorrect.'}, status=400)

    if new_pwd != confirm:
        return JsonResponse({'error': 'New passwords do not match.'}, status=400)

    try:
        validate_password(new_pwd, user=request.user)
    except ValidationError as e:
        return JsonResponse({'error': ' '.join(e.messages)}, status=400)

    request.user.set_password(new_pwd)
    request.user.save()
    update_session_auth_hash(request, request.user)
    return JsonResponse({'ok': True})

@login_required
def create_price_offer_view(request, booking_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed.'}, status=405)

    real_booking_id = decode_id(booking_id)
    if not real_booking_id:
        raise Http404
    booking = get_object_or_404(
        RideBooking.objects.select_related('ride__driver', 'passenger'),
        id=real_booking_id,
    )

    if request.user not in (booking.ride.driver, booking.passenger):
        return JsonResponse({'error': 'Forbidden.'}, status=403)

    if booking.status not in (RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
        return JsonResponse({'error': 'Negotiation is closed for this booking.'}, status=400)

    if UserBlock.objects.between(booking.ride.driver_id, booking.passenger_id):
        return JsonResponse({'error': BLOCKED_CHAT_MESSAGE}, status=403)

    try:
        price_cents = int(round(float(request.POST.get('price_cents', 0))))
    except (ValueError, TypeError, OverflowError):
        return JsonResponse({'error': 'Invalid price.'}, status=400)

    if price_cents <= 0:
        return JsonResponse({'error': 'Price must be greater than 0.'}, status=400)

    if price_cents > 100_000:
        return JsonResponse({'error': 'Offer cannot exceed $1,000.'}, status=400)

    # Expire any pending offers from the same user
    PriceOffer.objects.filter(
        booking=booking,
        proposed_by=request.user,
        status=PriceOffer.STATUS_PENDING,
    ).update(status=PriceOffer.STATUS_DECLINED, responded_at=timezone.now())

    offer = PriceOffer.objects.create(
        booking=booking,
        proposed_by=request.user,
        price_cents=price_cents,
    )

    return JsonResponse({
        'id': offer.id,
        'price_cents': offer.price_cents,
        'status': offer.status,
        'created_at': offer.created_at.isoformat(),
    }, status=201)


@login_required
def respond_price_offer_view(request, offer_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed.'}, status=405)

    real_offer_id = decode_id(offer_id)
    if not real_offer_id:
        raise Http404
    offer = get_object_or_404(
        PriceOffer.objects.select_related('booking__ride__driver', 'booking__passenger'),
        id=real_offer_id,
    )
    booking = offer.booking

    if request.user == offer.proposed_by:
        return JsonResponse({'error': 'You cannot respond to your own offer.'}, status=403)

    if request.user not in (booking.ride.driver, booking.passenger):
        return JsonResponse({'error': 'Forbidden.'}, status=403)

    if booking.status not in (RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
        return JsonResponse({'error': f'This booking is no longer active ({booking.status}).'}, status=400)

    if UserBlock.objects.between(booking.ride.driver_id, booking.passenger_id):
        return JsonResponse({'error': BLOCKED_CHAT_MESSAGE}, status=403)

    if offer.status != PriceOffer.STATUS_PENDING:
        return JsonResponse({'error': 'This offer is no longer pending.'}, status=400)

    action = request.POST.get('action', '').strip()

    if action == 'accept':
        offer.status = PriceOffer.STATUS_ACCEPTED
        offer.responded_at = timezone.now()
        offer.save(update_fields=['status', 'responded_at'])

        booking.agreed_price_cents = offer.price_cents * booking.seats + settings.SERVICE_FEE_CENTS
        booking.save(update_fields=['agreed_price_cents'])

        return JsonResponse({'ok': True, 'status': offer.status, 'price_cents': offer.price_cents})

    elif action == 'decline':
        offer.status = PriceOffer.STATUS_DECLINED
        offer.responded_at = timezone.now()
        offer.save(update_fields=['status', 'responded_at'])

        return JsonResponse({'ok': True, 'status': offer.status})

    elif action == 'counter':
        try:
            counter_cents = int(round(float(request.POST.get('price_cents', 0))))
        except (ValueError, TypeError, OverflowError):
            return JsonResponse({'error': 'Invalid counter price.'}, status=400)

        if counter_cents <= 0:
            return JsonResponse({'error': 'Counter price must be greater than 0.'}, status=400)

        if counter_cents > 100_000:
            return JsonResponse({'error': 'Counter offer cannot exceed $1,000.'}, status=400)

        offer.status = PriceOffer.STATUS_COUNTERED
        offer.responded_at = timezone.now()
        offer.save(update_fields=['status', 'responded_at'])

        new_offer = PriceOffer.objects.create(
            booking=booking,
            proposed_by=request.user,
            price_cents=counter_cents,
        )

        return JsonResponse({
            'ok': True,
            'offer_id': new_offer.id,
            'price_cents': new_offer.price_cents,
            'status': new_offer.status,
        }, status=201)

    return JsonResponse({'error': 'Invalid action. Use accept, decline, or counter.'}, status=400)


@login_required
@require_POST
def send_verification_email(request):
    try:
        user = request.user

        if EmailAddress.objects.filter(user=user, verified=True).exists():
            return JsonResponse({"message": "Already verified"})

        # If an email change is pending, the EmailAddress row already on
        # file for this user is the pending one (change_email_view creates
        # it and deletes any other row for this user) -- target that, not
        # user.email, or get_or_create's create() collides with it on the
        # unique_primary_email constraint (one primary=True row per user)
        # and raises IntegrityError.
        target_email = user.pending_email or user.email

        if (_rate_limited(request, 'send-verification', extra_key=str(user.id), per_ip=False)
                or _rate_limited(request, 'email-confirm-target', extra_key=target_email.lower(), per_ip=False)):
            return JsonResponse({"error": _TOO_MANY_ATTEMPTS_MSG}, status=429)

        email_address, _ = EmailAddress.objects.get_or_create(
            user=user,
            email=target_email,
            defaults={"verified": False, "primary": True}
        )

        confirmation = EmailConfirmationHMAC(email_address)
        activate_url = request.build_absolute_uri(
            reverse('account_confirm_email', args=[confirmation.key])
        )
        ctx = {'user': user, 'activate_url': activate_url}
        subject = render_to_string('account/email/email_confirmation_subject.txt', ctx).strip()
        text_body = render_to_string('account/email/email_confirmation_message.txt', ctx)
        html_body = render_to_string('account/email/email_confirmation_message.html', ctx)

        msg = EmailMultiAlternatives(
            subject, text_body, settings.DEFAULT_FROM_EMAIL, [target_email],
            reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_VERIFICATION],
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send()

        return JsonResponse({"message": "Verification email sent successfully"})
    except Exception:
        logger.exception("send_verification_email failed for user %s", request.user.id)
        return JsonResponse({"error": "Could not send verification email. Please try again later."}, status=400)


@login_required
def change_email_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed.'}, status=405)

    new_email = request.POST.get('new_email', '').strip().lower()
    password  = request.POST.get('password', '')
    user      = request.user

    if not new_email or not password:
        return JsonResponse({'error': 'Email and password are required.'}, status=400)

    # Before the password check, which this form also exposes (audit S-15).
    if _rate_limited(request, 'change-email', extra_key=str(user.id), per_ip=False):
        return JsonResponse({'error': _TOO_MANY_ATTEMPTS_MSG}, status=429)

    try:
        validate_email(new_email)
    except ValidationError:
        return JsonResponse({'error': 'Enter a valid email address.'}, status=400)

    if not user.check_password(password):
        return JsonResponse({'error': 'Incorrect password.'}, status=400)

    if new_email == user.email.lower():
        return JsonResponse({'error': 'This is already your current email.'}, status=400)

    if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
        return JsonResponse({'error': 'This email is already in use.'}, status=400)

    # Caps mail to one address however many accounts send it.
    if _rate_limited(request, 'email-confirm-target', extra_key=new_email, per_ip=False):
        return JsonResponse({'error': _TOO_MANY_ATTEMPTS_MSG}, status=429)

    try:
        # Replace existing EmailAddress records for this user
        EmailAddress.objects.filter(user=user).delete()
        email_address = EmailAddress.objects.create(
            user=user, email=new_email, verified=False, primary=True
        )
        # Store as pending — user.email updates when confirmed via allauth signal
        user.pending_email  = new_email
        user.email_verified = False
        user.save(update_fields=['pending_email', 'email_verified'])

        # Build the confirmation URL using allauth's HMAC key so the
        # confirm link still works through allauth's existing confirm view.
        confirmation = EmailConfirmationHMAC(email_address)
        activate_url = request.build_absolute_uri(
            reverse('account_confirm_email', args=[confirmation.key])
        )
        context = {
            'user': user,
            'new_email': new_email,
            'activate_url': activate_url,
        }
        subject = '[Komüte] Confirm your new email address'
        text_body = render_to_string(
            'account/email/email_change_confirmation_message.txt', context
        )
        html_body = render_to_string(
            'account/email/email_change_confirmation_message.html', context
        )
        msg = EmailMultiAlternatives(
            subject, text_body, settings.DEFAULT_FROM_EMAIL, [new_email],
            reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_EMAIL_CHANGE],
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send()

        return JsonResponse({'message': f'Verification email sent to {new_email}'})
    except Exception:
        logger.exception("change_email_view failed for user %s", user.id)
        return JsonResponse({'error': 'Could not update email. Please try again later.'}, status=400)


class CustomConfirmEmailView(AllauthConfirmEmailView):
    def post(self, *args, **kwargs):
        # allauth confirms the email address and fires the email_confirmed signal,
        # which triggers signals.py to update user.email and user.username.
        super().post(*args, **kwargs)
        # Log out so the session reflects the new credentials.
        logout(self.request)
        messages.success(self.request, 'Email confirmed! Please sign in with your new email address.')
        return redirect('login')


# ──────────────────────────────────────────────────────────────────
# ACCOUNT DELETION (ISSUES.md A2)
# The account closes (is_active=False, deactivated_at set) when the emailed
# link is clicked; purge_deactivated_accounts deletes or anonymizes it
# ACCOUNT_DELETION_GRACE_DAYS later.
# ──────────────────────────────────────────────────────────────────

_DEACTIVATE_SALT = 'komute-deactivate-v1'
_DEACTIVATE_LINK_MAX_AGE_SECONDS = 24 * 3600


def _deactivate_state_hash(user):
    # Bound to the user's password hash, so a token issued before a password
    # change (e.g. because an old email got forwarded, or SECRET_KEY leaked
    # and someone minted a token straight from a user ID) stops verifying
    # the moment the password changes, without needing a DB column.
    return salted_hmac(_DEACTIVATE_SALT + '-state', user.password).hexdigest()


def _make_deactivate_token(user):
    """Return a signed, URL-safe token encoding the user ID and a hash of
    their current password, verified by _decode_deactivate_token."""
    return signing.dumps(
        {'uid': user.id, 'h': _deactivate_state_hash(user)},
        salt=_DEACTIVATE_SALT,
    )


def _decode_deactivate_token(token):
    """Decode and verify a token produced by _make_deactivate_token.
    Raises signing.SignatureExpired if older than 24h, signing.BadSignature if
    tampered, expired, or if the user's password has changed since issuance."""
    data = signing.loads(
        token,
        salt=_DEACTIVATE_SALT,
        max_age=_DEACTIVATE_LINK_MAX_AGE_SECONDS,
    )
    user = User.objects.filter(id=data['uid']).first()
    if user is None or not constant_time_compare(data['h'], _deactivate_state_hash(user)):
        raise signing.BadSignature('stale deactivate token')
    return user.id


def _account_deletion_message():
    days = settings.ACCOUNT_DELETION_GRACE_DAYS
    return (
        f'Your account is closed now and permanently deleted after {days} days. '
        f'Contact support within that time if you change your mind. Some records '
        f'(e.g. completed ride history) are kept for the periods in our privacy policy.'
    )


def _send_delete_account_email(request, user):
    """Email `user` the signed link that closes their account. Raises if the
    email can't be sent."""
    token = _make_deactivate_token(user)
    confirm_url = request.build_absolute_uri(
        reverse('confirm-delete-account', args=[token])
    )
    ctx = {
        'user': user,
        'confirm_url': confirm_url,
        'grace_days': settings.ACCOUNT_DELETION_GRACE_DAYS,
        'delete_account_url': request.build_absolute_uri(reverse('delete-account')),
    }
    text_body = render_to_string('account/email/delete_account_confirmation.txt', ctx)
    html_body = render_to_string('account/email/delete_account_confirmation.html', ctx)
    msg = EmailMultiAlternatives(
        subject='[Komüte] Confirm account deletion',
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
        cc=[settings.ADMIN_EMAIL],
        reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_DEACTIVATION],
    )
    msg.attach_alternative(html_body, 'text/html')
    msg.send()


@login_required
@require_POST
def request_delete_account_view(request):
    user = request.user
    # Same limit as the public delete-account page, per account (audit S-15).
    if _rate_limited(request, 'delete-account-email', extra_key=user.email.lower(), per_ip=False):
        return JsonResponse({'error': _TOO_MANY_ATTEMPTS_MSG}, status=429)
    try:
        _send_delete_account_email(request, user)
    except Exception:
        logger.exception("request_delete_account_view failed for user %s", user.id)
        return JsonResponse({'error': 'Could not send confirmation email. Please try again later.'}, status=400)

    return JsonResponse({'message': f'A confirmation link has been sent to {user.email}. Click it to delete your account.'})


def _iso(value):
    return value.isoformat() if value else None


def _ride_summary(ride):
    return {'ride_id': encode_id(ride.id), 'from': ride.origin, 'to': ride.destination,
            'departs_at': _iso(ride.depart_at), 'status': ride.status}


def _build_data_export(user):
    """Everything Komüte holds about `user`, for "Download my data" (audit P-9).
    Other people appear only by the display name the app already shows. Left
    out: the password hash, staff notes, and reports made about the user (the
    policy promises reporters stay anonymous)."""
    other = _display_name

    rides = (Rides.objects.filter(driver=user).select_related('vehicle')
             .prefetch_related('stops', 'bookings__passenger').order_by('depart_at'))
    bookings = (RideBooking.objects.filter(passenger=user)
                .select_related('ride__driver').order_by('booked_at'))
    messages_qs = (Message.objects.filter(Q(sender=user) | Q(receiver=user))
                   .select_related('sender', 'receiver').order_by('created_at'))

    return {
        'exported_at': timezone.now().isoformat(),
        'account': {
            'full_name': user.full_name, 'email': user.email, 'email_verified': user.email_verified,
            'pending_email': user.pending_email, 'phone': user.phone, 'role': user.role,
            'date_of_birth': _iso(user.dob), 'gender': user.gender, 'bio': user.bio,
            'has_profile_photo': bool(user.avatar_url),
            'driver_verified': user.driver_verified, 'driver_verified_at': _iso(user.driver_verified_at),
            'licence_number': user.license_number, 'licence_province': user.license_province,
            'licence_expiry': _iso(user.license_expiry), 'face_match_score': user.face_match_score,
            'average_rating': user.avg_rating, 'total_rides': user.total_rides,
            'created_at': _iso(user.created_at), 'updated_at': _iso(user.updated_at),
        },
        'vehicles': [
            {'make': v.make, 'model': v.model, 'year': v.year, 'color': v.color, 'plate': v.plate,
             'seat_capacity': v.seat_capacity, 'has_photo': bool(v.photo_url),
             'verified': v.vehicle_verified, 'added_at': _iso(v.created_at)}
            for v in user.vehicles.order_by('created_at')
        ],
        'rides_as_driver': [
            {**_ride_summary(r), 'arrives_at': _iso(r.arrive_at),
             'origin_coordinates': [r.origin_lat, r.origin_lng],
             'destination_coordinates': [r.dest_lat, r.dest_lng],
             'stops': [{'location': s.location, 'coordinates': [s.lat, s.lng]} for s in r.stops.all()],
             'seats_total': r.seats_total, 'price_per_seat_cents': r.price_cents, 'currency': r.currency,
             'passenger_preference': r.passenger_gender_pref, 'preferences': r.preferences_json,
             'notes': r.notes, 'vehicle': f'{r.vehicle.make} {r.vehicle.model}' if r.vehicle else None,
             'created_at': _iso(r.created_at), 'cancelled_at': _iso(r.cancelled_at),
             'bookings': [{'passenger': other(b.passenger), 'seats': b.seats, 'status': b.status,
                           'price_cents': b.agreed_price_cents, 'booked_at': _iso(b.booked_at)}
                          for b in r.bookings.all()]}
            for r in rides
        ],
        'bookings_as_passenger': [
            {**_ride_summary(b.ride), 'driver': other(b.ride.driver), 'seats': b.seats,
             'status': b.status, 'price_cents': b.agreed_price_cents, 'booked_at': _iso(b.booked_at),
             'confirmed_at': _iso(b.confirmed_at), 'cancelled_at': _iso(b.cancelled_at),
             'rejected_at': _iso(b.rejected_at), 'cancellation_reason': b.cancellation_reason}
            for b in bookings
        ],
        'price_offers': [
            {'ride_id': encode_id(o.booking.ride_id), 'price_cents': o.price_cents, 'status': o.status,
             'made_by_you': o.proposed_by_id == user.id, 'created_at': _iso(o.created_at),
             'responded_at': _iso(o.responded_at)}
            for o in PriceOffer.objects.filter(Q(booking__passenger=user) | Q(booking__ride__driver=user))
                                       .select_related('booking').order_by('created_at')
        ],
        'messages': [
            {'ride_id': encode_id(m.ride_id), 'direction': 'sent' if m.sender_id == user.id else 'received',
             'with': other(m.receiver if m.sender_id == user.id else m.sender),
             'text': m.body, 'sent_at': _iso(m.created_at), 'read_at': _iso(m.read_at)}
            for m in messages_qs
        ],
        'reviews_written': [
            {'about': other(r.reviewee), 'rating': r.rating, 'text': r.body, 'tags': r.tags_json,
             'subratings': r.subratings_json, 'anonymous': r.is_anonymous, 'created_at': _iso(r.created_at)}
            for r in Review.objects.filter(reviewer=user).select_related('reviewee').order_by('created_at')
        ],
        'reviews_received': [
            {'from': 'Anonymous' if r.is_anonymous or not r.reviewer else other(r.reviewer),
             'rating': r.rating, 'text': r.body, 'tags': r.tags_json, 'subratings': r.subratings_json,
             'created_at': _iso(r.created_at)}
            for r in Review.objects.filter(reviewee=user).select_related('reviewer').order_by('created_at')
        ],
        'reports_you_made': [
            {'about': other(r.reported), 'reason': r.reason, 'details': r.details, 'status': r.status,
             'created_at': _iso(r.created_at)}
            for r in UserReport.objects.filter(reporter=user).select_related('reported').order_by('created_at')
        ],
        'users_you_blocked': [
            {'name': other(b.blocked), 'since': _iso(b.created_at)}
            for b in UserBlock.objects.filter(blocker=user).select_related('blocked').order_by('created_at')
        ],
        'ride_searches': [
            {'from': s.origin, 'to': s.destination, 'origin_coordinates': [s.origin_lat, s.origin_lng],
             'destination_coordinates': [s.dest_lat, s.dest_lng], 'date': _iso(s.search_date),
             'passengers': s.passenger_count, 'searched_at': _iso(s.created_at)}
            for s in RideSearch.objects.filter(user=user).order_by('created_at')
        ],
        'ride_invites': [
            {**_ride_summary(i.ride), 'direction': 'sent' if i.driver_id == user.id else 'received',
             'with': other(i.passenger if i.driver_id == user.id else i.driver), 'status': i.status,
             'sent_at': _iso(i.sent_at), 'responded_at': _iso(i.responded_at)}
            for i in RideInvite.objects.filter(Q(driver=user) | Q(passenger=user))
                                       .select_related('ride', 'driver', 'passenger').order_by('sent_at')
        ],
        'terms_acceptances': [
            {'terms_version': a.terms_version, 'privacy_version': a.privacy_version, 'source': a.source,
             'ip_address': a.ip_address, 'browser': a.user_agent, 'accepted_at': _iso(a.accepted_at)}
            for a in LegalAcceptance.objects.filter(user=user).order_by('accepted_at')
        ],
        'biometric_consents': [
            {'notice_version': c.notice_version, 'ip_address': c.ip_address, 'browser': c.user_agent,
             'accepted_at': _iso(c.accepted_at)}
            for c in BiometricConsent.objects.filter(user=user).order_by('accepted_at')
        ],
        'driver_verifications': [
            {'result': v.result, 'checks': v.stage_results, 'reasons': v.reason_codes,
             'face_score': v.face_score, 'created_at': _iso(v.created_at),
             'review': v.review_status, 'review_requested_at': _iso(v.review_requested_at),
             'your_review_comment': v.driver_comment, 'reviewed_at': _iso(v.reviewed_at)}
            for v in VerificationLog.objects.filter(user=user).order_by('created_at')
        ],
        'emails_sent_to_you': [
            {'kind': e.kind, 'subject': e.subject, 'status': e.status, 'queued_at': _iso(e.created_at),
             'sent_at': _iso(e.sent_at)}
            for e in EmailLog.objects.filter(user=user).order_by('created_at')
        ],
    }


@login_required
@never_cache
def download_my_data_view(request):
    """Profile → "Download my data": the account's data as a JSON file (audit P-9)."""
    user = request.user
    if _rate_limited(request, 'data-export', extra_key=str(user.id), per_ip=False):
        messages.error(request, _TOO_MANY_ATTEMPTS_MSG)
        return redirect('profile')
    body = json.dumps(_build_data_export(user), indent=2, ensure_ascii=False, default=str)
    resp = HttpResponse(body, content_type='application/json; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="komute-my-data-{timezone.now():%Y-%m-%d}.json"'
    return resp


_DELETE_ACCOUNT_SENT_MESSAGE = (
    "If an account exists for that email, we've sent it a link to confirm the deletion. "
    "Nothing happens until the link is clicked."
)


def delete_account_request_view(request):
    """Public page for asking to delete an account without signing in or
    using the app (Google Play's account deletion URL, ISSUES.md A2). Sends
    the same confirmation link as the profile page."""
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()

        # Same shape as forgot_password_view: throttled per IP and per
        # email before the lookup, and one reply whether or not the account
        # exists, so the form can't be used to find accounts.
        blocked = _rate_limited(request, 'delete-account-ip')
        if email:
            blocked = _rate_limited(request, 'delete-account-email', extra_key=email) or blocked

        user = None
        if email and not blocked:
            user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            try:
                _send_delete_account_email(request, user)
            except Exception:
                logger.exception("delete_account_request_view failed for user %s", user.id)

        messages.success(request, _DELETE_ACCOUNT_SENT_MESSAGE)
        return redirect('delete-account')

    initial_email = request.user.email if request.user.is_authenticated else ''
    return render(request, 'delete-account.html', {
        'initial_email': initial_email,
        'grace_days': settings.ACCOUNT_DELETION_GRACE_DAYS,
    })


def confirm_delete_account_view(request, token):
    try:
        user_id = _decode_deactivate_token(token)
    except signing.SignatureExpired:
        messages.error(request, 'This deletion link has expired. Please request a new one.')
        return redirect('delete-account')
    except signing.BadSignature:
        messages.error(request, 'Invalid account deletion link.')
        return redirect('delete-account')

    user = get_object_or_404(User, id=user_id)

    if not user.is_active:
        messages.info(request, 'This account is already closed and scheduled for deletion.')
        return redirect('login')

    user.is_active = False
    user.deactivated_at = timezone.now()
    user.save(update_fields=['is_active', 'deactivated_at'])
    # The privacy policy promises search logs go at once, not with the rest
    # of the account at the 45-day purge (audit P-14). It also takes the
    # user off drivers' Route Demand lists, which are built from searches.
    RideSearch.objects.filter(user=user).delete()

    if request.user.is_authenticated and request.user.id == user.id:
        logout(request)

    messages.success(request, _account_deletion_message())
    return redirect('login')


# ──────────────────────────────────────────────────────────────────
# PASSWORD RESET
# ──────────────────────────────────────────────────────────────────

_PASSWORD_RESET_SALT = 'komute-password-reset-v1'
_PASSWORD_RESET_LINK_MAX_AGE_SECONDS = settings.PASSWORD_RESET_LINK_MINUTES * 60


def _password_reset_state_hash(user):
    # Bound to the current password hash, so a reset link — issued by email,
    # forgeable by anyone holding SECRET_KEY (Finding 1), or simply left
    # sitting in an inbox — stops working the instant the password actually
    # changes, instead of staying valid for its full max-age regardless.
    return salted_hmac(_PASSWORD_RESET_SALT + '-state', user.password).hexdigest()


def _make_password_reset_token(user):
    """Return a signed, URL-safe token encoding the user ID and a hash of
    their current password, verified by _decode_password_reset_token."""
    return signing.dumps(
        {'uid': user.id, 'h': _password_reset_state_hash(user)},
        salt=_PASSWORD_RESET_SALT,
    )


def _decode_password_reset_token(token):
    """Decode and verify a token produced by _make_password_reset_token.
    Raises signing.SignatureExpired if older than PASSWORD_RESET_LINK_MINUTES,
    signing.BadSignature if
    tampered, expired, or if the user's password has changed since issuance."""
    data = signing.loads(
        token,
        salt=_PASSWORD_RESET_SALT,
        max_age=_PASSWORD_RESET_LINK_MAX_AGE_SECONDS,
    )
    user = User.objects.filter(id=data['uid']).first()
    if user is None or not constant_time_compare(data['h'], _password_reset_state_hash(user)):
        raise signing.BadSignature('stale password reset token')
    return user.id


def forgot_password_view(request):
    if request.user.is_authenticated:
        return redirect('profile')

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()

        # Per-IP (scripted enumeration) and per-email (one address getting
        # bombed with reset emails from many IPs) — counted before the DB
        # lookup so it can't be used to distinguish real vs. fake emails.
        blocked = _rate_limited(request, 'forgot-password-ip')
        if email:
            blocked = _rate_limited(request, 'forgot-password-email', extra_key=email) or blocked
        if blocked:
            messages.success(
                request,
                "If an account exists for that email, we've sent a link to reset your password."
            )
            return redirect('forgot-password')

        user = User.objects.filter(email__iexact=email).first() if email else None

        if user:
            token = _make_password_reset_token(user)
            reset_url = request.build_absolute_uri(
                reverse('reset-password', args=[token])
            )
            ctx = {
                'user': user,
                'reset_url': reset_url,
                'expiry_minutes': settings.PASSWORD_RESET_LINK_MINUTES,
            }
            try:
                text_body = render_to_string('account/email/password_reset_email.txt', ctx)
                html_body = render_to_string('account/email/password_reset_email.html', ctx)
                msg = EmailMultiAlternatives(
                    subject='[Komüte] Reset your password',
                    body=text_body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[user.email],
                    reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_PASSWORD],
                )
                msg.attach_alternative(html_body, 'text/html')
                msg.send()
            except Exception:
                pass

        # Always show the same message, whether or not the email exists,
        # so the form can't be used to enumerate registered accounts.
        messages.success(
            request,
            "If an account exists for that email, we've sent a link to reset your password."
        )
        return redirect('forgot-password')

    return render(request, 'forgot-password.html')


def reset_password_view(request, token):
    if request.user.is_authenticated:
        return redirect('profile')

    try:
        user_id = _decode_password_reset_token(token)
    except signing.SignatureExpired:
        messages.error(request, 'This password reset link has expired. Please request a new one.')
        return redirect('forgot-password')
    except signing.BadSignature:
        messages.error(request, 'Invalid password reset link.')
        return redirect('forgot-password')

    user = get_object_or_404(User, id=user_id)

    if not user.is_active:
        # Login (ModelBackend) refuses any is_active=False account regardless
        # of password, so letting the form "succeed" here would silently set
        # a password the user could never log in with. A closed account is
        # reopened only by support (see confirm_delete_account_view) -- a
        # password reset alone shouldn't undo it.
        messages.error(
            request,
            'This account is closed and scheduled for deletion. Contact support if you want it reopened.'
        )
        return redirect('forgot-password')

    if request.method == 'POST':
        new_pwd = request.POST.get('new_password', '')
        confirm = request.POST.get('confirm_password', '')

        if new_pwd != confirm:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'reset-password.html', {'token': token})

        try:
            validate_password(new_pwd, user=user)
        except ValidationError as e:
            messages.error(request, ' '.join(e.messages))
            return render(request, 'reset-password.html', {'token': token})

        user.set_password(new_pwd)
        user.save(update_fields=['password'])

        messages.success(request, 'Your password has been reset. Please sign in with your new password.')
        return redirect('login')

    return render(request, 'reset-password.html', {'token': token})



@login_required
def ride_receipt_pdf_view(request, ride_id):
    real_ride_id = decode_id(ride_id)
    if not real_ride_id:
        raise Http404
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    # Paragraph parses its text as markup (and fetches <img> sources), so
    # user-typed names and places must be escaped before they go in.
    from xml.sax.saxutils import escape as _pdf_escape

    ride = get_object_or_404(
        Rides.objects.prefetch_related('stops', 'bookings__passenger', 'bookings__pickup_stop').filter(
            Q(driver=request.user) |
            Q(bookings__passenger=request.user, bookings__status__in=('confirmed', 'completed'))
        ).distinct(),
        id=real_ride_id,
    )

    # ── build context (same logic as ride_summary_view) ──────────
    origin_city = ride.origin.split(',')[0].strip()
    distance_km = round(_haversine_km(ride.origin_lat, ride.origin_lng, ride.dest_lat, ride.dest_lng))

    duration = ''
    if ride.arrive_at and ride.depart_at:
        delta   = ride.arrive_at - ride.depart_at
        hours   = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    active_bookings = [b for b in ride.bookings.all() if b.status not in ('cancelled', 'rejected')]
    passenger_count = sum(b.seats for b in active_bookings)

    CO2_PER_KM_KG = 0.12
    co2_saved_kg  = round(passenger_count * distance_km * CO2_PER_KM_KG, 1)

    total_cents = 0
    enriched_bookings = []
    for b in active_bookings:
        name  = _display_name(b.passenger)
        pickup_name = b.pickup_stop.location.split(',')[0].strip() if b.pickup_stop else origin_city
        total_cents += b.agreed_price_cents
        enriched_bookings.append({
            'name':         name,
            'seats':        b.seats,
            'pickup':       pickup_name,
            'contribution': f"${b.agreed_price_cents / 100:.2f}",
        })

    ride_ref = f"KMT-{ride.depart_at.year}-{ride.id:04d}"
    driver_name = _display_name(ride.driver)

    # Convert to ride's local timezone for display in the PDF
    _depart_local = _local_dt(ride, ride.depart_at)
    _arrive_local = _local_dt(ride, ride.arrive_at) if ride.arrive_at else None
    _tz_suffix    = (' ' + _depart_local.tzname()) if ride.tz and _depart_local.tzname() else ' UTC'

    # ── PDF generation ────────────────────────────────────────────
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title='Komute Ride Receipt',
        author='Komute',
    )

    GREEN  = colors.HexColor('#0D7810')
    DARK   = colors.HexColor('#121212')
    MUTED  = colors.HexColor('#5a7060')
    LIGHT  = colors.HexColor('#E9F7EE')
    WHITE  = colors.white

    styles = getSampleStyleSheet()
    def _s(name, **kw):
        base = styles[name] if name in styles else styles['Normal']
        return ParagraphStyle(name + '_custom', parent=base, **kw)

    title_style   = _s('Normal', fontName='Helvetica-Bold', fontSize=22, textColor=GREEN, spaceAfter=2)
    sub_style     = _s('Normal', fontName='Helvetica',      fontSize=10, textColor=MUTED, spaceAfter=6)
    head_style    = _s('Normal', fontName='Helvetica-Bold', fontSize=11, textColor=DARK,  spaceAfter=4)
    body_style    = _s('Normal', fontName='Helvetica',      fontSize=10, textColor=DARK)
    muted_style   = _s('Normal', fontName='Helvetica',      fontSize=9,  textColor=MUTED)
    right_style   = _s('Normal', fontName='Helvetica',      fontSize=10, textColor=DARK, alignment=TA_RIGHT)
    total_style   = _s('Normal', fontName='Helvetica-Bold', fontSize=11, textColor=GREEN, alignment=TA_RIGHT)
    center_style  = _s('Normal', fontName='Helvetica',      fontSize=9,  textColor=MUTED, alignment=TA_CENTER)
    green_bold    = _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=GREEN)

    W = A4[0] - 40*mm  # usable width

    story = []

    # ── Header ──
    header_data = [[
        Paragraph('KOMÜTE', title_style),
        Paragraph(f'Ride Receipt<br/><font size="9" color="#5a7060">{ride_ref}</font>', _s('Normal', fontName='Helvetica-Bold', fontSize=14, textColor=DARK, alignment=TA_RIGHT)),
    ]]
    header_tbl = Table(header_data, colWidths=[W * 0.5, W * 0.5])
    header_tbl.setStyle(TableStyle([
        ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
        ('BACKGROUND',  (0,0), (-1,-1), LIGHT),
        ('TOPPADDING',  (0,0), (-1,-1), 10),
        ('BOTTOMPADDING',(0,0),(-1,-1), 10),
        ('LEFTPADDING', (0,0), (-1,-1), 14),
        ('RIGHTPADDING',(0,0), (-1,-1), 14),
        ('ROUNDEDCORNERS', (0,0), (-1,-1), [8, 8, 8, 8]),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 5*mm))
    story.append(Paragraph(f'Issued on {timezone.now().strftime("%B %d, %Y")}  ·  Driver: {_pdf_escape(driver_name)}', sub_style))
    story.append(HRFlowable(width=W, color=colors.HexColor('#c2dfc8'), thickness=1, spaceAfter=5*mm))

    # ── Trip Overview ──
    story.append(Paragraph('Trip Overview', head_style))
    trip_data = [
        ['Route',      f'{ride.origin}  →  {ride.destination}'],
        ['Date',       _depart_local.strftime('%A, %B %d, %Y').replace(' 0', ' ')],
        ['Departed',   _depart_local.strftime('%I:%M %p').lstrip('0') + _tz_suffix],
        ['Arrived',    _arrive_local.strftime('%I:%M %p').lstrip('0') + _tz_suffix if _arrive_local else '—'],
        ['Duration',   duration or '—'],
        ['Distance',   f'{distance_km} km'],
        ['Driver',     driver_name],
        ['Status',     ride.get_status_display()],
        ['Passengers', f'{passenger_count} passenger{"s" if passenger_count != 1 else ""}'],
    ]
    trip_tbl = Table(trip_data, colWidths=[W * 0.28, W * 0.72])
    trip_tbl.setStyle(TableStyle([
        ('FONTNAME',    (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME',    (1,0), (1,-1), 'Helvetica'),
        ('FONTSIZE',    (0,0), (-1,-1), 10),
        ('TEXTCOLOR',   (0,0), (0,-1), MUTED),
        ('TEXTCOLOR',   (1,0), (1,-1), DARK),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [WHITE, LIGHT]),
        ('TOPPADDING',  (0,0), (-1,-1), 6),
        ('BOTTOMPADDING',(0,0),(-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING',(0,0),(-1,-1), 10),
        ('GRID',        (0,0), (-1,-1), 0.5, colors.HexColor('#e0ede3')),
        ('ROUNDEDCORNERS', (0,0), (-1,-1), [6, 6, 6, 6]),
    ]))
    story.append(trip_tbl)
    story.append(Spacer(1, 5*mm))

    # ── Passenger & Cost Breakdown ──
    story.append(Paragraph('Cost-Share Breakdown', head_style))
    cost_header = [
        Paragraph('Passenger', _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE)),
        Paragraph('Seats',     _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE, alignment=TA_CENTER)),
        Paragraph('Pickup',    _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE)),
        Paragraph('Amount',    _s('Normal', fontName='Helvetica-Bold', fontSize=10, textColor=WHITE, alignment=TA_RIGHT)),
    ]
    cost_rows = [cost_header]
    for b in enriched_bookings:
        cost_rows.append([
            Paragraph(_pdf_escape(b['name']), body_style),
            Paragraph(str(b['seats']),   _s('Normal', fontName='Helvetica', fontSize=10, textColor=DARK, alignment=TA_CENTER)),
            Paragraph(_pdf_escape(b['pickup']), muted_style),
            Paragraph(b['contribution'], right_style),
        ])
    cost_rows.append([
        Paragraph('Total received', green_bold),
        '', '',
        Paragraph(f"${total_cents / 100:.2f}", total_style),
    ])

    cost_tbl = Table(cost_rows, colWidths=[W*0.38, W*0.12, W*0.32, W*0.18])
    cost_style_cmds = [
        ('BACKGROUND',   (0,0), (-1,0),  GREEN),
        ('ROWBACKGROUNDS',(0,1), (-1,-2), [WHITE, LIGHT]),
        ('BACKGROUND',   (0,-1),(-1,-1), colors.HexColor('#E9F7EE')),
        ('TOPPADDING',   (0,0), (-1,-1), 7),
        ('BOTTOMPADDING',(0,0), (-1,-1), 7),
        ('LEFTPADDING',  (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ('GRID',         (0,0), (-1,-1), 0.5, colors.HexColor('#e0ede3')),
        ('SPAN',         (1,-1),(2,-1)),
        ('LINEABOVE',    (0,-1),(-1,-1), 1.5, GREEN),
    ]
    cost_tbl.setStyle(TableStyle(cost_style_cmds))
    story.append(cost_tbl)
    story.append(Spacer(1, 5*mm))

    # ── Eco Impact ──
    story.append(Paragraph('Eco Impact', head_style))
    eco_data = [[
        Paragraph(f'<b>{co2_saved_kg} kg</b><br/><font size="8" color="#5a7060">CO₂ Saved</font>', _s('Normal', fontName='Helvetica', fontSize=11, textColor=GREEN, alignment=TA_CENTER)),
        Paragraph(f'<b>{passenger_count}</b><br/><font size="8" color="#5a7060">Passengers</font>', _s('Normal', fontName='Helvetica', fontSize=11, textColor=DARK, alignment=TA_CENTER)),
        Paragraph(f'<b>{distance_km} km</b><br/><font size="8" color="#5a7060">Distance</font>', _s('Normal', fontName='Helvetica', fontSize=11, textColor=DARK, alignment=TA_CENTER)),
    ]]
    eco_tbl = Table(eco_data, colWidths=[W/3, W/3, W/3])
    eco_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), LIGHT),
        ('TOPPADDING',    (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('INNERGRID',     (0,0), (-1,-1), 0.5, colors.HexColor('#c2dfc8')),
        ('BOX',           (0,0), (-1,-1), 0.5, colors.HexColor('#c2dfc8')),
    ]))
    story.append(eco_tbl)
    story.append(Spacer(1, 6*mm))

    # ── Footer note ──
    story.append(HRFlowable(width=W, color=colors.HexColor('#c2dfc8'), thickness=1, spaceAfter=4*mm))
    story.append(Paragraph(
        'Komüte is free to use. Amounts above were paid directly between driver and passengers. '
        'Komüte did not process or hold any funds — it simply connected the parties.',
        _s('Normal', fontName='Helvetica-Oblique', fontSize=8, textColor=MUTED),
    ))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(f'© {timezone.now().year} Komüte by Indus Network LLC  ·  {ride_ref}', center_style))

    doc.build(story)
    buffer.seek(0)
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="Komute-Receipt-{ride_ref}.pdf"'
    return response


@login_required
def private_media_view(request, path):
    """GET /private-media/<path> - serves a license/selfie/vehicle image to
    its owner or staff only (ISSUES.md C1). Anyone else gets a 404, so the
    response doesn't reveal whether the file exists."""
    rel_path = private_media.clean_rel_path(path)
    if rel_path is None or not private_media.user_can_access(request.user, rel_path):
        raise Http404

    storage = private_media.private_storage()
    if not storage.exists(rel_path):
        raise Http404

    content_type = private_media.content_type_for(rel_path)
    if settings.PRIVATE_MEDIA_USE_ACCEL:
        # Nginx streams the file from its `internal` location.
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"{settings.PRIVATE_MEDIA_ACCEL_PREFIX}{rel_path}"
    else:
        response = FileResponse(storage.open(rel_path, "rb"), content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _norm_name(value):
    """A name as DL-Verification compares it: case, hyphens and extra
    spaces ignored."""
    return " ".join((value or "").replace("-", " ").split()).casefold()


def _norm_code(value):
    """A license or plate number with case, spaces and hyphens ignored."""
    return re.sub(r"[\s\-]", "", value or "").upper()


# Keyword -> category for DL-Verification's free-text rejection_reasons. The
# service can't send codes, and its text may quote licence values, which the
# privacy policy (s.9/s.13) doesn't let us keep -- so only the category is
# stored. First match wins: image quality before the field checks, and vehicle
# before licence number so "licence plate number" isn't read as the DL number.
_REASON_PATTERNS = [
    (VerificationLog.REASON_IMAGE_QUALITY, re.compile(r"blur|unreadable|illegible|quality|glare|too dark|retry", re.I)),
    (VerificationLog.REASON_FACE_MISMATCH, re.compile(r"face|facial|biometric|selfie", re.I)),
    (VerificationLog.REASON_VEHICLE_MISMATCH, re.compile(r"vehicle|plate|colou?r|alpr", re.I)),
    (VerificationLog.REASON_EXPIRY_MISMATCH, re.compile(r"expir", re.I)),
    (VerificationLog.REASON_DOB_MISMATCH, re.compile(r"birth|\bdob\b", re.I)),
    (VerificationLog.REASON_LICENSE_NUMBER_MISMATCH, re.compile(r"licen[cs]e\s*(number|no\b|#)|\bdl\s*(number|no\b)", re.I)),
    (VerificationLog.REASON_NAME_MISMATCH, re.compile(r"\bname\b", re.I)),
]


# Shown when a check failed but the service gave no reason for it.
_STAGE_FAILED_REASONS = {
    "license_ocr": "The licence check didn't pass: the details read from your licence photo didn't match your profile.",
    "face_biometrics": "The face check didn't pass: your selfie didn't match the photo on your licence.",
    "vehicle_verification": "The vehicle check didn't pass: your vehicle photo didn't match your vehicle details.",
}


def _reason_codes(reasons):
    """Categories for the service's rejection reasons, deduplicated in order."""
    codes = []
    for reason in reasons:
        code = next((c for c, pat in _REASON_PATTERNS if pat.search(reason)), VerificationLog.REASON_OTHER)
        if code not in codes:
            codes.append(code)
    return codes


def _record_biometric_consent(request, user):
    """Stores proof that `user` accepted the current biometric notice."""
    BiometricConsent.objects.create(
        user=user,
        notice_version=settings.BIOMETRIC_NOTICE_VERSION,
        **_consent_request_details(request),
    )


class _InMemoryUploadHandler(MemoryFileUploadHandler):
    """Keeps every uploaded file in memory whatever its size. Django's default
    writes files over FILE_UPLOAD_MAX_MEMORY_SIZE to a temp file on disk; the
    licence photo must never touch disk (audit P-5/P-13). Nginx's
    client_max_body_size still caps the request."""
    def handle_raw_input(self, *args, **kwargs):
        self.activated = True


@csrf_exempt
def verify_driver_view(request):
    # Upload handlers must be swapped before anything reads the body, and
    # CsrfViewMiddleware reads it, so CSRF is checked inside instead.
    request.upload_handlers = [_InMemoryUploadHandler(request)]
    return _verify_driver_view(request)


@csrf_protect
def _verify_driver_view(request):
    """POST /profile/verify-driver/ - calls DL-Verification Node API and updates driver_verified."""
    try:
        import requests as http_client
    except ImportError:
        return JsonResponse({"error": "requests library not installed on this server."}, status=500)

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    # request.user only: a session-stored user id outlived deactivation
    # (closed or banned accounts kept access to this view).
    user = request.user if request.user.is_authenticated else None

    if not user:
        return JsonResponse({"error": "Not authenticated"}, status=401)

    if _rate_limited(request, 'verify-driver', extra_key=str(user.id)):
        return JsonResponse({"error": "Too many verification attempts. Please try again later."}, status=429)

    # Biometric consent (privacy policy s.12): the driver must accept the
    # current notice on this attempt, before any image is read or sent. An
    # old version means the page predates a wording change, so the driver
    # hasn't seen the text now in force.
    if request.POST.get("biometric_consent") != "1":
        return JsonResponse({"error": "Please read the biometric notice and tick the box to agree before verifying."}, status=400)
    if request.POST.get("notice_version") != settings.BIOMETRIC_NOTICE_VERSION:
        return JsonResponse({"error": "The biometric notice has changed. Please reload the page and review it again."}, status=400)

    # The licence photo comes with this request and is never written to
    # storage: it is resized in memory, sent to DL-Verification and dropped
    # with the request (audit P-5/P-13).
    license_file = request.FILES.get("license_image")
    if not license_file:
        return JsonResponse({"error": "Please add a photo of your driving license."}, status=400)
    try:
        license_bytes = _resize_image(license_file, label=f"user={user.id} verify-license").read()
    except ValidationError as e:
        return JsonResponse({"error": f"License photo: {e.messages[0]} Please choose another photo."}, status=400)

    # Only the selfie captured for this attempt counts. The profile photo is
    # never used: it may legitimately show the driver's vehicle instead of
    # their face, and it isn't proof that the person is present now.
    selfie_file = request.FILES.get("selfie")
    if not selfie_file:
        return JsonResponse({"error": "Please take a selfie using the camera when prompted."}, status=400)
    # Same size as the stored photos, not the full-resolution original.
    try:
        selfie_bytes = _resize_image(selfie_file, label=f"user={user.id} verify-selfie").read()
    except ValidationError as e:
        return JsonResponse({"error": f"Selfie: {e.messages[0]} Please take the selfie again."}, status=400)

    # The verification pipeline scores DL OCR, face biometrics, and vehicle
    # ALPR as one composite decision -- a missing vehicle photo makes the
    # whole pipeline reject the driver (not just the vehicle), so a vehicle
    # with a photo is required before calling out to the service at all.
    user_vehicle = user.vehicles.filter(is_primary=True).first() or user.vehicles.first()
    if not user_vehicle or not user_vehicle.photo_url:
        return JsonResponse({"error": "Please add a vehicle with a photo to your profile before verifying."}, status=400)

    try:
        with open_stored_file(user_vehicle.photo_url) as f:
            vehicle_bytes = f.read()
    except Exception:
        logger.exception("verify_driver_view: could not read vehicle image for user %s", user.id)
        return JsonResponse({"error": "Could not read vehicle image. Please try again later."}, status=500)

    payload = {
        "request_id": f"req_{uuid.uuid4().hex[:8]}",
        "driver_id": f"usr_{user.id}",
        "personal_info": {
            "full_name": user.full_name or "",
            "date_of_birth": user.dob.strftime("%Y-%m-%d") if user.dob else "",
        },
        "license_details": {
            "license_number": user.license_number or "",
            "issuing_province": user.license_province or "",
            "expiry_date": user.license_expiry.strftime("%Y-%m-%d") if user.license_expiry else "",
        },
        "vehicle_details": {
            "make": user_vehicle.make,
            "model": user_vehicle.model,
            "year": user_vehicle.year,
            "color": user_vehicle.color,
            "plate_number": user_vehicle.plate,
        },
        "images": {
            "selfie_base64": base64.b64encode(selfie_bytes).decode("utf-8"),
            "license_image_base64": base64.b64encode(license_bytes).decode("utf-8"),
            "vehicle_photo_base64": base64.b64encode(vehicle_bytes).decode("utf-8"),
        },
    }

    # The consent record is written before the images leave the server.
    _record_biometric_consent(request, user)

    resp = None
    try:
        resp = http_client.post(
            settings.DL_VERIFICATION_API_URL,
            headers={"X-API-Key": settings.DL_VERIFICATION_API_KEY},
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
    except http_client.exceptions.ConnectionError:
        return JsonResponse({"error": "Verification service is offline. Make sure DL-Verification is running."}, status=503)
    except Exception:
        logger.exception("verify_driver_view: verification service call failed for user %s", user.id)
        upload_logger.warning("verification failed: user=%s service error (HTTP %s)",
                              user.id, getattr(resp, "status_code", "-"))
        return JsonResponse({"error": "Verification service error. Please try again later."}, status=503)

    decision = result.get("decision")
    stages = result.get("stages", {})
    stage_results = {
        name: bool((stages.get(name) or {}).get("passed", False))
        for name in ("license_ocr", "face_biometrics", "vehicle_verification")
    }
    # The service approves on one combined score (licence OCR + face +
    # vehicle, > 85%), which can carry a failed check. A driver passes only
    # if every check passed as well; a missing check counts as failed.
    verified = decision == "APPROVED" and all(stage_results.values())
    stage2 = stages.get("face_biometrics", {})
    try:
        similarity = float(stage2.get("vlm_confidence", 0.0))
    except (TypeError, ValueError):
        similarity = 0.0

    user.driver_verified  = verified
    user.face_match_score = similarity
    update_fields = ["driver_verified", "face_match_score"]
    if verified:
        # Starts a new DRIVER_REVERIFY_DAYS period.
        user.driver_verified_at        = timezone.now()
        user.reverify_reminder_sent_at = None
        update_fields += ["driver_verified_at", "reverify_reminder_sent_at"]
    user.save(update_fields=update_fields)

    if stage_results["vehicle_verification"]:
        user_vehicle.vehicle_verified = True
        user_vehicle.save(update_fields=["vehicle_verified"])

    rejection_reasons = result.get("rejection_reasons") or []
    if not isinstance(rejection_reasons, list):
        rejection_reasons = [str(rejection_reasons)]
    rejection_reasons = [str(r) for r in rejection_reasons]
    # An approval with a failed check comes with no reasons; name the checks.
    if not verified and not rejection_reasons:
        rejection_reasons = [_STAGE_FAILED_REASONS[name] for name, ok in stage_results.items() if not ok]

    # Privacy policy s.9/s.13: keep the outcome and minimum details only. The
    # OCR output and the reason text (which can quote licence values) are not
    # stored; the driver sees the reasons in this response.
    reason_codes = _reason_codes(rejection_reasons)

    # No clear decision from the service: a person decides (audit P-10) and
    # the driver waits, unverified.
    if decision not in ("APPROVED", "REJECTED"):
        log_result = VerificationLog.RESULT_MANUAL_REVIEW
    else:
        log_result = VerificationLog.RESULT_APPROVED if verified else VerificationLog.RESULT_REJECTED
    borderline = log_result == VerificationLog.RESULT_MANUAL_REVIEW
    log = VerificationLog.objects.create(
        user=user,
        stage_results=stage_results,
        reason_codes=reason_codes or None,
        face_score=similarity,
        result=log_result,
        **({"review_status": VerificationLog.REVIEW_PENDING,
            "review_source": VerificationLog.REVIEW_SOURCE_BORDERLINE,
            "review_requested_at": timezone.now()} if borderline else {}),
    )
    if borderline:
        _send_review_admin_email(request, log)
    # Reason codes only, never the reason text (see above).
    upload_logger.info(
        "verification result: user=%s decision=%s stages=%s reasons=%s image_bytes=selfie:%d,license:%d,vehicle:%d",
        user.id, decision, stage_results, reason_codes,
        len(selfie_bytes), len(license_bytes), len(vehicle_bytes),
    )

    details = "; ".join(rejection_reasons)

    return JsonResponse({
        "verified":   verified,
        "similarity": round(similarity * 100, 1),
        "face_match": stage2,
        "decision":   {"status": decision, "details": details},
        # A person will look at this result anyway (borderline).
        "review_pending": borderline,
        # Law 25 s.12.1: a rejection can be sent to a person for review.
        "can_request_review": log_result == VerificationLog.RESULT_REJECTED,
    })


VERIFICATION_REVIEW_COMMENT_MAX_CHARS = 2000


def _latest_verification(user):
    return VerificationLog.objects.filter(user=user).order_by('-created_at', '-id').first()


def _verification_review_state(user):
    log = _latest_verification(user)
    if not log:
        return ""
    if log.review_status == VerificationLog.REVIEW_PENDING:
        return "pending"
    if log.result == VerificationLog.RESULT_REJECTED and not log.review_status:
        return "available"
    return ""


def _send_review_admin_email(request, log):
    """Tell staff a verification attempt is waiting in the review queue."""
    admin_url = request.build_absolute_uri(reverse('admin:komute_verificationlog_change', args=[log.id]))
    user = log.user
    lines = [
        f"Verification #{log.id} needs a review ({log.get_review_source_display()})",
        '─' * 45,
        '',
        f"Driver:    {user.full_name} <{user.email}> (user {user.id})",
        f"Result:    {log.get_result_display()}",
        f"Checks:    {log.stage_results}",
        f"Reasons:   {', '.join(log.reason_codes or []) or '(none)'}",
    ]
    if log.driver_comment:
        lines += ['', "Driver's comment:", log.driver_comment]
    lines += ['', f"Review it in the admin: {admin_url}"]
    send_later(EmailMultiAlternatives(
        subject=f"[Komüte Verification] Review needed — #{log.id}",
        body='\n'.join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[settings.ADMIN_EMAIL],
    ), EmailLog.KIND_REVIEW_ADMIN)


@login_required
@require_POST
def request_verification_review_view(request):
    """POST /profile/verify-driver/review/ - the driver asks a person to
    review their latest rejected verification (Quebec Law 25 s.12.1, GDPR
    Art. 22). Puts it in the admin's review queue with their comment."""
    comment = request.POST.get('comment', '').strip()
    if len(comment) > VERIFICATION_REVIEW_COMMENT_MAX_CHARS:
        return JsonResponse({'error': f'Please keep your comment under {VERIFICATION_REVIEW_COMMENT_MAX_CHARS} characters.'}, status=400)
    log = _latest_verification(request.user)
    if log and log.review_status == VerificationLog.REVIEW_PENDING:
        return JsonResponse({'error': 'A review of your verification is already under way.'}, status=409)
    if not log or log.result != VerificationLog.RESULT_REJECTED or log.review_status:
        return JsonResponse({'error': 'There is no rejected verification to review.'}, status=400)
    log.review_status = VerificationLog.REVIEW_PENDING
    log.review_source = VerificationLog.REVIEW_SOURCE_DRIVER
    log.driver_comment = comment or None
    log.review_requested_at = timezone.now()
    log.save(update_fields=['review_status', 'review_source', 'driver_comment', 'review_requested_at'])
    _send_review_admin_email(request, log)
    return JsonResponse({'ok': True})


@login_required
def route_demand_view(request):
    if request.user.role != 'driver':
        return redirect('profile')

    from collections import Counter
    from django.utils.timesince import timesince as _timesince

    driver   = request.user
    now      = timezone.now()
    today    = now.date()
    week_ago = now - timedelta(days=7)

    # Start from the driver's posted rides — always the anchor
    driver_rides = list(
        Rides.objects
        .filter(driver=driver, status__in=['posted', 'full'])
        .prefetch_related('bookings')
        .order_by('depart_at')
    )

    # Searches by logged-in users in the past 7 days, narrowed in SQL to the
    # boxes around the driver's routes (audit F-3). Recurring rides share a
    # route, so each distinct route adds one box pair.
    radius = settings.SEARCH_RADIUS_KM
    routes = {(r.origin_lat, r.origin_lng, r.dest_lat, r.dest_lng) for r in driver_rides}
    near_routes = Q(pk__in=[])
    for o_lat, o_lng, d_lat, d_lng in routes:
        near_routes |= (_geo_box_q(o_lat, o_lng, radius, 'origin_lat', 'origin_lng')
                        & _geo_box_q(d_lat, d_lng, radius, 'dest_lat', 'dest_lng'))
    recent_searches = list(
        RideSearch.objects
        .filter(near_routes, created_at__gte=week_ago, user__isnull=False, user__is_active=True)
        .exclude(user=driver)
        # Blocked riders can't be invited, so don't list them (ISSUES.md A1).
        .exclude(user_id__in=UserBlock.objects.hidden_ids(driver.id))
        .select_related('user')
        .order_by('-created_at')
    ) if routes else []

    # Riders who searched within the radius of a route's origin AND
    # destination, computed once per route (newest first).
    route_matches = {
        key: [
            s for s in recent_searches
            if _haversine_km(key[0], key[1], s.origin_lat, s.origin_lng) <= radius
            and _haversine_km(key[2], key[3], s.dest_lat, s.dest_lng) <= radius
        ]
        for key in routes
    }

    AVATAR_COLORS = [
        'linear-gradient(135deg,#7c3aed,#a78bfa)',
        'linear-gradient(135deg,#0C63B4,#38bdf8)',
        'linear-gradient(135deg,#dc2626,#f87171)',
        'linear-gradient(135deg,#047857,#34d399)',
        'linear-gradient(135deg,#d97706,#fbbf24)',
        'linear-gradient(135deg,#be185d,#f472b6)',
        'linear-gradient(135deg,#0C63B4,#0CB439)',
        'linear-gradient(135deg,#0e7490,#22d3ee)',
    ]

    route_demands = []
    for ride in driver_rides:
        matching_searches = route_matches[
            (ride.origin_lat, ride.origin_lng, ride.dest_lat, ride.dest_lng)]

        count  = len(matching_searches)
        is_new = any(s.created_at.date() == today for s in matching_searches)

        if count >= 6:
            heat, demand_label = 'hot',  'High demand 🔥'
            demand_percent = min(100, count * 8)
        elif count >= 4:
            heat, demand_label = 'warm', 'Moderate demand'
            demand_percent = min(100, count * 10)
        elif count > 0:
            heat, demand_label = 'cool', 'Low demand'
            demand_percent = min(100, count * 12)
        else:
            heat, demand_label = 'cool', 'No searches yet'
            demand_percent = 0

        if matching_searches:
            weekdays = Counter(s.search_date.strftime('%a') for s in matching_searches)
            top_days = [d for d, _ in weekdays.most_common(2)]
            most_searched_days = '–'.join(top_days) if top_days else None
            avg_raw    = sum(s.passenger_count for s in matching_searches) / count
            avg_seats_n = round(avg_raw)
            avg_seats  = (
                f'{avg_raw:.0f}–{avg_seats_n + 1} seats'
                if avg_raw != avg_seats_n
                else f'{avg_seats_n} seat{"s" if avg_seats_n != 1 else ""}'
            )
            latest_search = matching_searches[0]
        else:
            most_searched_days = None
            avg_seats          = None
            latest_search      = None

        # Build per-rider rows (one per unique user). Searches are newest
        # first, so a user's first one here is their latest.
        seen_users = set()
        riders = []
        for s in matching_searches:
            uid = s.user_id
            if uid in seen_users:
                continue
            seen_users.add(uid)
            u    = s.user
            # Not invitable under the ride's gender preference (audit L-8).
            if not _gender_pref_allows(ride, u):
                continue
            name = _display_name(u)
            initials = ''.join(w[0].upper() for w in name.split()[:2])
            avatar_color = AVATAR_COLORS[uid % len(AVATAR_COLORS)]
            # avg_rating is 0 iff the rider has never received a review --
            # real ratings are 1-5 (Review.rating's MinValueValidator), so a
            # real average can never land on 0.
            rating_label = f'⭐ {round(u.avg_rating, 1)} rider rating' if u.avg_rating else 'Not yet rated'
            riders.append({
                'id':              uid,
                'initials':        initials,
                'avatar_color':    avatar_color,
                'name':            name,
                'searching_since': _timesince(s.created_at),
                'rating_label':    rating_label,
                'seats_needed':    s.passenger_count,
            })

        booked_seats = ride.seats_total - ride.seats_available
        open_seats   = ride.seats_available
        ride_date    = None
        if ride.depart_at.date() != today:
            _d = ride.depart_at
            ride_date = f"{_d.strftime('%b')} {_d.day}"

        route_demands.append({
            'heat':              heat,
            'is_my_route':       True,
            'is_new':            is_new,
            'has_my_ride':       True,
            'ride_id':           encode_id(ride.id),
            'from_location':     ride.origin,
            'to_location':       ride.destination,
            'search_count':      count,
            'demand_label':      demand_label,
            'demand_percent':    demand_percent,
            'match_label':       'Your posted ride',
            'most_searched_days': most_searched_days,
            'specific_date':     None,
            'peak_time':         None,
            'departure_range':   None,
            'avg_seats':         avg_seats,
            'last_updated':      _timesince(latest_search.created_at) if latest_search else None,
            'available_seats':   open_seats,
            'ride_date':         ride_date,
            'booked_seats':      booked_seats,
            'open_seats':        open_seats,
            'riders':            riders,
        })

    # Most-demand rides first
    route_demands.sort(key=lambda d: d['search_count'], reverse=True)

    hot_count       = sum(1 for d in route_demands if d['heat'] == 'hot')
    new_today_count = sum(1 for d in route_demands if d['is_new'])

    stats = {
        'riders_searching':   RideSearch.objects.filter(
                                created_at__gte=week_ago, user__isnull=False
                              ).values('user').distinct().count(),
        'hot_routes':         hot_count,
        'invites_sent_today': RideInvite.objects.filter(
                                driver=driver, sent_at__date=today
                              ).count(),
        'invites_accepted':   RideInvite.objects.filter(
                                driver=driver, status=RideInvite.STATUS_ACCEPTED
                              ).count(),
        'total_count':        len(route_demands),
        'hot_count':          hot_count,
        'my_routes_count':    len(route_demands),
        'new_today_count':    new_today_count,
    }

    has_active_rides = len(driver_rides) > 0

    return render(request, 'route-demand-ride_available.html', {
        'route_demands':   route_demands,
        'stats':           stats,
        'has_active_rides': has_active_rides,
    })


# ──────────────────────────────────────────────────────────────────
# INVITE HELPERS
# ──────────────────────────────────────────────────────────────────

_INVITE_SALT = 'komute-invite-v1'


def _make_invite_token(invite_id):
    """Return a signed, URL-safe token encoding the invite ID.
    The token is verified and decoded by _decode_invite_token.
    Signing uses Django's SECRET_KEY + a salt so changing either
    the ID or the salt (e.g. after a key rotation) invalidates it."""
    return signing.dumps(invite_id, salt=_INVITE_SALT)


def _decode_invite_token(token):
    """Decode and verify a token produced by _make_invite_token.
    Returns the invite_id integer.
    Raises signing.SignatureExpired if older than settings.INVITE_EXPIRY_HOURS.
    Raises signing.BadSignature if tampered or invalid."""
    return signing.loads(
        token,
        salt=_INVITE_SALT,
        max_age=settings.INVITE_EXPIRY_HOURS * 3600,
    )


def _invite_is_expired(invite):
    return (timezone.now() - invite.sent_at).total_seconds() > settings.INVITE_EXPIRY_HOURS * 3600


def _send_invite_email(request, invite):
    try:
        ride   = invite.ride
        driver = invite.driver
        rider  = invite.passenger

        token = _make_invite_token(invite.id)
        invite_url  = request.build_absolute_uri(
            reverse('invite-detail', args=[token])
        )
        accept_url  = invite_url
        decline_url = invite_url

        ctx = {
            'rider_name':    (rider.full_name  or rider.email).title(),
            'driver_name':   _display_name(driver),
            'origin':        ride.origin,
            'destination':   ride.destination,
            'depart_display': _fmt_ride_local(ride),
            'price':         f"${ride.price_cents / 100:.2f}",
            'seats_total':   ride.seats_total,
            'accept_url':    accept_url,
            'decline_url':   decline_url,
        }

        html = render_to_string('account/email/ride_invite.html', ctx)
        dest = ride.destination.split(',')[0].strip()

        msg = EmailMultiAlternatives(
            subject=f"{ctx['driver_name']} invited you to their ride to {dest} 🚗",
            body=(
                f"Hi {ctx['rider_name']}, you've been invited to a ride "
                f"from {ride.origin} to {ride.destination}. "
                f"Accept: {accept_url}  Decline: {decline_url}"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[rider.email],
            reply_to=[settings.REPLY_TO_EMAIL_RIDE_INVITE],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_INVITE, rider)
    except Exception:
        logger.exception('_send_invite_email could not build or queue an email')


# ──────────────────────────────────────────────────────────────────
# INVITE VIEWS
# ──────────────────────────────────────────────────────────────────

_INVITE_DISCOVERY_WINDOW_DAYS = 7  # matches route_demand_view's own window


def _rider_is_inviteable(driver, ride, rider):
    """A driver may only invite a rider they could legitimately have found
    through the app: someone who recently searched for a route matching this
    ride (same geo-matching used by route_demand_view), or someone who has
    booked with this driver before. Blocks inviting via an arbitrary/
    enumerated user ID that the driver has no real connection to."""
    since = timezone.now() - timedelta(days=_INVITE_DISCOVERY_WINDOW_DAYS)
    matched_search = any(
        _haversine_km(ride.origin_lat, ride.origin_lng, s.origin_lat, s.origin_lng) <= settings.SEARCH_RADIUS_KM
        and _haversine_km(ride.dest_lat, ride.dest_lng, s.dest_lat, s.dest_lng) <= settings.SEARCH_RADIUS_KM
        for s in RideSearch.objects.filter(user=rider, created_at__gte=since)
    )
    if matched_search:
        return True
    return RideBooking.objects.filter(passenger=rider, ride__driver=driver).exists()


@login_required
@require_POST
def send_invite_view(request):
    if _rate_limited(request, 'send-invite', extra_key=str(request.user.id)):
        return JsonResponse({'error': 'Too many invites sent. Please try again later.'}, status=429)
    if _rate_limited(request, 'send-invite-daily', extra_key=str(request.user.id)):
        return JsonResponse({'error': 'Daily invite limit reached. Please try again tomorrow.'}, status=429)

    try:
        data     = json.loads(request.body)
        rider_id = int(data['rider_id'])
        ride_id  = decode_id(data['ride_id'])
    except (KeyError, ValueError, json.JSONDecodeError, TypeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    if not ride_id:
        return JsonResponse({'error': 'Invalid ride.'}, status=400)

    driver = request.user
    ride   = get_object_or_404(Rides, id=ride_id, driver=driver, status__in=['posted', 'full'])

    if not driver.driver_verification_current:
        return JsonResponse({'error': 'Please verify your identity on your profile page before inviting riders.'}, status=403)

    # Closed accounts can't be invited (audit P-14): they stay in the
    # database until the 45-day purge, and a prior booking would still
    # make them inviteable below.
    rider  = User.objects.filter(id=rider_id, is_active=True).first()

    if rider and rider == driver:
        return JsonResponse({'error': 'Cannot invite yourself.'}, status=400)

    # Identical response whether rider_id doesn't exist at all or exists but
    # isn't a legitimate invite target for this ride — otherwise the status
    # code/message becomes an oracle for enumerating real user IDs. The same
    # goes for a block either way (ISSUES.md A1): the driver isn't told.
    if (rider is None or UserBlock.objects.between(driver.id, rider.id)
            or not _gender_pref_allows(ride, rider)
            or not _rider_is_inviteable(driver, ride, rider)):
        return JsonResponse({'error': 'This rider is not eligible to be invited to this ride.'}, status=400)

    if ride.seats_available <= 0:
        return JsonResponse({'error': 'Ride is full.'}, status=400)

    already = RideInvite.objects.filter(
        ride=ride, passenger=rider,
        status__in=[RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED],
    ).exists()
    if already:
        return JsonResponse({'error': 'Invite already sent to this rider.'}, status=400)

    invite = RideInvite.objects.create(driver=driver, passenger=rider, ride=ride)
    _send_invite_email(request, invite)

    return JsonResponse({'ok': True, 'invite_id': invite.id})


def _resolve_invite_token(token):
    """Decode token → invite_id, or return (None, error_string)."""
    try:
        invite_id = _decode_invite_token(token)
        return invite_id, None
    except signing.SignatureExpired:
        return None, 'expired_token'
    except signing.BadSignature:
        return None, 'invalid_token'


@login_required
def invite_detail_view(request, token):
    invite_id, err = _resolve_invite_token(token)
    if err == 'invalid_token':
        raise Http404
    if err == 'expired_token':
        return render(request, 'invite-detail.html', {'expired_token': True})

    invite = get_object_or_404(RideInvite, id=invite_id)

    if request.user != invite.passenger:
        return render(request, 'invite-detail.html', {
            'invite': invite,
            'error':  'This invite was not sent to your account.',
        })

    expired = _invite_is_expired(invite)

    if invite.status == RideInvite.STATUS_SENT and not expired:
        invite.status = RideInvite.STATUS_VIEWED
        invite.save(update_fields=['status'])

    if expired and invite.status not in [
        RideInvite.STATUS_ACCEPTED,
        RideInvite.STATUS_DECLINED,
        RideInvite.STATUS_EXPIRED,
    ]:
        invite.status = RideInvite.STATUS_EXPIRED
        invite.save(update_fields=['status'])

    return render(request, 'invite-detail.html', {
        'invite':  invite,
        'token':   token,
        'expired': expired,
    })


@login_required
def accept_invite_view(request, token):
    if request.method == 'GET':
        return redirect('invite-detail', token=token)

    invite_id, err = _resolve_invite_token(token)
    if err:
        return render(request, 'invite-detail.html', {'expired_token': err == 'expired_token'})

    invite = get_object_or_404(RideInvite, id=invite_id)

    if request.user != invite.passenger:
        return render(request, 'invite-detail.html', {
            'invite': invite,
            'token':  token,
            'error':  'This invite was not sent to your account.',
        })

    if invite.status not in (RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED):
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token,
            'error':  'This invite has already been responded to.',
        })

    if _invite_is_expired(invite):
        invite.status = RideInvite.STATUS_EXPIRED
        invite.save(update_fields=['status'])
        return render(request, 'invite-detail.html', {'invite': invite, 'token': token, 'expired': True})

    ride = invite.ride

    if (ride.status in ['cancelled', 'completed'] or ride.depart_at <= timezone.now()
            or UserBlock.objects.between(ride.driver_id, request.user.id)):
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token,
            'error':  'This ride is no longer available.',
        })

    if not request.user.email_verified:
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token, 'error': _EMAIL_UNVERIFIED_MSG,
        })

    if not request.user.dob:
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token, 'error': _DOB_MISSING_MSG,
        })

    # The rules a normal booking follows (audit L-7/L-8): the gender
    # preference, and one active booking per rider per ride.
    if not _gender_pref_allows(ride, request.user):
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token, 'error': _GENDER_PREF_MSG,
        })
    existing = RideBooking.objects.filter(
        ride=ride,
        passenger=request.user,
        status__in=[RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED],
    ).first()
    if existing and existing.status != RideBooking.STATUS_DRAFT:
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token,
            'error':  'You already have a booking on this ride.',
        })
    seats = existing.seats if existing else 1

    # Accepting an invite confirms a booking, so the driver's verification
    # must be current (same rule as accept_booking_view).
    if not ride.driver.driver_verification_current:
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token,
            'error':  "This invite can't be accepted right now because the driver needs to re-verify their identity. Please try again later.",
        })

    # Atomically claim the invite first so a replayed/concurrent accept
    # (double-click, resubmit) can't pass this check twice.
    prior_status = invite.status
    claimed = RideInvite.objects.filter(
        id=invite.id, status__in=(RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED),
    ).update(status=RideInvite.STATUS_ACCEPTED, responded_at=timezone.now())
    if not claimed:
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token,
            'error':  'This invite has already been responded to.',
        })

    updated = Rides.objects.filter(
        id=ride.id, seats_available__gte=seats
    ).update(seats_available=F('seats_available') - seats)
    if not updated:
        # Couldn't complete the acceptance — release the claim so the invite
        # stays actionable instead of being stuck "accepted" with no booking.
        RideInvite.objects.filter(id=invite.id).update(status=prior_status, responded_at=None)
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token,
            'error':  'Sorry, this ride is now full.' if seats == 1
                      else 'Sorry, this ride no longer has enough seats.',
        })
    Rides.objects.filter(id=ride.id, seats_available=0).update(status='full')

    if existing:
        # Confirm the rider's draft in place, as create_ride_booking promotes
        # it: keeps its chat history, seat count and any price agreed there.
        # Conditional, so a draft booked or dropped meanwhile isn't overwritten.
        confirmed_at = timezone.now()
        promoted = RideBooking.objects.filter(
            id=existing.id, status=RideBooking.STATUS_DRAFT,
        ).update(status=RideBooking.STATUS_CONFIRMED, confirmed_at=confirmed_at)
        if not promoted:
            Rides.objects.filter(id=ride.id).update(
                seats_available=Least(F('seats_available') + seats, ride.seats_total))
            Rides.objects.filter(id=ride.id, status='full').update(status='posted')
            RideInvite.objects.filter(id=invite.id).update(status=prior_status, responded_at=None)
            return render(request, 'invite-detail.html', {
                'invite': invite, 'token': token,
                'error':  'Your booking on this ride changed meanwhile. Please reload and try again.',
            })
        booking = existing
        booking.status       = RideBooking.STATUS_CONFIRMED
        booking.confirmed_at = confirmed_at
        _notify_booking_status(booking)
    else:
        booking = RideBooking.objects.create(
            ride=ride,
            passenger=invite.passenger,
            seats=seats,
            agreed_price_cents=ride.price_cents * seats + settings.SERVICE_FEE_CENTS,
            status=RideBooking.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )
    notify_bookings_changed([ride.driver_id], 'A passenger accepted your ride invite.')

    _send_booking_confirmation_emails(booking)

    return redirect('ride-summary', ride_id=encode_id(ride.id))


@login_required
def decline_invite_view(request, token):
    if request.method == 'GET':
        return redirect('invite-detail', token=token)

    invite_id, err = _resolve_invite_token(token)
    if err:
        return render(request, 'invite-detail.html', {'expired_token': err == 'expired_token'})

    invite = get_object_or_404(RideInvite, id=invite_id)

    if request.user != invite.passenger:
        return render(request, 'invite-detail.html', {
            'invite': invite,
            'token':  token,
            'error':  'This invite was not sent to your account.',
        })

    updated = RideInvite.objects.filter(
        id=invite.id, status__in=(RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED),
    ).update(status=RideInvite.STATUS_DECLINED, responded_at=timezone.now())
    if not updated:
        return render(request, 'invite-detail.html', {
            'invite': invite, 'token': token,
            'error':  'This invite has already been responded to.',
        })
    invite.status = RideInvite.STATUS_DECLINED

    return render(request, 'invite-declined.html', {'invite': invite})


# ──────────────────────────────────────────────────────────────────
# REPORT AND BLOCK (ISSUES.md A1)
# ──────────────────────────────────────────────────────────────────

REPORT_DETAILS_MAX_CHARS = 2000


def _safety_target(request):
    """The other user a report or block is about, and the booking it came
    from. Named by a booking the signed-in user is part of (`booking`), or by
    a ride they can view (`ride`, meaning its driver), never by a raw user
    ID. Returns (None, None) if neither names someone else."""
    me = request.user
    booking_key = request.POST.get('booking', '').strip()
    ride_key = request.POST.get('ride', '').strip()
    if booking_key:
        booking_id = decode_id(booking_key)
        booking = booking_id and (
            RideBooking.objects.select_related('ride__driver', 'passenger').filter(id=booking_id).first()
        )
        if not booking:
            return None, None
        if me == booking.passenger:
            return booking.ride.driver, booking
        if me == booking.ride.driver:
            return booking.passenger, booking
        return None, None
    if ride_key:
        ride_id = decode_id(ride_key)
        ride = ride_id and Rides.objects.select_related('driver').filter(id=ride_id).first()
        if not ride or ride.driver == me:
            return None, None
        booking = RideBooking.objects.filter(ride=ride, passenger=me).order_by('-booked_at').first()
        return ride.driver, booking
    return None, None


def _send_report_admin_email(request, report):
    """Tell staff a report came in. The report is saved either way."""
    admin_url = request.build_absolute_uri(reverse('admin:komute_userreport_change', args=[report.id]))
    booking = report.booking
    lines = [
        f"New user report #{report.id}",
        '─' * 45,
        '',
        f"Reported:  {report.reported.full_name} <{report.reported.email}> (user {report.reported_id})",
        f"Reporter:  {report.reporter.full_name} <{report.reporter.email}> (user {report.reporter_id})",
        f"Reason:    {report.get_reason_display()}",
    ]
    if booking:
        lines.append(f"Booking:   #{booking.id} ({booking.status}), ride {booking.ride.origin} → {booking.ride.destination}")
    lines += ['', 'Details:', report.details or '(none)', '', f"Review it in the admin: {admin_url}"]
    send_later(EmailMultiAlternatives(
        subject=f"[Komüte Report] {report.get_reason_display()} — report #{report.id}",
        body='\n'.join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[settings.ADMIN_EMAIL],
    ), EmailLog.KIND_REPORT_ADMIN)


@login_required
@require_POST
def report_user_view(request):
    if _rate_limited(request, 'report-user', extra_key=str(request.user.id)):
        return JsonResponse({'error': 'Too many reports sent. Please try again later.'}, status=429)

    reported, booking = _safety_target(request)
    if reported is None:
        return JsonResponse({'error': 'User not found.'}, status=404)

    reason = request.POST.get('reason', '').strip()
    if reason not in dict(UserReport.REASON_CHOICES):
        return JsonResponse({'error': 'Please choose a reason.'}, status=400)
    details = request.POST.get('details', '').strip()
    if len(details) > REPORT_DETAILS_MAX_CHARS:
        return JsonResponse({'error': f'Please keep the details under {REPORT_DETAILS_MAX_CHARS} characters.'}, status=400)
    if reason == UserReport.REASON_OTHER and not details:
        return JsonResponse({'error': 'Please tell us what happened.'}, status=400)

    report = UserReport.objects.create(
        reporter=request.user,
        reported=reported,
        booking=booking,
        reason=reason,
        details=details,
    )
    _send_report_admin_email(request, report)
    return JsonResponse({'ok': True})


def _close_chats_between(user_a, user_b):
    """Lock any open chat page between two users who are now blocked."""
    bookings = (
        RideBooking.objects
        .filter(Q(ride__driver=user_a, passenger=user_b) | Q(ride__driver=user_b, passenger=user_a))
        .filter(status__in=(RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED))
        .select_related('ride')
    )
    for booking in bookings:
        _notify_booking_status(booking)


@login_required
@require_POST
def block_user_view(request):
    target, _ = _safety_target(request)
    if target is None:
        return JsonResponse({'error': 'User not found.'}, status=404)

    _, created = UserBlock.objects.get_or_create(blocker=request.user, blocked=target)
    if created:
        _close_chats_between(request.user, target)
    return JsonResponse({'ok': True})


@login_required
@require_POST
def unblock_user_view(request, user_id):
    real_id = decode_id(user_id)
    deleted = 0
    if real_id:
        deleted, _ = UserBlock.objects.filter(blocker=request.user, blocked_id=real_id).delete()
    if deleted:
        messages.success(request, "User unblocked.")
    return redirect('profile')
