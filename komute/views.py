import json
import logging
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlsplit


from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.http import Http404
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout, update_session_auth_hash
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.core.paginator import Paginator
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.contrib.auth.decorators import login_required
from allauth.account.views import ConfirmEmailView as AllauthConfirmEmailView
from django.core.mail import EmailMultiAlternatives  # noqa: F401  (tests patch it here)
from django.core import signing
from django.utils.http import url_has_allowed_host_and_scheme
from django.urls import Resolver404, resolve, reverse

from .client_ip import client_ip as _client_ip
from .models import Rides, Vehicle, RideBooking, PriceOffer, Message, Review, RideInvite, UserBlock, LegalAcceptance
from .middleware import LegalAcceptanceMiddleware, legal_versions_key
from .hashid_utils import encode_id, decode_id, decode_id_strict as decode_hashid
from .notifications import booking_fingerprint
from . import private_media
from .private_media import delete_stored_file, save_private_image
# Account logic shared with the mobile app API (docs/api-modular-breakup.md,
# module 1). The old private names below are kept as aliases; some are only
# used by komute.social and the tests.
from .services import accounts
from .services.accounts import (
    account_deletion_message as _account_deletion_message,
    build_data_export as _build_data_export,
    check_signup_details as _check_signup_details,
    decode_deactivate_token as _decode_deactivate_token,
    decode_password_reset_token as _decode_password_reset_token,
    latest_adult_dob as _latest_adult_dob,
    load_country_codes as _load_country_codes,
    make_deactivate_token as _make_deactivate_token,  # noqa: F401
    make_password_reset_token as _make_password_reset_token,  # noqa: F401
    send_delete_account_email as _send_delete_account_email,
    send_welcome_email as _send_welcome_email,  # noqa: F401
)
from .services.common import (
    UNNAMED_USER as _UNNAMED_USER,
    consent_details,
    display_name as _display_name,
)
# Profile and vehicles (module 2).
from .services import profiles, vehicles as vehicle_rules
from .services.images import resize_image as _resize_image
from .services.profiles import split_phone as _split_phone
# Rides (module 3).
from .services.booking_status import (  # noqa: F401  (BLOCKED_CHAT_MESSAGE: tests)
    BLOCKED_CHAT_MESSAGE,
    chat_status as _chat_status,
)
from .services.geo import (  # noqa: F401  (_haversine_km, _geo_box_q, _max_ride_date: tests)
    geo_box_q as _geo_box_q,
    haversine_km as _haversine_km,
    max_ride_date as _max_ride_date,
    zone_or_utc as _zone_or_utc,
)
from .services import rides as ride_rules
from .services.receipts import build_receipt_pdf
from .services import invites, route_demand
# Driver verification (module 8).
from .services import verification
from .services.verification import (  # noqa: F401  (_reason_codes: tests)
    InMemoryUploadHandler as _InMemoryUploadHandler,
    reason_codes as _reason_codes,
    review_state as _verification_review_state,
)
# Reviews, reports and blocks (module 7).
from .services import contact, safety
# Chat history (module 5).
from .services import chat as chat_rules
# Bookings and price offers (module 4).
from .services import bookings as booking_rules
from .services.bookings import (  # noqa: F401  (komute.consumers, tests)
    send_booking_cancelled_emails as _send_booking_cancelled_emails,
    send_booking_confirmation_emails as _send_booking_confirmation_emails,
    send_booking_rejected_email as _send_booking_rejected_email,
    send_booking_request_email as _send_booking_request_email,
)
from .services.invites import (  # noqa: F401  (_make_invite_token: tests)
    make_invite_token as _make_invite_token,
    resolve_invite_token as _resolve_invite_token,
)
from .services.rides import (  # noqa: F401  (RIDE_PREFERENCE_OPTIONS: tests)
    MAX_TZ_OFFSET_MINUTES as _MAX_TZ_OFFSET_MINUTES,
    RIDE_PREFERENCE_OPTIONS,
    parse_duration_seconds as _parse_duration_seconds,
)

User = get_user_model()
logger = logging.getLogger(__name__)


def _consent_request_details(request):
    """IP address and user agent stored with a consent or acceptance record.
    An address that doesn't parse is stored as null."""
    return consent_details(_client_ip(request), request.META.get("HTTP_USER_AGENT", ""))


def _record_legal_acceptance(request, user, source):
    """Stores that `user` accepted the current Terms and Privacy Policy, and
    remembers it in the session so LegalAcceptanceMiddleware lets them on."""
    accounts.record_legal_acceptance(user, source, _consent_request_details(request))
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




def _clean_stops(post):
    """The form's stops, checked by komute.services.rides.clean_stops."""
    return ride_rules.clean_stops(post.getlist('stop_location'), post.getlist('stop_lat'), post.getlist('stop_lng'))


def _vehicle_rows_error(post):
    """The first vehicle row that profile_view would save (make, plate and a
    year) with a value too long for its column or a year out of range, as
    the message to show; None if all fit. Values are read exactly as the
    save loop reads them."""
    makes       = post.getlist("vehicle_make")
    models_list = post.getlist("vehicle_model")
    years       = post.getlist("vehicle_year")
    colors      = post.getlist("vehicle_color")
    plates      = post.getlist("vehicle_plate")
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
        model = models_list[i] if i < len(models_list) else ""
        color = colors[i] if i < len(colors) else ""
        message = vehicle_rules.fields_error(make, model, yr, color, plate)
        if message:
            return f"Vehicle {i + 1}: {message}"
    return None


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


upload_logger = logging.getLogger("komute.uploads")


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

        errors, phone, dob = _check_signup_details(full_name, country_code, phone_number,
                                                   dob_str, agreed_to_terms)
        errors.update(accounts.check_signup_credentials(email, password, confirm_password, full_name))

        if not errors:
            accounts.create_account(request, email=email, password=password, full_name=full_name,
                                    phone=phone, dob=dob, consent=_consent_request_details(request))
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

        # Case-insensitive, as forgot-password is (see the service).
        user = accounts.authenticate_by_email(request, email, password)
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
        if accounts.set_role(user, request.POST.get("role")):
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

        errors, phone, dob, license_expiry = profiles.check_profile(
            full_name, country_code, phone_number, dob_str, role, license_expiry_str, gender, bio)

        if role == "driver":
            submitted_plates = [p.strip() for p in request.POST.getlist("vehicle_plate") if p.strip()]
            if len(submitted_plates) != len({p.upper() for p in submitted_plates}):
                errors["vehicles"] = vehicle_rules.DUPLICATE_PLATE_MSG
            else:
                for plate in submitted_plates:
                    if vehicle_rules.plate_registered_elsewhere(user, plate):
                        errors["vehicles"] = vehicle_rules.plate_taken_msg(plate)
                        break

            if not errors.get("vehicles"):
                rows_error = _vehicle_rows_error(request.POST)
                if rows_error:
                    errors["vehicles"] = rows_error

            if not errors.get("vehicles") and not _has_valid_vehicle_row(request.POST):
                errors["vehicles"] = profiles.DRIVER_NEEDS_VEHICLE_MSG

        if not errors:
            # Name, DOB or licence changes end a driver's verification (C3).
            update_fields, identity_reset = profiles.apply_profile(
                user, full_name=full_name, phone=phone, dob=dob, gender=gender, role=role, bio=bio,
                license_number=license_number, license_province=license_province,
                license_expiry=license_expiry)
            # Replaced image files, deleted only once the new URLs are saved.
            stale_urls = []
            # A rejected photo must not end in "Profile saved successfully".
            image_rejected = False

            if identity_reset:
                messages.warning(request, profiles.IDENTITY_CHANGED_MSG)

            # selfie / profile picture upload — all roles
            selfie_file = request.FILES.get("selfie_file")
            if selfie_file and selfie_file.size > 0:
                try:
                    photo_fields, photo_stale = profiles.set_profile_photo(user, selfie_file)
                except ValidationError as e:
                    image_rejected = True
                    messages.error(request, f"Profile photo: {e.messages[0]}")
                else:
                    stale_urls += photo_stale
                    update_fields += photo_fields

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
                        # Verified only by a verification-node pass (C3).
                        vehicle = Vehicle(owner=user, vehicle_verified=False)

                    model = models_list[i] if i < len(models_list) else ""
                    color = colors[i] if i < len(colors) else ""
                    # A verification pass vouched for these vehicle details
                    # (the node compares the plate); any real change voids that.
                    details_changed = not is_new and vehicle_rules.details_changed(
                        vehicle, make, model, yr, color, plate)

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
    status = profiles.driver_status(user, [v["vehicle_verified"] for v in vehicles])
    context = {
        "user": user,
        "full_name": full_name,
        "email": user.email,
        # Apple "Hide My Email" accounts show their communication email in
        # place of the relay address (komute/contact_email.py).
        "uses_private_relay": user.uses_private_relay,
        "contact_email": user.contact_email or "",
        "contact_email_confirmed": user.contact_email_confirmed,
        "mail_to": user.mail_to,
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
        # driver_verified, reverify_due_at, license_expired,
        # has_unverified_vehicle, reverify_soon, reverify_expired.
        **status,
        "face_match_score": user.face_match_score,
        "biometric_notice_version": settings.BIOMETRIC_NOTICE_VERSION,
        # Human review of the latest attempt (audit P-10): "pending" while
        # it's in the queue, "available" if a rejection can still be sent.
        "verification_review": _verification_review_state(user),
        "image_upload_max_mb": settings.IMAGE_UPLOAD_MAX_MB,
        "upload_request_max_mb": settings.UPLOAD_REQUEST_MAX_MB,
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

    # Driver, verified email, date of birth, current verification, a
    # verified vehicle (in that order).
    reason = ride_rules.post_ride_block_reason(user)
    if reason:
        messages.error(request, reason)
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
        if error := ride_rules.endpoint_error(origin, origin_lat, origin_lng, "pickup", "Starting point is required."):
            errors["origin"] = error
        if error := ride_rules.endpoint_error(destination, dest_lat, dest_lng, "drop-off",
                                         "Final destination is required."):
            errors["destination"] = error
        clean_stops, stops_error = _clean_stops(request.POST)
        if stops_error:
            errors["stops"] = stops_error
        if not date_str:
            errors["date"] = "Date is required."
        if not time_str:
            errors["depart_time"] = "Departure time is required."
        if not vehicle_id:
            errors["vehicle"] = "Please select a vehicle."

        seats, error = ride_rules.seats_error(seats_str)
        if error:
            errors["seats"] = error

        price_cents, error = ride_rules.price_error(price_str, ride_rules.POST_PRICE_ZERO_MSG)
        if error:
            errors["price"] = error

        depart_at = None
        depart_utc_str = request.POST.get('depart_utc', '').strip()
        if depart_utc_str and "date" not in errors and "depart_time" not in errors:
            try:
                depart_at = datetime.fromisoformat(depart_utc_str.replace('Z', '+00:00'))
                if timezone.is_naive(depart_at):
                    from datetime import timezone as _utc
                    depart_at = depart_at.replace(tzinfo=_utc.utc)
                if bad := ride_rules.departure_error(depart_at, ride_rules.POST_TOO_FAR_MSG):
                    errors[bad[0]] = bad[1]
            except ValueError:
                errors["depart_time"] = "Invalid departure date or time."
        elif date_str and time_str and "date" not in errors and "depart_time" not in errors:
            try:
                # No depart_utc (script didn't run): the form's date and time
                # are the driver's local ones.
                depart_at = timezone.make_aware(datetime.fromisoformat(f"{date_str}T{time_str}"), driver_tz)
                if bad := ride_rules.departure_error(depart_at, ride_rules.POST_TOO_FAR_MSG):
                    errors[bad[0]] = bad[1]
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
            recurring_errs, recurring_end, recurring_days = ride_rules.recurring_errors(
                recurring_end_str, recurring_days_str, depart_at, driver_tz)
            errors.update(recurring_errs)

        if not errors:
            duration_seconds = _parse_duration_seconds(request.POST.get("duration_seconds"))

            first_ride = ride_rules.create_rides(
                user, vehicle=vehicle, origin=origin, origin_lat=origin_lat, origin_lng=origin_lng,
                destination=destination, dest_lat=dest_lat, dest_lng=dest_lng, depart_at=depart_at,
                seats=seats, price_cents=price_cents, passenger_gender_pref=passenger_gender_pref,
                notes=notes, preferences_json=preferences_json, tz_name=tz_name, driver_tz=driver_tz,
                duration_seconds=duration_seconds, stops=clean_stops,
                recurring_end=recurring_end if is_recurring else None, recurring_days=recurring_days)

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
        if date_error := ride_rules.search_date_error(parsed_search_date, rider_today):
            errors['date'] = date_error

    if search_performed:
        try:
            s_origin_lat = float(request.POST.get('origin_lat') or 0)
            s_origin_lng = float(request.POST.get('origin_lng') or 0)
            s_dest_lat   = float(request.POST.get('dest_lat') or 0)
            s_dest_lng   = float(request.POST.get('dest_lng') or 0)
        except (ValueError, TypeError):
            s_origin_lat = s_origin_lng = s_dest_lat = s_dest_lng = 0.0

        errors.update(ride_rules.search_area_errors(
            search_from, search_to, s_origin_lat, s_origin_lng, s_dest_lat, s_dest_lng))

    if search_performed and not errors:
        rides = ride_rules.search_rides(
            request.user, search_from=search_from, search_to=search_to, parsed_date=parsed_search_date,
            rider_tz=rider_tz, seats_needed=seats_needed, max_price_str=filter_max_price,
            time_slot=filter_time_slot, preference=filter_preference,
            origin_lat=s_origin_lat, origin_lng=s_origin_lng, dest_lat=s_dest_lat, dest_lng=s_dest_lng)
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

        vehicle = ride_rules.vehicle_card(ride.vehicle)
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
            'vehicle_color':       vehicle['color'],
            'vehicle_display':     vehicle['display'],
            'vehicle_plate':       vehicle['plate'],
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
        # Verified email and date of birth, an open ride, the gender
        # preference, seats 1-8, one active booking (komute.services.bookings).
        try:
            booking = booking_rules.book_ride(
                ride, request.user, intent=request.POST.get("intent", "book"),
                seats_raw=request.POST.get("seats", 1),
                pickup_stop_id=request.POST.get("pickup_stop_id"),
                dropoff_stop_id=request.POST.get("dropoff_stop_id"))
        except booking_rules.BookingError as e:
            return JsonResponse({'ok': False, 'error': e.message}, status=e.status)
        from .chat_tokens import make_chat_token
        return JsonResponse({'ok': True, 'chat_token': make_chat_token(booking.id)})

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
        'vehicle_color':       ride_rules.vehicle_card(v)['color'],
        'vehicle_plate':       ride_rules.vehicle_card(v)['plate'],
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

    try:
        booking_rules.cancel_booking(booking)
    except booking_rules.BookingError as e:
        return JsonResponse({"error": e.message}, status=e.status)

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

    try:
        booking_rules.accept_booking(booking)
    except booking_rules.BookingError as e:
        return JsonResponse({'error': e.message}, status=e.status)

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

    try:
        booking_rules.reject_booking(booking)
    except booking_rules.BookingError as e:
        return JsonResponse({'error': e.message}, status=e.status)

    return JsonResponse({'ok': True})


@login_required
def cancel_ride_view(request, ride_id):
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)

    real_ride_id = decode_id(ride_id)
    if not real_ride_id:
        raise Http404
    ride = get_object_or_404(Rides, id=real_ride_id, driver=request.user)

    error = ride_rules.cancel_error(ride)
    if error:
        return JsonResponse({"error": error}, status=400)

    ride_rules.cancel_ride(ride)

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
            # Seats on bookings the trip carried: a request never answered
            # holds a seat but earned nothing.
            carried = sum(b.seats for b in ride.bookings.all() if b.status in ride_rules.CARRIED_STATUSES)
            total_earned_cents += ride.price_cents * carried
            passengers_helped += carried

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

    other_user = chat_rules.other_party(booking, request.user)
    initials = lambda u: ''.join(p[0].upper() for p in u.full_name.split()[:2])

    history = chat_rules.history(booking, request.user)

    # Mark unread messages (received by current user) as read
    chat_rules.mark_read(history, request.user)

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
        pending_incoming = chat_rules.latest_pending_offer(booking, other_user)
        pending_incoming_price = pending_incoming.price_cents // 100 if pending_incoming else None
        pending_incoming_offer_id = pending_incoming.id if pending_incoming else None

        pending_outgoing = chat_rules.latest_pending_offer(booking, request.user)
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

    try:
        contact.send_message(*(request.POST.get(f, '').strip()
                               for f in ('name', 'email', 'role', 'topic', 'subject', 'message')))
    except contact.ContactError as e:
        return JsonResponse({'error': e.message}, status=e.status)

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

    reason = ride_rules.edit_block_reason(ride)
    if reason:
        messages.error(request, reason)
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
        if error := ride_rules.endpoint_error(origin, origin_lat, origin_lng, 'pickup', 'Starting point is required.'):
            errors['origin'] = error
        if error := ride_rules.endpoint_error(destination, dest_lat, dest_lng, 'drop-off',
                                         'Final destination is required.'):
            errors['destination'] = error
        clean_stops, stops_error = _clean_stops(request.POST)
        if stops_error:
            errors['stops'] = stops_error
        if not date_str:    errors['date']        = 'Date is required.'
        if not time_str:    errors['depart_time'] = 'Departure time is required.'

        seats, error = ride_rules.seats_error(seats_str)
        if error:
            errors['seats'] = error

        price_cents, error = ride_rules.price_error(price_str, ride_rules.EDIT_PRICE_ZERO_MSG)
        if error:
            errors['price'] = error

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
                if bad := ride_rules.departure_error(depart_at, ride_rules.EDIT_TOO_FAR_MSG):
                    errors[bad[0]] = bad[1]
            except ValueError:
                errors['depart_time'] = 'Invalid departure date or time.'
        elif date_str and time_str and 'date' not in errors and 'depart_time' not in errors:
            try:
                tz_offset = int(request.POST.get('timezone_offset', 0))
                if abs(tz_offset) > _MAX_TZ_OFFSET_MINUTES:
                    raise ValueError('timezone offset out of range')
                depart_at = timezone.make_aware(datetime.fromisoformat(f'{date_str}T{time_str}')) + timedelta(minutes=tz_offset)
                if bad := ride_rules.departure_error(depart_at, ride_rules.EDIT_TOO_FAR_MSG):
                    errors[bad[0]] = bad[1]
            except (ValueError, OverflowError):  # OverflowError: a date near year 9999
                errors['depart_time'] = 'Invalid departure date or time.'

        if not errors:
            duration_seconds = _parse_duration_seconds(request.POST.get('duration_seconds'))

            try:
                ride_rules.update_ride(
                    ride, origin=origin, origin_lat=origin_lat, origin_lng=origin_lng, destination=destination,
                    dest_lat=dest_lat, dest_lng=dest_lng, depart_at=depart_at, tz_name=tz_name,
                    duration_seconds=duration_seconds, seats=seats, price_cents=price_cents, notes=notes,
                    preferences_json=preferences_json, passenger_gender_pref=passenger_gender_pref,
                    stops=clean_stops)
            except ride_rules.EditBlocked as e:
                messages.error(request, e.reason)
                return redirect('my-rides')

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

    is_driver, is_passenger = ride_rules.summary_viewer(ride, request.user)

    if not is_driver and not is_passenger:
        messages.error(request, "You don't have access to this ride summary.")
        return redirect('my-rides')

    if ride.status != 'completed':
        messages.error(request, "Ride summary is only available for completed rides.")
        return redirect('my-rides')

    stats = ride_rules.summary_stats(ride)
    origin_city, dest_city = stats['origin_city'], stats['dest_city']
    distance_km, duration = stats['distance_km'], stats['duration']
    active_bookings, passenger_count = stats['active_bookings'], stats['passenger_count']
    co2_saved_kg, trees_equiv, km_avoided = stats['co2_saved_kg'], stats['trees_equiv'], stats['km_avoided']

    # Sorted here so the prefetched stops are reused, not queried again.
    stops = sorted(ride.stops.all(), key=lambda s: s.stop_order)
    stop_pickup_counts = {}
    for b in active_bookings:
        if b.pickup_stop_id:
            stop_pickup_counts[b.pickup_stop_id] = stop_pickup_counts.get(b.pickup_stop_id, 0) + b.seats
    for stop in stops:
        stop.passengers_picked_up = stop_pickup_counts.get(stop.id, 0)

    stops_json = json.dumps([{'lng': s.lng, 'lat': s.lat} for s in stops])

    arrival_note = stats['arrival_note']

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

    # The same test as saving the review: this booking's trip happened. The
    # page used to open for a cancelled, rejected or draft booking on a
    # completed ride, and the review was then refused.
    if not safety.took_place(booking):
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
        # The opaque id, as everywhere else (submit_review_view decodes it).
        'reviewee_id':         encode_id(reviewee.id),
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
    # A JSON list or number here used to end in a server error.
    if not isinstance(data, dict):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    booking_key  = data.get('booking_id', '')
    booking_id   = decode_id(booking_key) if isinstance(booking_key, str) else None
    reviewee_id  = data.get('reviewee_id')
    rating       = data.get('rating')
    is_anonymous = bool(data.get('anonymous', False))

    if not all([booking_id, reviewee_id, rating]):
        return JsonResponse({'error': 'booking_id, reviewee_id, and rating are required.'}, status=400)

    # Whole stars 1-5; a comment, tags and detailed ratings as the page offers
    # them (komute.services.safety).
    try:
        rating = safety.clean_rating(rating)
        body, tags, subratings = safety.clean_review_extras(
            data.get('comment'), data.get('tags'), data.get('subratings'))
    except safety.SafetyError as e:
        return JsonResponse({'error': e.message}, status=400)

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

    # The page sends the opaque id; a page opened before that change sent the
    # database id, which is still understood.
    reviewee_key = str(reviewee_id)
    reviewee_id = decode_hashid(reviewee_key)
    if reviewee_id is None:
        if not reviewee_key.isdigit():
            return JsonResponse({'error': 'Invalid reviewee_id.'}, status=400)
        reviewee_id = int(reviewee_key)

    expected_reviewee_id = booking.passenger_id if is_driver else ride.driver_id
    if reviewee_id != expected_reviewee_id:
        return JsonResponse({'error': 'Invalid reviewee.'}, status=400)

    if Review.objects.filter(booking=booking, reviewer=user).exists():
        return JsonResponse({'error': 'You have already reviewed this trip.'}, status=400)

    safety.save_review(booking, user, expected_reviewee_id, rating=rating, body=body,
                       is_anonymous=is_anonymous, tags=tags, subratings=subratings)

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
    AVATAR_CLASSES = ['c1', 'c2', 'c3', 'c4', 'c5']
    now = timezone.now()

    conversations = []
    for row in booking_rules.inbox(user, as_driver=True):
        booking, ride, passenger = row['booking'], row['ride'], row['other']
        last_msg, unread_count, latest_offer = row['last_msg'], row['unread_count'], row['latest_offer']
        nego_status = row['nego_status']
        if nego_status == 'agreed':
            nego_label = f"Agreed ${row['nego_cents'] / 100:.2f}"
        elif nego_status == 'pending':
            nego_label = f"{'Offer' if row['nego_by_other'] else 'Counter'} ${row['nego_cents'] / 100:.2f}"
        else:
            nego_label = ''
        ui_status = row['status']

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
            'blocked_by_me':      row['blocked_by_me'],
            'initials':           initials,
            'avatar_class':       AVATAR_CLASSES[row['index'] % 5],
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

    AVATAR_CLASSES = ['c1', 'c2', 'c3', 'c4', 'c5']
    now = timezone.now()

    conversations = []
    for row in booking_rules.inbox(user, as_driver=False):
        booking, ride, driver = row['booking'], row['ride'], row['other']
        last_msg, unread_count, latest_offer = row['last_msg'], row['unread_count'], row['latest_offer']
        nego_status = row['nego_status']
        if nego_status == 'agreed':
            nego_label = f"Agreed ${row['nego_cents'] / 100:.2f}"
        elif nego_status == 'pending':
            nego_label = f"{'Offer' if row['nego_by_other'] else 'Counter'} ${row['nego_cents'] / 100:.2f}"
        else:
            nego_label = ''
        ui_status = row['status']

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
            'blocked_by_me':      row['blocked_by_me'],
            'initials':           initials,
            'avatar_class':       AVATAR_CLASSES[row['index'] % 5],
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

    try:
        accounts.change_password(request.user, current, new_pwd, confirm)
    except accounts.AccountError as e:
        return JsonResponse({'error': e.message}, status=400)

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

    try:
        offer = booking_rules.create_offer(booking, request.user, request.POST.get('price_cents', 0))
    except booking_rules.BookingError as e:
        return JsonResponse({'error': e.message}, status=e.status)

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
    action = request.POST.get('action', '').strip()
    try:
        result = booking_rules.respond_offer(offer, request.user, action, request.POST.get('price_cents', 0))
    except booking_rules.BookingError as e:
        return JsonResponse({'error': e.message}, status=e.status)

    if action == 'accept':
        return JsonResponse({'ok': True, 'status': result.status, 'price_cents': result.price_cents})
    if action == 'decline':
        return JsonResponse({'ok': True, 'status': result.status})
    return JsonResponse({
        'ok': True,
        'offer_id': result.id,
        'price_cents': result.price_cents,
        'status': result.status,
    }, status=201)


@login_required
@require_POST
def send_verification_email(request):
    try:
        user = request.user

        if accounts.is_email_address_verified(user):
            return JsonResponse({"message": "Already verified"})

        target_email = accounts.verification_target(user)

        if (_rate_limited(request, 'send-verification', extra_key=str(user.id), per_ip=False)
                or _rate_limited(request, 'email-confirm-target', extra_key=target_email.lower(), per_ip=False)):
            return JsonResponse({"error": _TOO_MANY_ATTEMPTS_MSG}, status=429)

        accounts.send_verification_email(request, user)

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
        accounts.check_email_change(user, new_email, password)
    except accounts.AccountError as e:
        return JsonResponse({'error': e.message}, status=400)

    # Caps mail to one address however many accounts send it.
    if _rate_limited(request, 'email-confirm-target', extra_key=new_email, per_ip=False):
        return JsonResponse({'error': _TOO_MANY_ATTEMPTS_MSG}, status=429)

    try:
        accounts.start_email_change(request, user, new_email)

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

# Tokens, the email and the data export live in komute.services.accounts
# (imported at the top under their old names).


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

    return JsonResponse({'message': f'A confirmation link has been sent to {user.mail_to}. Click it to delete your account.'})


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


_DELETE_ACCOUNT_SENT_MESSAGE = accounts.DELETE_ACCOUNT_SENT_MESSAGE


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

    accounts.close_account(user)

    if request.user.is_authenticated and request.user.id == user.id:
        logout(request)

    messages.success(request, _account_deletion_message())
    return redirect('login')


# ──────────────────────────────────────────────────────────────────
# PASSWORD RESET
# ──────────────────────────────────────────────────────────────────

# Reset tokens and the email live in komute.services.accounts.


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

        accounts.send_password_reset_email(request, email)

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

        try:
            accounts.reset_password(user, new_pwd, confirm)
        except accounts.AccountError as e:
            messages.error(request, e.message)
            return render(request, 'reset-password.html', {'token': token})

        messages.success(request, 'Your password has been reset. Please sign in with your new password.')
        return redirect('login')

    return render(request, 'reset-password.html', {'token': token})



@login_required
def ride_receipt_pdf_view(request, ride_id):
    real_ride_id = decode_id(ride_id)
    if not real_ride_id:
        raise Http404
    ride = get_object_or_404(
        ride_rules.receipt_rides(request.user).prefetch_related('stops', 'bookings__passenger', 'bookings__pickup_stop'),
        id=real_ride_id,
    )
    pdf, ride_ref = build_receipt_pdf(ride)
    response = HttpResponse(pdf, content_type='application/pdf')
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

    response = private_media.file_response(rel_path)
    if response is None:
        raise Http404
    return response


@csrf_exempt
def verify_driver_view(request):
    # Upload handlers must be swapped before anything reads the body, and
    # CsrfViewMiddleware reads it, so CSRF is checked inside instead.
    request.upload_handlers = [_InMemoryUploadHandler(request)]
    return _verify_driver_view(request)


@csrf_protect
def _verify_driver_view(request):
    """POST /profile/verify-driver/ - calls the verification node and updates driver_verified."""
    try:
        import requests as http_client  # noqa: F401  (500 early if it is missing)
    except ImportError:
        return JsonResponse({"error": "requests library not installed on this server."}, status=500)

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    # request.user only: a session-stored user id outlived deactivation
    # (closed or banned accounts kept access to this view).
    user = request.user if request.user.is_authenticated else None

    if not user:
        return JsonResponse({"error": "Not authenticated"}, status=401)

    # Biometric consent (privacy policy s.12): the driver must accept the
    # current notice on this attempt, before any image is read or sent. An
    # old version means the page predates a wording change, so the driver
    # hasn't seen the text now in force.
    refused = verification.consent_error(request.POST.get("biometric_consent") == "1",
                                         request.POST.get("notice_version"))
    if refused:
        return JsonResponse({"error": refused[0]}, status=400)

    # The licence photos and selfie are resized in memory, sent to
    # the verification node and dropped (audit P-5/P-13; komute.services.verification).
    try:
        result = verification.verify(request, user, request.FILES.get("license_image"),
                                     request.FILES.get("license_back_image"),
                                     request.FILES.get("selfie"), _consent_request_details(request))
    except verification.VerificationError as e:
        return JsonResponse({"error": e.message}, status=e.status)

    return JsonResponse({
        "verified":   result["verified"],
        "decision":   {"status": result["decision"], "details": "; ".join(result["reasons"])},
        # A person will look at this result anyway (borderline).
        "review_pending": result["review_pending"],
        # Law 25 s.12.1: a rejection can be sent to a person for review.
        "can_request_review": result["can_request_review"],
    })


@login_required
@require_POST
def request_verification_review_view(request):
    """POST /profile/verify-driver/review/ - the driver asks a person to
    review their latest rejected verification (Quebec Law 25 s.12.1, GDPR
    Art. 22). Puts it in the admin's review queue with their comment."""
    try:
        verification.request_review(request, request.user, request.POST.get('comment', '').strip())
    except verification.VerificationError as e:
        return JsonResponse({'error': e.message}, status=e.status)
    return JsonResponse({'ok': True})


@login_required
def route_demand_view(request):
    if request.user.role != 'driver':
        return redirect('profile')

    return render(request, 'route-demand-ride_available.html', route_demand.build(request.user))


# ──────────────────────────────────────────────────────────────────
# INVITE HELPERS
# ──────────────────────────────────────────────────────────────────

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

    try:
        invite = invites.send_invite(request, driver, ride, rider_id)
    except invites.InviteError as e:
        return JsonResponse({'error': e.message}, status=e.status)

    return JsonResponse({'ok': True, 'invite_id': invite.id})


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

    expired = invites.open_invite(invite)

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

    try:
        invites.accept_invite(invite, request.user)
    except invites.InviteExpired:
        return render(request, 'invite-detail.html', {'invite': invite, 'token': token, 'expired': True})
    except invites.InviteError as e:
        return render(request, 'invite-detail.html', {'invite': invite, 'token': token, 'error': e.message})

    return redirect('ride-summary', ride_id=encode_id(invite.ride_id))


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

    try:
        invites.decline_invite(invite)
    except invites.InviteError as e:
        return render(request, 'invite-detail.html', {'invite': invite, 'token': token, 'error': e.message})

    return render(request, 'invite-declined.html', {'invite': invite})


# ──────────────────────────────────────────────────────────────────
# REPORT AND BLOCK (ISSUES.md A1)
# ──────────────────────────────────────────────────────────────────

REPORT_DETAILS_MAX_CHARS = safety.REPORT_DETAILS_MAX_CHARS


def _safety_target(request):
    """The other user a report or block names (komute.services.safety)."""
    return safety.safety_target(request.user, request.POST.get('booking', '').strip(),
                                request.POST.get('ride', '').strip())


@login_required
@require_POST
def report_user_view(request):
    if _rate_limited(request, 'report-user', extra_key=str(request.user.id)):
        return JsonResponse({'error': 'Too many reports sent. Please try again later.'}, status=429)

    reported, booking = _safety_target(request)
    if reported is None:
        return JsonResponse({'error': 'User not found.'}, status=404)

    try:
        safety.report_user(request, request.user, reported, booking,
                           request.POST.get('reason', '').strip(), request.POST.get('details', '').strip())
    except safety.SafetyError as e:
        return JsonResponse({'error': e.message}, status=e.status)
    return JsonResponse({'ok': True})


@login_required
@require_POST
def block_user_view(request):
    target, _ = _safety_target(request)
    if target is None:
        return JsonResponse({'error': 'User not found.'}, status=404)

    safety.block(request.user, target)
    return JsonResponse({'ok': True})


@login_required
@require_POST
def unblock_user_view(request, user_id):
    real_id = decode_id(user_id)
    if real_id and safety.unblock(request.user, real_id):
        messages.success(request, "User unblocked.")
    return redirect('profile')
