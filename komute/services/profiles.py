"""Profile: the details on the profile page, the profile photo and the
driver's verification status (API module 2).

Moved from komute/views.py profile_view, which now calls these. The vehicle
rows of that page are in komute.services.vehicles."""
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

from komute import private_media
from komute.private_media import save_private_image
from komute.services.accounts import PHONE_NUMBER_RE, PHONE_RE, parse_dob, valid_dial_codes
from komute.services.common import norm_code, norm_name
from komute.services.images import resize_image

SAVABLE_ROLES = ("rider", "driver")
GENDERS = ("male", "female", "other")

IDENTITY_CHANGED_MSG = ("Your name, date of birth or license details changed. "
                        "Please verify your identity again before posting rides.")
DRIVER_NEEDS_VEHICLE_MSG = "You must add at least one vehicle to be a driver."


def split_phone(phone):
    """Split a stored E.164 phone string back into (country_code, phone_number)
    for re-populating the separate form fields."""
    if not phone:
        return "", ""
    for code in sorted(valid_dial_codes(), key=len, reverse=True):
        if phone.startswith(code):
            return code, phone[len(code):]
    return "", phone


def form_values(user):
    """The profile form's fields as the page shows them (strings), for a
    caller that changes only some of them."""
    country_code, phone_number = split_phone(user.phone)
    return {
        'full_name': user.full_name,
        'country_code': country_code,
        'phone_number': phone_number,
        'dob': user.dob.isoformat() if user.dob else "",
        # A stored value outside the choices can't be shown in the page's
        # dropdown, so the page sends it back blank; do the same.
        'gender': user.gender if user.gender in GENDERS else "",
        'role': user.role or "rider",
        'bio': user.bio or "",
        'license_number': user.license_number or "",
        'license_province': user.license_province or "",
        'license_expiry': user.license_expiry.isoformat() if user.license_expiry else "",
    }


INVALID_GENDER_MSG = "Please select a valid gender."
# The profile page's box stops typing at this many characters (maxlength).
BIO_MAX_LENGTH = 280
BIO_TOO_LONG_MSG = f"Self description can be at most {BIO_MAX_LENGTH} characters."


def check_profile(full_name, country_code, phone_number, dob_str, role, license_expiry_str, gender="",
                  bio=""):
    """The profile form's checks, apart from the vehicle rows. Returns
    (errors, E.164 phone or "", dob, license_expiry). Errors are in the
    order the page has always reported them."""
    errors = {}
    if not full_name:
        errors["full_name"] = "Full name is required."
    if role not in SAVABLE_ROLES:
        errors["role"] = "Invalid role selected."
    # Ride gender preferences (women/men only) match on these exact values,
    # so nothing else may be stored. Blank means "not given".
    if gender and gender not in GENDERS:
        errors["gender"] = INVALID_GENDER_MSG
    # Counted as the browser counts for maxlength: a line break is one
    # character, though the form sends it as two (\r\n).
    if len((bio or "").replace("\r\n", "\n")) > BIO_MAX_LENGTH:
        errors["bio"] = BIO_TOO_LONG_MSG

    phone = ""
    if phone_number or country_code:
        if not country_code or country_code not in valid_dial_codes():
            errors["country_code"] = "Please select a valid country code."
        if not phone_number:
            errors["phone_number"] = "Phone number is required."
        elif not PHONE_NUMBER_RE.match(phone_number):
            errors["phone_number"] = "Enter a valid phone number (digits only)."
        if not errors.get("country_code") and not errors.get("phone_number"):
            phone = f"{country_code}{phone_number}"
            if not PHONE_RE.match(phone):
                errors["phone_number"] = "Phone must be in E.164 format (e.g. +14155552671)."

    # Required for riders too: booking needs it (audit L-5).
    dob, dob_error = parse_dob(dob_str)
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
            # server-side. The verification node cross-checks this value against
            # the license, so it must be a real, current expiry.
            if role == "driver" and license_expiry < timezone.localdate():
                errors["license_expiry"] = "Your license has expired. Please enter the expiry date of a valid license."
    return errors, phone, dob, license_expiry


def identity_changed(user, role, full_name, dob, license_number, license_province, license_expiry):
    """Details the verification node compared against the license. Changing any of
    them means the driver is no longer verified as entered (C3). Comparison
    mirrors the service: case and hyphens ignored."""
    return role == "driver" and user.driver_verified and (
        norm_name(full_name) != norm_name(user.full_name)
        or dob != user.dob
        or norm_code(license_number) != norm_code(user.license_number)
        or license_province.casefold() != (user.license_province or "").casefold()
        or license_expiry != user.license_expiry
    )


def apply_profile(user, *, full_name, phone, dob, gender, role, bio,
                  license_number, license_province, license_expiry):
    """Sets the checked details on `user` without saving. Returns
    (update_fields, identity_reset): identity_reset is True when the change
    ended the driver's verification (show IDENTITY_CHANGED_MSG)."""
    changed = identity_changed(user, role, full_name, dob, license_number, license_province, license_expiry)

    user.full_name = full_name
    user.phone     = phone or None
    user.dob       = dob
    user.gender    = gender or None
    user.role      = role
    user.bio       = bio or None

    update_fields = ["full_name", "phone", "dob", "gender", "role", "bio"]
    identity_reset = False
    if role == "driver":
        user.license_number   = license_number or None
        user.license_province = license_province or None
        user.license_expiry   = license_expiry
        update_fields += ["license_number", "license_province", "license_expiry"]

        # The licence photo isn't saved with the profile: it is taken
        # in the verification step and never stored (audit P-5/P-13).

        if changed and user.driver_verified:
            user.driver_verified  = False
            user.face_match_score = None
            update_fields += ["driver_verified", "face_match_score"]
            identity_reset = True
    return update_fields, identity_reset


def set_profile_photo(user, photo_file):
    """Resizes and stores a new profile photo on `user` (not saved yet).
    Raises ValidationError for a file that isn't a usable image, before
    changing anything. Returns (update_fields, stale_urls): delete the stale
    files once the user row is saved."""
    resized = resize_image(photo_file, label=f"user={user.id} profile-photo")
    stale_urls = [user.selfie_url, user.avatar_url]
    url = save_private_image(private_media.KIND_SELFIE, user.id, resized)
    user.selfie_url = url
    user.avatar_url = url
    # No re-verification needed: the profile photo (face or vehicle) is
    # never compared with the license.
    return ["selfie_url", "avatar_url"], stale_urls


def driver_status(user, vehicle_verified_flags):
    """The verification facts the profile page shows. `vehicle_verified_flags`
    is the vehicle_verified value of each of the user's vehicles."""
    reverify_due_at = user.driver_reverify_due_at
    verification_current = user.driver_verification_current
    return {
        "driver_verified": verification_current,
        "reverify_due_at": reverify_due_at,
        # Verified against a license that has since expired.
        "license_expired": user.driver_verified and user.license_expired,
        # Verified driver with a vehicle that still needs a verification run.
        "has_unverified_vehicle": verification_current and any(not v for v in vehicle_verified_flags),
        # Verified, but inside the reminder window before it lapses.
        "reverify_soon": verification_current and reverify_due_at is not None and (
            reverify_due_at - timezone.now() <= timedelta(days=settings.DRIVER_REVERIFY_REMINDER_DAYS)
        ),
        # Was verified, and that verification has run out.
        "reverify_expired": not verification_current and reverify_due_at is not None
                            and timezone.now() >= reverify_due_at,
    }
