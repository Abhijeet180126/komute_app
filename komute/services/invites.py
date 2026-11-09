"""Ride invites: a driver invites a rider found through Route Demand (or
who has booked with them before); the rider accepts (a confirmed booking)
or declines (API module 3).

Moved from komute/views.py (send_invite_view, invite_detail_view,
accept_invite_view, decline_invite_view), which now call these. Errors are
InviteError with the website's message; the caller decides how to show it."""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db.models import F
from django.db.models.functions import Least
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from komute.models import EmailLog, RideBooking, RideInvite, Rides, RideSearch, UserBlock
from komute.notifications import notify_bookings_changed
from komute.services.booking_status import notify_booking_status
from komute.services.bookings import send_booking_confirmation_emails
from komute.services.common import DOB_MISSING_MSG, EMAIL_UNVERIFIED_MSG, display_name
from komute.services.geo import haversine_km
from komute.services.rides import GENDER_PREF_MSG, fmt_ride_local, gender_pref_allows
from komute.tasks import send_later

logger = logging.getLogger('komute.views')

INVITE_SALT = 'komute-invite-v1'
INVITE_DISCOVERY_WINDOW_DAYS = 7  # matches route demand's own window
ALREADY_RESPONDED_MSG = 'This invite has already been responded to.'
NOT_YOURS_MSG = 'This invite was not sent to your account.'
NOT_ELIGIBLE_MSG = 'This rider is not eligible to be invited to this ride.'


class InviteError(Exception):
    """A refused invite action. `status` is the HTTP status the website's
    JSON endpoint used (send); the pages show `message` instead."""

    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class InviteExpired(InviteError):
    def __init__(self):
        super().__init__('This invite has expired.', status=410, code='invite_expired')


# ──────────────────────────────────────────────────────────────────
# Tokens and email
# ──────────────────────────────────────────────────────────────────

def make_invite_token(invite_id):
    """Return a signed, URL-safe token encoding the invite ID.
    The token is verified and decoded by decode_invite_token.
    Signing uses Django's SECRET_KEY + a salt so changing either
    the ID or the salt (e.g. after a key rotation) invalidates it."""
    return signing.dumps(invite_id, salt=INVITE_SALT)


def decode_invite_token(token):
    """Decode and verify a token produced by make_invite_token.
    Returns the invite_id integer.
    Raises signing.SignatureExpired if older than settings.INVITE_EXPIRY_HOURS.
    Raises signing.BadSignature if tampered or invalid."""
    return signing.loads(
        token,
        salt=INVITE_SALT,
        max_age=settings.INVITE_EXPIRY_HOURS * 3600,
    )


def resolve_invite_token(token):
    """Decode token → invite_id, or return (None, error_string)."""
    try:
        invite_id = decode_invite_token(token)
        return invite_id, None
    except signing.SignatureExpired:
        return None, 'expired_token'
    except signing.BadSignature:
        return None, 'invalid_token'


def invite_is_expired(invite):
    return (timezone.now() - invite.sent_at).total_seconds() > settings.INVITE_EXPIRY_HOURS * 3600


def send_invite_email(request, invite):
    try:
        ride   = invite.ride
        driver = invite.driver
        rider  = invite.passenger

        token = make_invite_token(invite.id)
        invite_url  = request.build_absolute_uri(
            reverse('invite-detail', args=[token])
        )
        accept_url  = invite_url
        decline_url = invite_url

        ctx = {
            'rider_name':    (rider.full_name  or rider.email).title(),
            'driver_name':   display_name(driver),
            'origin':        ride.origin,
            'destination':   ride.destination,
            'depart_display': fmt_ride_local(ride),
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
            to=[rider.mail_to],
            reply_to=[settings.REPLY_TO_EMAIL_RIDE_INVITE],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_INVITE, rider)
    except Exception:
        logger.exception('_send_invite_email could not build or queue an email')


# ──────────────────────────────────────────────────────────────────
# Send
# ──────────────────────────────────────────────────────────────────

def rider_is_inviteable(driver, ride, rider):
    """A driver may only invite a rider they could legitimately have found
    through the app: someone who recently searched for a route matching this
    ride (same geo-matching used by route demand), or someone who has
    booked with this driver before. Blocks inviting via an arbitrary/
    enumerated user ID that the driver has no real connection to."""
    since = timezone.now() - timedelta(days=INVITE_DISCOVERY_WINDOW_DAYS)
    matched_search = any(
        haversine_km(ride.origin_lat, ride.origin_lng, s.origin_lat, s.origin_lng) <= settings.SEARCH_RADIUS_KM
        and haversine_km(ride.dest_lat, ride.dest_lng, s.dest_lat, s.dest_lng) <= settings.SEARCH_RADIUS_KM
        for s in RideSearch.objects.filter(user=rider, created_at__gte=since)
    )
    if matched_search:
        return True
    return RideBooking.objects.filter(passenger=rider, ride__driver=driver).exists()


def send_invite(request, driver, ride, rider_id):
    """Invites user `rider_id` to `ride` (the driver's own posted or full
    ride; callers apply the send-invite rate limits first). Returns the
    invite, or raises InviteError."""
    if not driver.driver_verification_current:
        raise InviteError('Please verify your identity on your profile page before inviting riders.',
                          status=403, code='driver_not_verified')

    # Closed accounts can't be invited (audit P-14): they stay in the
    # database until the 45-day purge, and a prior booking would still
    # make them inviteable below.
    rider = get_user_model().objects.filter(id=rider_id, is_active=True).first()

    if rider and rider == driver:
        raise InviteError('Cannot invite yourself.')

    # Identical response whether rider_id doesn't exist at all or exists but
    # isn't a legitimate invite target for this ride — otherwise the status
    # code/message becomes an oracle for enumerating real user IDs. The same
    # goes for a block either way (ISSUES.md A1): the driver isn't told.
    if (rider is None or UserBlock.objects.between(driver.id, rider.id)
            or not gender_pref_allows(ride, rider)
            or not rider_is_inviteable(driver, ride, rider)):
        raise InviteError(NOT_ELIGIBLE_MSG, code='not_eligible')

    if ride.seats_available <= 0:
        raise InviteError('Ride is full.', code='ride_full')

    already = RideInvite.objects.filter(
        ride=ride, passenger=rider,
        status__in=[RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED],
    ).exists()
    if already:
        raise InviteError('Invite already sent to this rider.', code='already_invited')

    invite = RideInvite.objects.create(driver=driver, passenger=rider, ride=ride)
    send_invite_email(request, invite)
    return invite


# ──────────────────────────────────────────────────────────────────
# Open, accept, decline (the invited rider; callers check it's them)
# ──────────────────────────────────────────────────────────────────

def open_invite(invite):
    """The rider looked at the invite: "sent" becomes "viewed", and an
    unanswered invite past its time becomes "expired". Returns whether it
    has expired."""
    expired = invite_is_expired(invite)

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
    return expired


def accept_invite(invite, user):
    """Accepting confirms a booking on the ride (one seat, or the seats of
    the rider's draft booking, whose chat it keeps). Returns the booking, or
    raises InviteError / InviteExpired."""
    if invite.status not in (RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED):
        raise InviteError(ALREADY_RESPONDED_MSG, code='already_responded')

    if invite_is_expired(invite):
        invite.status = RideInvite.STATUS_EXPIRED
        invite.save(update_fields=['status'])
        raise InviteExpired()

    ride = invite.ride

    if (ride.status in ['cancelled', 'completed'] or ride.depart_at <= timezone.now()
            or UserBlock.objects.between(ride.driver_id, user.id)):
        raise InviteError('This ride is no longer available.', code='ride_unavailable')

    if not user.email_verified:
        raise InviteError(EMAIL_UNVERIFIED_MSG, status=403, code='email_unverified')

    if not user.dob:
        raise InviteError(DOB_MISSING_MSG, status=403, code='dob_missing')

    # The rules a normal booking follows (audit L-7/L-8): the gender
    # preference, and one active booking per rider per ride.
    if not gender_pref_allows(ride, user):
        raise InviteError(GENDER_PREF_MSG, status=403, code='gender_preference')
    existing = RideBooking.objects.filter(
        ride=ride,
        passenger=user,
        status__in=[RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED],
    ).first()
    if existing and existing.status != RideBooking.STATUS_DRAFT:
        raise InviteError('You already have a booking on this ride.', code='already_booked')
    seats = existing.seats if existing else 1

    # Accepting an invite confirms a booking, so the driver's verification
    # must be current (same rule as accepting a booking request).
    if not ride.driver.driver_verification_current:
        raise InviteError("This invite can't be accepted right now because the driver needs to re-verify "
                          "their identity. Please try again later.", code='driver_not_verified')

    # Atomically claim the invite first so a replayed/concurrent accept
    # (double-click, resubmit) can't pass this check twice.
    prior_status = invite.status
    claimed = RideInvite.objects.filter(
        id=invite.id, status__in=(RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED),
    ).update(status=RideInvite.STATUS_ACCEPTED, responded_at=timezone.now())
    if not claimed:
        raise InviteError(ALREADY_RESPONDED_MSG, code='already_responded')

    updated = Rides.objects.filter(
        id=ride.id, seats_available__gte=seats
    ).update(seats_available=F('seats_available') - seats)
    if not updated:
        # Couldn't complete the acceptance — release the claim so the invite
        # stays actionable instead of being stuck "accepted" with no booking.
        RideInvite.objects.filter(id=invite.id).update(status=prior_status, responded_at=None)
        raise InviteError('Sorry, this ride is now full.' if seats == 1
                          else 'Sorry, this ride no longer has enough seats.', code='ride_full')
    Rides.objects.filter(id=ride.id, seats_available=0).update(status='full')

    if existing:
        # Confirm the rider's draft in place, as booking promotes it: keeps
        # its chat history, seat count and any price agreed there.
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
            raise InviteError('Your booking on this ride changed meanwhile. Please reload and try again.',
                              status=409, code='booking_changed')
        booking = existing
        booking.status       = RideBooking.STATUS_CONFIRMED
        booking.confirmed_at = confirmed_at
        notify_booking_status(booking)
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

    send_booking_confirmation_emails(booking)
    invite.status = RideInvite.STATUS_ACCEPTED
    return booking


def decline_invite(invite):
    """Raises InviteError if it was already answered."""
    updated = RideInvite.objects.filter(
        id=invite.id, status__in=(RideInvite.STATUS_SENT, RideInvite.STATUS_VIEWED),
    ).update(status=RideInvite.STATUS_DECLINED, responded_at=timezone.now())
    if not updated:
        raise InviteError(ALREADY_RESPONDED_MSG, code='already_responded')
    invite.status = RideInvite.STATUS_DECLINED
