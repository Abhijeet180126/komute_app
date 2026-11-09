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


class BookingError(Exception):
    """A refused booking action, with the status the website's JSON used."""

    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


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
    if ride.status not in ('posted', 'full') or ride.depart_at <= timezone.now():
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

    existing = RideBooking.objects.filter(
        ride=ride,
        passenger=user,
        status__in=[RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED],
    ).first()

    if existing and existing.status in (RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
        raise BookingError('You have already requested this ride.', code='already_requested')

    if intent == "chat":
        # Pre-request negotiation: open a chat without reserving a seat or notifying the driver.
        if existing and existing.status == RideBooking.STATUS_DRAFT:
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

    updated = Rides.objects.filter(
        id=ride.id, seats_available__gte=seats
    ).update(seats_available=F('seats_available') - seats)
    if not updated:
        raise BookingError('Not enough seats available.', code='not_enough_seats')
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

    updated = RideBooking.objects.filter(id=booking.id).exclude(
        status__in=(RideBooking.STATUS_CANCELLED, RideBooking.STATUS_REJECTED, RideBooking.STATUS_COMPLETED),
    ).update(status=RideBooking.STATUS_CANCELLED, cancelled_at=cancelled_at)
    if not updated:
        raise BookingError("Booking is already cancelled.", code='already_closed')
    booking.status = RideBooking.STATUS_CANCELLED
    booking.cancelled_at = cancelled_at

    Rides.objects.filter(id=ride.id).update(
        seats_available=Least(F('seats_available') + booking.seats, ride.seats_total)
    )
    Rides.objects.filter(id=ride.id, status='full').update(status='posted')
    notify_booking_status(booking)
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

    updated = RideBooking.objects.filter(
        id=booking.id, status=RideBooking.STATUS_REQUESTED,
    ).update(status=RideBooking.STATUS_REJECTED, rejected_at=timezone.now())
    if not updated:
        raise BookingError(f'Cannot reject a booking that is already {booking.status}.', code='not_pending')
    booking.status = RideBooking.STATUS_REJECTED

    ride = booking.ride
    Rides.objects.filter(id=ride.id).update(
        seats_available=Least(F('seats_available') + booking.seats, ride.seats_total)
    )
    Rides.objects.filter(id=ride.id, status='full').update(status='posted')
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

    if booking.status not in (RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
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

    if booking.status not in (RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
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
