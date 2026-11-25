"""Accounts: signup, login, role, email verification and change, passwords,
Terms acceptance, account deletion and the data export (API module 1).

Moved here unchanged from komute/views.py, which keeps its old names for
these as aliases. Messages are the website's wording; the API shows the same."""
import json
import logging
import os
import re
from datetime import date

from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from komute.hashid_utils import encode_id
from komute.models import (
    BiometricConsent, EmailLog, LegalAcceptance, Message, PriceOffer, RideBooking, RideInvite,
    Rides, RideSearch, Review, UserBlock, UserReport, VerificationLog,
)
from komute.services.common import display_name, iso, ride_summary
from komute.tasks import send_later

logger = logging.getLogger('komute.views')

TOO_MANY_ATTEMPTS_MSG = "Too many attempts. Please try again later."


class AccountError(Exception):
    """A request the rules refuse. `message` is shown to the user as is."""

    def __init__(self, message, code='invalid', field=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field


def _user_model():
    return get_user_model()


# ──────────────────────────────────────────────────────────────────
# Signup details
# ──────────────────────────────────────────────────────────────────

PHONE_RE = re.compile(r'^\+?[1-9]\d{6,14}$')
PHONE_NUMBER_RE = re.compile(r'^\d{4,14}$')

_country_codes_cache = None


def load_country_codes():
    global _country_codes_cache
    if _country_codes_cache is None:
        path = os.path.join(settings.BASE_DIR, "static", "data", "country_codes.json")
        with open(path, encoding="utf-8") as f:
            _country_codes_cache = json.load(f)
    return _country_codes_cache


def valid_dial_codes():
    return {c["dial_code"] for c in load_country_codes()}


def latest_adult_dob():
    """The latest date of birth that is 18 today (Feb 29 -> Feb 28)."""
    today = date.today()
    try:
        return today.replace(year=today.year - 18)
    except ValueError:
        return today.replace(year=today.year - 18, day=28)


def parse_dob(dob_str):
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


def check_signup_details(full_name, country_code, phone_number, dob_str, agreed_to_terms):
    """Checks the fields both signup pages ask for (email/password signup and
    the Google/Apple one, komute.social). Returns (errors, E.164 phone, dob)."""
    errors = {}
    if not full_name:
        errors["full_name"] = "Full name is required."

    phone = ""
    if not country_code:
        errors["country_code"] = "Please select a country code."
    elif country_code not in valid_dial_codes():
        errors["country_code"] = "Please select a valid country code."

    if not phone_number:
        errors["phone_number"] = "Phone number is required."
    elif not PHONE_NUMBER_RE.match(phone_number):
        errors["phone_number"] = "Enter a valid phone number (digits only)."

    if not errors.get("country_code") and not errors.get("phone_number"):
        phone = f"{country_code}{phone_number}"
        if not PHONE_RE.match(phone):
            errors["phone_number"] = "Phone must be in E.164 format (e.g. +14155552671)."

    # 18+ only (audit L-5): checked here, not first at booking.
    dob, dob_error = parse_dob(dob_str)
    if dob_error:
        errors["dob"] = dob_error

    if agreed_to_terms != "1":
        errors["agreed_to_terms"] = "You must accept the Terms & Conditions and the Privacy Policy to sign up."
    return errors, phone, dob


def check_signup_credentials(email, password, confirm_password, full_name):
    """The email and password checks of the email signup. Returns errors."""
    errors = {}
    User = _user_model()
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
    return errors


# ──────────────────────────────────────────────────────────────────
# Account creation, Terms, welcome email
# ──────────────────────────────────────────────────────────────────

def record_legal_acceptance(user, source, consent):
    """Stores that `user` accepted the current Terms and Privacy Policy.
    `consent` is common.consent_details(); callers that keep a website
    session also remember the acceptance there (views._record_legal_acceptance)."""
    return LegalAcceptance.objects.create(
        user=user,
        terms_version=settings.TERMS_VERSION,
        privacy_version=settings.PRIVACY_VERSION,
        source=source,
        **consent,
    )


def has_accepted_current_terms(user):
    return LegalAcceptance.objects.filter(
        user=user, terms_version=settings.TERMS_VERSION,
        privacy_version=settings.PRIVACY_VERSION).exists()


def send_welcome_email(request, user):
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
            to=[user.mail_to],
            reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_SIGNUP],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_WELCOME, user)
    except Exception:
        logger.exception('_send_welcome_email could not build or queue an email')


def create_account(request, *, email, password, full_name, phone, dob, consent):
    """Email/password signup, after the checks above passed: the user, their
    Terms acceptance, and the welcome email (queued once the row commits)."""
    with transaction.atomic():
        user = _user_model().objects.create_user(
            username=email,
            email=email,
            password=password,
            full_name=full_name,
            phone=phone or None,
            dob=dob,
        )
        record_legal_acceptance(user, LegalAcceptance.SOURCE_SIGNUP, consent)
    send_welcome_email(request, user)
    return user


# ──────────────────────────────────────────────────────────────────
# Login and role
# ──────────────────────────────────────────────────────────────────

def authenticate_by_email(request, email, password):
    """The user for this email and password, or None.

    forgot_password resolves accounts case-insensitively (email__iexact), but
    ModelBackend's username lookup is an exact match -- so look the account
    up the same way here first and authenticate with its actual stored
    casing, or a user whose email was typed/auto-capitalized differently than
    it was stored could reset their password successfully via email yet still
    be told "Invalid email or password" when logging in with it."""
    lookup_email = email
    if email:
        matched_user = _user_model().objects.filter(email__iexact=email).first()
        if matched_user:
            lookup_email = matched_user.email
    return authenticate(request, username=lookup_email, password=password)


SELECTABLE_ROLES = ("rider", "driver")


def set_role(user, role):
    """Rider or driver, as picked on the role-select page. False if `role`
    isn't one of those."""
    if role not in SELECTABLE_ROLES:
        return False
    user.role = role
    user.save(update_fields=["role"])
    return True


# ──────────────────────────────────────────────────────────────────
# Email verification and email change
# ──────────────────────────────────────────────────────────────────

def is_email_address_verified(user):
    return EmailAddress.objects.filter(user=user, verified=True).exists()


def verification_target(user):
    """Where "Verify Now" sends its link. If an email change is pending, the
    EmailAddress row already on file for this user is the pending one
    (start_email_change creates it and deletes any other row for this user)
    -- target that, not user.email, or get_or_create's create() collides
    with it on the unique_primary_email constraint (one primary=True row per
    user) and raises IntegrityError."""
    return user.pending_email or user.email


def send_verification_email(request, user):
    """Emails the "Verify Now" link. Raises if it can't be sent. Callers
    check is_email_address_verified() and their rate limits first."""
    target_email = verification_target(user)
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


def check_email_change(user, new_email, password):
    """Raises AccountError unless `user` may move to `new_email`. `new_email`
    is already stripped and lower-cased. Callers apply the change-email rate
    limit before this and the email-confirm-target limit after it."""
    try:
        validate_email(new_email)
    except ValidationError:
        raise AccountError('Enter a valid email address.', field='new_email')

    if not user.check_password(password):
        raise AccountError('Incorrect password.', code='incorrect_password', field='password')

    if new_email == user.email.lower():
        raise AccountError('This is already your current email.', field='new_email')

    if _user_model().objects.filter(email=new_email).exclude(pk=user.pk).exists():
        raise AccountError('This email is already in use.', code='email_taken', field='new_email')


def start_email_change(request, user, new_email):
    """Stores `new_email` as pending and emails its confirmation link. The
    address changes when the link is confirmed (signals.email_verified_update).
    Raises if the email can't be sent."""
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


# ──────────────────────────────────────────────────────────────────
# Passwords
# ──────────────────────────────────────────────────────────────────

def change_password(user, current, new_pwd, confirm):
    """Raises AccountError, or sets the new password. Callers apply the
    change-password rate limit first, and keep their own session signed in
    (the password hash in every other session no longer matches)."""
    if not user.check_password(current):
        raise AccountError('Current password is incorrect.', code='incorrect_password',
                           field='current_password')

    if new_pwd != confirm:
        raise AccountError('New passwords do not match.', field='confirm_password')

    try:
        validate_password(new_pwd, user=user)
    except ValidationError as e:
        raise AccountError(' '.join(e.messages), field='new_password')

    user.set_password(new_pwd)
    user.save()


PASSWORD_RESET_SALT = 'komute-password-reset-v1'
PASSWORD_RESET_LINK_MAX_AGE_SECONDS = settings.PASSWORD_RESET_LINK_MINUTES * 60
PASSWORD_RESET_SENT_MESSAGE = "If an account exists for that email, we've sent a link to reset your password."


def _password_reset_state_hash(user):
    # Bound to the current password hash, so a reset link — issued by email,
    # forgeable by anyone holding SECRET_KEY (Finding 1), or simply left
    # sitting in an inbox — stops working the instant the password actually
    # changes, instead of staying valid for its full max-age regardless.
    return salted_hmac(PASSWORD_RESET_SALT + '-state', user.password).hexdigest()


def make_password_reset_token(user):
    """Return a signed, URL-safe token encoding the user ID and a hash of
    their current password, verified by decode_password_reset_token."""
    return signing.dumps(
        {'uid': user.id, 'h': _password_reset_state_hash(user)},
        salt=PASSWORD_RESET_SALT,
    )


def decode_password_reset_token(token):
    """Decode and verify a token produced by make_password_reset_token.
    Raises signing.SignatureExpired if older than PASSWORD_RESET_LINK_MINUTES,
    signing.BadSignature if
    tampered, expired, or if the user's password has changed since issuance."""
    data = signing.loads(
        token,
        salt=PASSWORD_RESET_SALT,
        max_age=PASSWORD_RESET_LINK_MAX_AGE_SECONDS,
    )
    user = _user_model().objects.filter(id=data['uid']).first()
    if user is None or not constant_time_compare(data['h'], _password_reset_state_hash(user)):
        raise signing.BadSignature('stale password reset token')
    return user.id


def send_password_reset_email(request, email):
    """The forgot-password email, if `email` (lower-cased) has an account.
    Says nothing either way, and a failed send is swallowed, so the caller
    can always give the same answer. Callers rate-limit first."""
    user = _user_model().objects.filter(email__iexact=email).first() if email else None

    if user:
        token = make_password_reset_token(user)
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


def user_for_password_reset(token):
    """The user a reset link is for. Raises AccountError for an expired,
    invalid or stale link, or a closed account."""
    try:
        user_id = decode_password_reset_token(token)
    except signing.SignatureExpired:
        raise AccountError('This password reset link has expired. Please request a new one.',
                           code='link_expired')
    except signing.BadSignature:
        raise AccountError('Invalid password reset link.', code='link_invalid')

    user = _user_model().objects.filter(id=user_id).first()
    if user is None:
        raise AccountError('Invalid password reset link.', code='link_invalid')

    if not user.is_active:
        # Login (ModelBackend) refuses any is_active=False account regardless
        # of password, so letting the form "succeed" here would silently set
        # a password the user could never log in with. A closed account is
        # reopened only by support (see close_account) -- a password reset
        # alone shouldn't undo it.
        raise AccountError(
            'This account is closed and scheduled for deletion. Contact support if you want it reopened.',
            code='account_closed')
    return user


def reset_password(user, new_pwd, confirm):
    """Raises AccountError, or sets the new password."""
    if new_pwd != confirm:
        raise AccountError('Passwords do not match.', field='confirm_password')

    try:
        validate_password(new_pwd, user=user)
    except ValidationError as e:
        raise AccountError(' '.join(e.messages), field='new_password')

    user.set_password(new_pwd)
    user.save(update_fields=['password'])


# ──────────────────────────────────────────────────────────────────
# Account deletion (ISSUES.md A2)
# The account closes (is_active=False, deactivated_at set) when the emailed
# link is clicked; purge_deactivated_accounts deletes or anonymizes it
# ACCOUNT_DELETION_GRACE_DAYS later.
# ──────────────────────────────────────────────────────────────────

DEACTIVATE_SALT = 'komute-deactivate-v1'
DEACTIVATE_LINK_MAX_AGE_SECONDS = 24 * 3600
DELETE_ACCOUNT_SENT_MESSAGE = (
    "If an account exists for that email, we've sent it a link to confirm the deletion. "
    "Nothing happens until the link is clicked."
)


def _deactivate_state_hash(user):
    # Bound to the user's password hash, so a token issued before a password
    # change (e.g. because an old email got forwarded, or SECRET_KEY leaked
    # and someone minted a token straight from a user ID) stops verifying
    # the moment the password changes, without needing a DB column.
    return salted_hmac(DEACTIVATE_SALT + '-state', user.password).hexdigest()


def make_deactivate_token(user):
    """Return a signed, URL-safe token encoding the user ID and a hash of
    their current password, verified by decode_deactivate_token."""
    return signing.dumps(
        {'uid': user.id, 'h': _deactivate_state_hash(user)},
        salt=DEACTIVATE_SALT,
    )


def decode_deactivate_token(token):
    """Decode and verify a token produced by make_deactivate_token.
    Raises signing.SignatureExpired if older than 24h, signing.BadSignature if
    tampered, expired, or if the user's password has changed since issuance."""
    data = signing.loads(
        token,
        salt=DEACTIVATE_SALT,
        max_age=DEACTIVATE_LINK_MAX_AGE_SECONDS,
    )
    user = _user_model().objects.filter(id=data['uid']).first()
    if user is None or not constant_time_compare(data['h'], _deactivate_state_hash(user)):
        raise signing.BadSignature('stale deactivate token')
    return user.id


def account_deletion_message():
    days = settings.ACCOUNT_DELETION_GRACE_DAYS
    return (
        f'Your account is closed now and permanently deleted after {days} days. '
        f'Your upcoming rides and bookings have been cancelled. '
        f'Contact support within that time if you change your mind. Some records '
        f'(e.g. completed ride history) are kept for the periods in our privacy policy.'
    )


def send_delete_account_email(request, user):
    """Email `user` the signed link that closes their account. Raises if the
    email can't be sent."""
    token = make_deactivate_token(user)
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
        to=[user.mail_to],
        cc=[settings.ADMIN_EMAIL],
        reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_DEACTIVATION],
    )
    msg.attach_alternative(html_body, 'text/html')
    msg.send()


def close_account(user):
    """What the confirmed deletion link does. The caller checks is_active first."""
    user.is_active = False
    user.deactivated_at = timezone.now()
    user.save(update_fields=['is_active', 'deactivated_at'])
    # The privacy policy promises search logs go at once, not with the rest
    # of the account at the grace-period purge (audit P-14). It also takes the
    # user off drivers' Route Demand lists, which are built from searches.
    RideSearch.objects.filter(user=user).delete()
    end_upcoming_trips(user)


def end_upcoming_trips(user):
    """A closed account must not leave other people waiting on it: its
    rides not yet departed are cancelled and its bookings on them are
    cancelled, through the same steps (seats, chats, emails) as doing it
    by hand, so the others get the usual cancellation notices, which don't
    say the account was closed. Rides already under way are left to finish.
    Each one is tried on its own, so one failure doesn't stop the rest;
    search and booking also skip closed drivers' rides as a backstop."""
    from komute.services import bookings as booking_rules
    from komute.services import rides as ride_rules

    now = timezone.now()
    upcoming = dict(status__in=('posted', 'full'), depart_at__gt=now)

    for ride in Rides.objects.filter(driver=user, **upcoming):
        try:
            ride_rules.cancel_ride(ride)
        except Exception:
            logger.exception('Closing account %s: could not cancel ride %s', user.id, ride.id)

    bookings = (RideBooking.objects
                .filter(passenger=user, status__in=booking_rules.ACTIVE)
                .filter(**{f'ride__{k}': v for k, v in upcoming.items()})
                .select_related('ride__driver', 'passenger'))
    for booking in bookings:
        try:
            booking_rules.cancel_booking(booking)
        except booking_rules.BookingError:
            pass  # closed meanwhile (e.g. the driver rejected it)
        except Exception:
            logger.exception('Closing account %s: could not cancel booking %s', user.id, booking.id)

    RideInvite.objects.filter(
        passenger=user, status__in=(RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED),
    ).update(status=RideInvite.STATUS_DECLINED, responded_at=now)


# ──────────────────────────────────────────────────────────────────
# Download my data (audit P-9)
# ──────────────────────────────────────────────────────────────────

def build_data_export(user):
    """Everything Komüte holds about `user`, for "Download my data" (audit P-9).
    Other people appear only by the display name the app already shows. Left
    out: the password hash, staff notes, and reports made about the user (the
    policy promises reporters stay anonymous)."""
    other = display_name
    _iso = iso
    _ride_summary = ride_summary

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
            'pending_email': user.pending_email,
            'communication_email': user.contact_email,
            'communication_email_confirmed': user.contact_email_confirmed,
            'phone': user.phone, 'role': user.role,
            'date_of_birth': _iso(user.dob), 'gender': user.gender, 'bio': user.bio,
            'has_profile_photo': bool(user.avatar_url),
            'driver_verified': user.driver_verified, 'driver_verified_at': _iso(user.driver_verified_at),
            'licence_number': user.license_number, 'licence_province': user.license_province,
            'licence_expiry': _iso(user.license_expiry), 'face_match_score': user.face_match_score,
            'average_rating': user.avg_rating, 'total_rides': user.total_rides,
            'created_at': _iso(user.created_at), 'updated_at': _iso(user.updated_at),
        },
        # Google/Apple sign-in links (komute/social.py), with what we kept
        # from the provider.
        'sign_in_with': [
            {'provider': a.provider, 'account_id': a.uid, 'details': a.extra_data,
             'linked_at': _iso(a.date_joined), 'last_used_at': _iso(a.last_login)}
            for a in SocialAccount.objects.filter(user=user).order_by('date_joined')
        ],
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
