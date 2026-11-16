"""Bookings and price offers: requesting a seat (or opening a pre-request
chat), cancelling, the driver accepting or rejecting, price negotiation,
the booking emails, and the inbox conversation list (API module 4).

Moved from komute/views.py (create_ride_booking, cancel_booking_view,
accept_booking_view, reject_booking_view, create/respond_price_offer_view,
driver/rider_inbox_view), which call these and keep the old names as
aliases. Refusals are BookingError with the website's message and status."""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Count, F, Q
from django.db.models.functions import Least
from django.template.loader import render_to_string
from django.utils import timezone

from komute.models import EmailLog, Message, PriceOffer, RideBooking, Rides, RideStop, UserBlock
from komute.notifications import notify_bookings_changed
from komute.services.booking_status import BLOCKED_CHAT_MESSAGE, notify_booking_status
from komute.services.common import DOB_MISSING_MSG, EMAIL_UNVERIFIED_MSG, display_name
from komute.services.rides import GENDER_PREF_MSG, fmt_ride_local, gender_pref_allows
from komute.tasks import send_later

logger = logging.getLogger('komute.views')

ACTIVE = (RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED)
MAX_OFFER_CENTS = 100_000
# The chat's message (komute.consumers.ChatConsumer.ERROR_MESSAGES['nego_locked']).
NEGO_LOCKED_MSG = 'Price negotiation opens once the driver accepts the booking request.'


class BookingError(Exception):
    """A refused booking action, with the status the website's JSON used."""

    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# ──────────────────────────────────────────────────────────────────
# Seats. Only a requested or confirmed booking holds seats; a draft
# ("Chat with driver") never does. Every booking change that takes or
# gives back seats runs in a transaction that first locks the ride row
# with lock_ride(), so changes to one ride's bookings happen one at a
# time. (The regression suite runs on SQLite, which can't show these
# races; production is Postgres.)
# ──────────────────────────────────────────────────────────────────

def lock_ride(ride_id):
    """Lock the ride row until the surrounding transaction ends."""
    return Rides.objects.select_for_update().filter(id=ride_id).first()


def reserve_seats(ride_id, seats):
    """Take `seats` from a ride still open for booking. False if it has
    fewer free, or was cancelled, completed or has left meanwhile, or its
    driver's account is closed."""
    taken = Rides.objects.filter(
        id=ride_id, status__in=('posted', 'full'), depart_at__gt=timezone.now(),
        driver__is_active=True, seats_available__gte=seats,
    ).update(seats_available=F('seats_available') - seats)
    if taken:
        Rides.objects.filter(id=ride_id, seats_available=0).update(status='full')
    return bool(taken)


def release_seats(ride_id, seats):
    """Give back `seats` held by a booking that is ending."""
    Rides.objects.filter(id=ride_id).update(
        seats_available=Least(F('seats_available') + seats, F('seats_total'))
    )
    Rides.objects.filter(id=ride_id, status='full').update(status='posted')


# ──────────────────────────────────────────────────────────────────
# Emails
# ──────────────────────────────────────────────────────────────────

def send_booking_confirmation_emails(booking):
    try:
        ride    = booking.ride
        rider   = booking.passenger
        driver  = ride.driver
        vehicle = ride.vehicle

        depart_display = fmt_ride_local(ride)
        amount         = f"${booking.agreed_price_cents / 100:.2f}"
        booking_ref    = f"KMT-{ride.depart_at.year}-{ride.id:04d}-B{booking.id}"
        vehicle_display = f"{vehicle.year} {vehicle.make} {vehicle.model}" if vehicle else "N/A"
        vehicle_plate   = vehicle.plate if vehicle else "N/A"
        seats_remaining = ride.seats_available

        rider_ctx = {
            'rider_name':      display_name(rider),
            'driver_name':     display_name(driver),
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
            'driver_name':     display_name(driver),
            'rider_name':      display_name(rider),
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
            to=[rider.mail_to],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_CONFIRMATION],
        )
        rider_msg.attach_alternative(rider_html, 'text/html')
        send_later(rider_msg, EmailLog.KIND_BOOKING_CONFIRMED, rider)

        driver_msg = EmailMultiAlternatives(
            subject=f"You accepted {rider_ctx['rider_name']}'s ride request 🚗",
            body=f"Hi {driver_ctx['driver_name']}, you accepted {rider_ctx['rider_name']}'s request for {booking.seats} seat(s). Ref: {booking_ref}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[driver.mail_to],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_CONFIRMATION],
        )
        driver_msg.attach_alternative(driver_html, 'text/html')
        send_later(driver_msg, EmailLog.KIND_BOOKING_CONFIRMED, driver)
    except Exception:
        logger.exception('_send_booking_confirmation_emails could not build or queue an email')


def send_booking_cancelled_emails(booking):
    """Passenger cancelled their booking — notify the driver."""
    try:
        ride   = booking.ride
        rider  = booking.passenger
        driver = ride.driver

        depart_display = fmt_ride_local(ride)
        booking_ref    = f"KMT-{ride.depart_at.year}-{ride.id:04d}-B{booking.id}"

        ctx = {
            'driver_name':    display_name(driver),
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
            to=[driver.mail_to],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_CANCELLED],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_BOOKING_CANCELLED, driver)
    except Exception:
        logger.exception('_send_booking_cancelled_emails could not build or queue an email')


def send_booking_request_email(booking):
    """Rider requested a booking — notify the driver."""
    try:
        ride   = booking.ride
        rider  = booking.passenger
        driver = ride.driver

        depart_display = fmt_ride_local(ride)
        booking_ref    = f"KMT-{ride.depart_at.year}-{ride.id:04d}-B{booking.id}"

        ctx = {
            'driver_name':    display_name(driver),
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
            to=[driver.mail_to],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_REQUEST],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_BOOKING_REQUEST, driver)
    except Exception:
        logger.exception('_send_booking_request_email could not build or queue an email')


def send_booking_rejected_email(booking):
    """Driver rejected a booking request — notify the rider."""
    try:
        ride   = booking.ride
        rider  = booking.passenger
        driver = ride.driver

        depart_display = fmt_ride_local(ride)
        booking_ref    = f"KMT-{ride.depart_at.year}-{ride.id:04d}-B{booking.id}"

        ctx = {
            'rider_name':     (rider.full_name  or rider.email).title(),
            'driver_name':    display_name(driver),
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
            to=[rider.mail_to],
            reply_to=[settings.REPLY_TO_EMAIL_BOOKING_REJECTED],
        )
        msg.attach_alternative(html, 'text/html')
        send_later(msg, EmailLog.KIND_BOOKING_REJECTED, rider)
    except Exception:
        logger.exception('_send_booking_rejected_email could not build or queue an email')


# ──────────────────────────────────────────────────────────────────
# Requesting a seat
# ──────────────────────────────────────────────────────────────────

def book_ride(ride, user, *, intent, seats_raw, pickup_stop_id=None, dropoff_stop_id=None):
    """A rider (not the driver, not blocked; callers check those) asks for
    seats. intent "chat" opens or reuses a pre-request draft (no seat held,
    driver not told); "book" requests the seats, promoting a draft in place.
    Returns the booking, or raises BookingError."""
    # Both checks cover "Chat with driver" too: a draft can later be
    # confirmed in chat or used to leave a review. Booking emails go to
    # the rider's address, so it must be verified (audit S-10).
    if not user.email_verified:
        raise BookingError(EMAIL_UNVERIFIED_MSG, status=403, code='email_unverified')
    # Accounts from before DOB was asked at signup (audit L-5).
    if not user.dob:
        raise BookingError(DOB_MISSING_MSG, status=403, code='dob_missing')
    if ride.status not in ('posted', 'full') or ride.depart_at <= timezone.now() or not ride.driver.is_active:
        raise BookingError('This ride is no longer available.', code='ride_unavailable')
    if not gender_pref_allows(ride, user):
        raise BookingError(GENDER_PREF_MSG, status=403, code='gender_preference')
    try:
        seats = int(seats_raw)
    except (TypeError, ValueError):
        raise BookingError('Invalid seat count.', code='invalid_seats')
    if seats < 1 or seats > 8:
        raise BookingError('Seats must be between 1 and 8.', code='invalid_seats')

    pickup_stop  = RideStop.objects.filter(id=pickup_stop_id,  ride=ride).first() if pickup_stop_id  else None
    dropoff_stop = RideStop.objects.filter(id=dropoff_stop_id, ride=ride).first() if dropoff_stop_id else None

    # The ride row is locked for the rest of this, so two clicks (or a
    # click and a chat price-accept / invite accept / cancel) on the same
    # ride run one after the other: a double click can't make two requests,
    # and a draft can't be promoted while it is being confirmed or cancelled.
    with transaction.atomic():
        locked = lock_ride(ride.id)
        if locked is None or locked.status not in ('posted', 'full'):
            raise BookingError('This ride is no longer available.', code='ride_unavailable')
        existing = RideBooking.objects.select_for_update().filter(
            ride=ride, passenger=user, status__in=ACTIVE,
        ).first()

        if existing and existing.status in (RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
            raise BookingError('You have already requested this ride.', code='already_requested')

        if intent == "chat":
            # Pre-request negotiation: open a chat without reserving a seat or notifying the driver.
            if existing:
                return existing
            return RideBooking.objects.create(
                ride=ride,
                passenger=user,
                pickup_stop=pickup_stop,
                dropoff_stop=dropoff_stop,
                seats=seats,
                agreed_price_cents=ride.price_cents * seats + settings.SERVICE_FEE_CENTS,
                status=RideBooking.STATUS_DRAFT,
            )

        if not reserve_seats(ride.id, seats):
            raise BookingError('Not enough seats available.', code='not_enough_seats')

        if existing:
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
                passenger=user,
                pickup_stop=pickup_stop,
                dropoff_stop=dropoff_stop,
                seats=seats,
                agreed_price_cents=ride.price_cents * seats + settings.SERVICE_FEE_CENTS,
                status=RideBooking.STATUS_REQUESTED,
                confirmed_at=None,
            )

    notify_bookings_changed([ride.driver_id], 'You have a new booking request.')
    send_booking_request_email(booking)
    return booking


# ──────────────────────────────────────────────────────────────────
# Cancel (rider), accept / reject (driver)
# ──────────────────────────────────────────────────────────────────

def cancel_booking(booking):
    """The rider cancels (callers check it's theirs). The seats go back."""
    if booking.status in (RideBooking.STATUS_CANCELLED, RideBooking.STATUS_REJECTED, RideBooking.STATUS_COMPLETED):
        raise BookingError(f"Booking is already {booking.status}.", code='already_closed')

    if booking.ride.status in ('completed', 'cancelled'):
        raise BookingError(f"Cannot cancel — ride is already {booking.ride.status}.", code='ride_closed')

    if booking.ride.depart_at <= timezone.now():
        raise BookingError("Cannot cancel after the ride has departed.", code='departed')

    ride = booking.ride
    cancelled_at = timezone.now()

    # Only a requested or confirmed booking holds seats. A draft ("Chat with
    # driver") never reserved any, so cancelling one must not hand seats back
    # (that let a full ride be oversold), and the driver, who never saw a
    # request, gets no notice or email. The status and seats are read under
    # the ride lock, not taken from the caller's copy, so a draft promoted
    # to a request at the same moment is cancelled as a request, with its
    # current seats released exactly once.
    with transaction.atomic():
        lock_ride(ride.id)
        current = (
            RideBooking.objects.select_for_update()
            .filter(id=booking.id).values('status', 'seats').first()
        )
        if current is None or current['status'] not in ACTIVE:
            raise BookingError("Booking is already cancelled.", code='already_closed')
        held = current['status'] != RideBooking.STATUS_DRAFT
        RideBooking.objects.filter(id=booking.id).update(
            status=RideBooking.STATUS_CANCELLED, cancelled_at=cancelled_at,
        )
        if held:
            release_seats(ride.id, current['seats'])
    booking.seats = current['seats']
    booking.status = RideBooking.STATUS_CANCELLED
    booking.cancelled_at = cancelled_at

    # Locks any open chat page, the driver's included (they may have replied).
    notify_booking_status(booking)
    if held:
        notify_bookings_changed([ride.driver_id], 'A passenger cancelled their booking.')
        send_booking_cancelled_emails(booking)


def accept_booking(booking):
    """The driver accepts a request (callers check it's their ride)."""
    if booking.status != RideBooking.STATUS_REQUESTED:
        raise BookingError(f'Cannot accept a booking that is already {booking.status}.', code='not_pending')

    if not booking.ride.driver.driver_verification_current:
        raise BookingError('Please verify your identity on your profile page before accepting bookings.',
                           status=403, code='driver_not_verified')

    # A request sent before a block stays pending; the driver can still
    # reject it and the rider can cancel it, but it can't be confirmed.
    if UserBlock.objects.between(booking.ride.driver_id, booking.passenger_id):
        raise BookingError("You can't accept this booking request.", status=403, code='blocked')

    # Conditional, like reject and cancel: a cancel that lands between the
    # check above and this write must not be overwritten to confirmed after
    # its seat was already released.
    confirmed_at = timezone.now()
    updated = RideBooking.objects.filter(
        id=booking.id, status=RideBooking.STATUS_REQUESTED,
    ).update(status=RideBooking.STATUS_CONFIRMED, confirmed_at=confirmed_at)
    if not updated:
        raise BookingError('This booking request is no longer pending.', code='not_pending')
    booking.status = RideBooking.STATUS_CONFIRMED
    booking.confirmed_at = confirmed_at
    notify_booking_status(booking)
    notify_bookings_changed([booking.passenger_id], 'Your booking request was accepted.')

    send_booking_confirmation_emails(booking)


def reject_booking(booking):
    """The driver turns a request down (callers check it's their ride)."""
    if booking.status != RideBooking.STATUS_REQUESTED:
        raise BookingError(f'Cannot reject a booking that is already {booking.status}.', code='not_pending')

    # A requested booking holds its seats and its seat count no longer
    # changes, so the conditional write alone makes the release happen once.
    with transaction.atomic():
        updated = RideBooking.objects.filter(
            id=booking.id, status=RideBooking.STATUS_REQUESTED,
        ).update(status=RideBooking.STATUS_REJECTED, rejected_at=timezone.now())
        if not updated:
            raise BookingError(f'Cannot reject a booking that is already {booking.status}.', code='not_pending')
        release_seats(booking.ride_id, booking.seats)
    booking.status = RideBooking.STATUS_REJECTED
    notify_booking_status(booking)
    notify_bookings_changed([booking.passenger_id], 'Your booking request was declined.')

    send_booking_rejected_email(booking)


# ──────────────────────────────────────────────────────────────────
# Price offers (per seat)
# ──────────────────────────────────────────────────────────────────

def _cents(raw, invalid_msg):
    try:
        return int(round(float(raw)))
    except (ValueError, TypeError, OverflowError):
        raise BookingError(invalid_msg, code='invalid_price')


def create_offer(booking, user, price_raw):
    """`user` (the rider or the driver) proposes a price per seat; their
    own earlier pending offer is withdrawn. Returns the offer."""
    if user not in (booking.ride.driver, booking.passenger):
        raise BookingError('Forbidden.', status=403, code='forbidden')

    # As in the chat: locked while the request waits for the driver.
    if booking.status == RideBooking.STATUS_REQUESTED:
        raise BookingError(NEGO_LOCKED_MSG, code='nego_locked')
    if booking.status != RideBooking.STATUS_CONFIRMED:
        raise BookingError('Negotiation is closed for this booking.', code='negotiation_closed')

    if UserBlock.objects.between(booking.ride.driver_id, booking.passenger_id):
        raise BookingError(BLOCKED_CHAT_MESSAGE, status=403, code='blocked')

    price_cents = _cents(price_raw, 'Invalid price.')

    if price_cents <= 0:
        raise BookingError('Price must be greater than 0.', code='invalid_price')

    if price_cents > MAX_OFFER_CENTS:
        raise BookingError('Offer cannot exceed $1,000.', code='invalid_price')

    # Expire any pending offers from the same user
    PriceOffer.objects.filter(
        booking=booking,
        proposed_by=user,
        status=PriceOffer.STATUS_PENDING,
    ).update(status=PriceOffer.STATUS_DECLINED, responded_at=timezone.now())

    return PriceOffer.objects.create(
        booking=booking,
        proposed_by=user,
        price_cents=price_cents,
    )


def respond_offer(offer, user, action, price_raw=None):
    """The other party accepts, declines or counters an offer. Accepting
    sets the booking's agreed price (offer × seats + service fee). Returns
    the offer it acted on, or for "counter" the new offer."""
    booking = offer.booking

    if user == offer.proposed_by:
        raise BookingError('You cannot respond to your own offer.', status=403, code='own_offer')

    if user not in (booking.ride.driver, booking.passenger):
        raise BookingError('Forbidden.', status=403, code='forbidden')

    # As in the chat: locked while the request waits for the driver.
    if booking.status == RideBooking.STATUS_REQUESTED:
        raise BookingError(NEGO_LOCKED_MSG, code='nego_locked')
    if booking.status != RideBooking.STATUS_CONFIRMED:
        raise BookingError(f'This booking is no longer active ({booking.status}).', code='negotiation_closed')

    if UserBlock.objects.between(booking.ride.driver_id, booking.passenger_id):
        raise BookingError(BLOCKED_CHAT_MESSAGE, status=403, code='blocked')

    if offer.status != PriceOffer.STATUS_PENDING:
        raise BookingError('This offer is no longer pending.', code='not_pending')

    if action == 'accept':
        offer.status = PriceOffer.STATUS_ACCEPTED
        offer.responded_at = timezone.now()
        offer.save(update_fields=['status', 'responded_at'])

        booking.agreed_price_cents = offer.price_cents * booking.seats + settings.SERVICE_FEE_CENTS
        booking.save(update_fields=['agreed_price_cents'])
        return offer

    if action == 'decline':
        offer.status = PriceOffer.STATUS_DECLINED
        offer.responded_at = timezone.now()
        offer.save(update_fields=['status', 'responded_at'])
        return offer

    if action == 'counter':
        counter_cents = _cents(price_raw if price_raw is not None else 0, 'Invalid counter price.')

        if counter_cents <= 0:
            raise BookingError('Counter price must be greater than 0.', code='invalid_price')

        if counter_cents > MAX_OFFER_CENTS:
            raise BookingError('Counter offer cannot exceed $1,000.', code='invalid_price')

        offer.status = PriceOffer.STATUS_COUNTERED
        offer.responded_at = timezone.now()
        offer.save(update_fields=['status', 'responded_at'])

        return PriceOffer.objects.create(
            booking=booking,
            proposed_by=user,
            price_cents=counter_cents,
        )

    raise BookingError('Invalid action. Use accept, decline, or counter.', code='invalid_action')


# ──────────────────────────────────────────────────────────────────
# Inbox: one conversation per ride and person
# ──────────────────────────────────────────────────────────────────

def inbox(user, as_driver):
    """The inbox rows for `user` as a driver (requests and chats on their
    rides) or as a rider (their bookings). Each row: booking, ride, other
    (the other person), last_msg, unread_count, latest_offer, nego_status
    ('agreed' / 'pending' / 'none'), nego_cents, nego_by_other (the pending
    offer came from the other person), status ('completed' / 'active' /
    'upcoming'), blocked_by_me. In the website's order: requests first (for
    drivers), then unread."""
    if as_driver:
        # Draft bookings (pre-request chats — "Chat with driver" without
        # "Request to book") are included too: once the rider has actually
        # sent a message or price offer, the driver needs to see and respond
        # to it, even though no seat has been reserved yet. Untouched, empty
        # drafts are filtered out below so opening a draft chat alone still
        # doesn't surface anything.
        bookings_qs = (
            RideBooking.objects
            .filter(ride__driver=user)
            .exclude(status__in=['cancelled', 'rejected'])
            .select_related('ride', 'passenger')
            .order_by('-booked_at')
        )
    else:
        bookings_qs = (
            RideBooking.objects
            .filter(passenger=user)
            .exclude(status__in=['cancelled', 'rejected', 'draft'])
            .select_related('ride', 'ride__driver')
            .order_by('-booked_at')
        )

    # Deduplicate: one conversation per (ride, other person) — keep the latest booking
    seen = {}
    for b in bookings_qs:
        key = (b.ride_id, b.passenger_id if as_driver else b.ride.driver_id)
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

    # 1 query: unread count per (ride_id, other person)
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

    my_blocks = set(UserBlock.objects.filter(blocker=user).values_list('blocked_id', flat=True))

    rows = []
    for index, booking in enumerate(unique_bookings):
        ride = booking.ride
        other = booking.passenger if as_driver else ride.driver

        last_msg     = last_msg_map.get((ride.id, other.id))
        unread_count = unread_map.get((ride.id, other.id), 0)
        latest_offer = latest_offer_map.get(booking.id)

        if as_driver and booking.status == RideBooking.STATUS_DRAFT and last_msg is None and latest_offer is None:
            continue

        nego_cents = None
        nego_by_other = False
        if booking.status == RideBooking.STATUS_CONFIRMED and booking.agreed_price_cents:
            nego_status = 'agreed'
            nego_cents = booking.agreed_price_cents
        elif latest_offer and latest_offer.status == PriceOffer.STATUS_PENDING:
            nego_status = 'pending'
            nego_cents = latest_offer.price_cents
            nego_by_other = latest_offer.proposed_by_id == other.id
        else:
            nego_status = 'none'

        if ride.status == 'completed' or booking.status == RideBooking.STATUS_COMPLETED:
            ui_status = 'completed'
        elif booking.status == RideBooking.STATUS_CONFIRMED:
            ui_status = 'active'
        else:
            ui_status = 'upcoming'

        rows.append({
            'booking': booking, 'ride': ride, 'other': other, 'last_msg': last_msg,
            'unread_count': unread_count, 'latest_offer': latest_offer, 'nego_status': nego_status,
            'nego_cents': nego_cents, 'nego_by_other': nego_by_other, 'status': ui_status,
            'blocked_by_me': other.id in my_blocks,
            # Position before empty drafts are skipped (the page's avatar colour).
            'index': index,
        })

    if as_driver:
        rows.sort(key=lambda r: (
            0 if r['booking'].status == RideBooking.STATUS_REQUESTED else 1,
            0 if r['unread_count'] > 0 else 1,
        ))
    else:
        rows.sort(key=lambda r: not r['unread_count'] > 0)
    return rows


# ──────────────────────────────────────────────────────────────────
# Price negotiation as the chat page does it (the socket and the API).
# A counter is simply a new offer from the other person. Moved from
# komute/consumers.py. Errors are codes: ChatConsumer.ERROR_MESSAGES.
# ──────────────────────────────────────────────────────────────────

def chat_offer(user, booking_id, price):
    """(offer, None) or (None, error code). `price` is dollars per seat."""
    booking = RideBooking.objects.get(id=booking_id)
    if booking.status == RideBooking.STATUS_REQUESTED:
        # The page disables negotiation while the driver hasn't answered
        # the request; enforce it here too, not just in the browser.
        return None, 'nego_locked'
    if booking.status not in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_CONFIRMED):
        return None, 'booking_inactive'
    # 1 cent to $1,000 a seat.
    try:
        price_cents = int(round(float(price) * 100))
    except (TypeError, ValueError, OverflowError):
        return None, 'bad_price'
    if not 0 < price_cents <= 100_000:
        return None, 'bad_price'
    PriceOffer.objects.filter(
        booking=booking,
        proposed_by=user,
        status=PriceOffer.STATUS_PENDING,
    ).update(status=PriceOffer.STATUS_DECLINED, responded_at=timezone.now())
    return PriceOffer.objects.create(
        booking=booking,
        proposed_by=user,
        price_cents=price_cents,
    ), None


def chat_accept(user, booking_id, offer_id, price):
    """Accepts the other person's offer. On a pre-request chat that IS the
    booking: a seat is reserved and it is confirmed. Returns ('ok', agreed
    dollars per seat) or (error code, None)."""
    booking = RideBooking.objects.select_related('ride__driver', 'passenger').filter(id=booking_id).first()
    if booking is not None and booking.status == RideBooking.STATUS_REQUESTED:
        # Agreeing a price confirms the booking, so while the request is
        # waiting for the driver this would bypass their accept/reject.
        return 'nego_locked', None
    if booking is None or booking.status not in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_CONFIRMED):
        return 'booking_inactive', None
    # Cancelling a ride leaves drafts in place, so a draft can outlive its
    # ride; agreeing a price must not confirm a seat on it.
    if (booking.ride.status not in ('posted', 'full') or booking.ride.depart_at <= timezone.now()
            or not booking.ride.driver.is_active or not booking.passenger.is_active):
        return 'booking_inactive', None

    # Prefer the specific offer the client had on screen — matching by price
    # alone is ambiguous once a proposer has superseded their own pending
    # offer with a newer one (same price could belong to a stale offer).
    if offer_id is not None:
        # The page sends the number it was given; anything else is stale
        # (it used to raise and drop the socket).
        if isinstance(offer_id, bool) or not isinstance(offer_id, int):
            return 'offer_stale', None
        offer = PriceOffer.objects.filter(id=offer_id, booking_id=booking_id).first()
    else:
        try:
            price_cents = int(round(float(price) * 100))
        except (TypeError, ValueError, OverflowError):
            return 'offer_stale', None
        offer = (
            PriceOffer.objects
            .filter(booking_id=booking_id, price_cents=price_cents, status=PriceOffer.STATUS_PENDING)
            .exclude(proposed_by=user)
            .order_by('-created_at')
            .first()
        )
    if offer is not None and offer.proposed_by_id == user.id:
        # Only the other side can accept an offer; otherwise a rider could
        # offer a price and confirm it themselves.
        return 'own_offer', None
    if offer is None or offer.status != PriceOffer.STATUS_PENDING:
        return 'offer_stale', None

    agreed_price_cents = offer.price_cents * booking.seats + settings.SERVICE_FEE_CENTS
    was_unconfirmed = booking.status in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED)
    # Agreeing a price confirms the booking, which a driver whose identity
    # verification has lapsed may not do (same rule as accepting a request).
    if was_unconfirmed and not booking.ride.driver.driver_verification_current:
        return 'driver_unverified', None

    with transaction.atomic():
        # Re-read under the ride lock: a double click, the other person
        # accepting too, "Request to book" or a cancel may have changed the
        # booking since it was loaded above, and acting on the old copy
        # reserved its seats twice or confirmed a cancelled booking.
        lock_ride(booking.ride_id)
        status_now = RideBooking.objects.filter(id=booking_id).values_list('status', flat=True).first()
        if status_now != booking.status:
            return ('nego_locked' if status_now == RideBooking.STATUS_REQUESTED else
                    'booking_inactive' if status_now not in ACTIVE else 'offer_stale'), None
        responded = PriceOffer.objects.filter(id=offer.id, status=PriceOffer.STATUS_PENDING).update(
            status=PriceOffer.STATUS_ACCEPTED, responded_at=timezone.now(),
        )
        if not responded:
            return 'offer_stale', None

        if booking.status == RideBooking.STATUS_DRAFT:
            # A pre-request chat ("Chat with driver") never went through
            # "Request to book", so no seat was ever reserved — agreeing on
            # a price here IS the booking, so reserve the seat now.
            if not reserve_seats(booking.ride_id, booking.seats):
                transaction.set_rollback(True)  # the offer stays pending
                return 'no_seats', None
            booking.ride.seats_available -= booking.seats

        update_fields = {'agreed_price_cents': agreed_price_cents}
        if was_unconfirmed:
            update_fields['status'] = RideBooking.STATUS_CONFIRMED
            update_fields['confirmed_at'] = timezone.now()
        RideBooking.objects.filter(id=booking_id).update(**update_fields)
        offer.status = PriceOffer.STATUS_ACCEPTED

    if was_unconfirmed:
        booking.agreed_price_cents = agreed_price_cents
        booking.status = RideBooking.STATUS_CONFIRMED
        send_booking_confirmation_emails(booking)

    return 'ok', offer.price_cents / 100
