"""Rides: posting, editing, cancelling, searching, and the completed-ride
summary (API module 3).

Moved from komute/views.py (post_ride_view, edit_ride_view, cancel_ride_view,
search_rides_view, ride_summary_view), which call these and keep the old
helper names as aliases. Each check returns the website's own message."""
import logging
import math
import uuid
from datetime import date, datetime, timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import ExtractHour
from django.template.loader import render_to_string
from django.utils import timezone

from komute.models import EmailLog, RideBooking, RideSearch, Rides, RideStop, UserBlock
from komute.notifications import notify_bookings_changed
from komute.services.booking_status import notify_booking_status
from komute.services.common import EMAIL_UNVERIFIED_MSG, display_name
from komute.services.geo import (
    geo_box_q, haversine_km, in_us_canada, local_day_bounds, max_ride_date, service_area_error,
)
from komute.service_area import in_quebec
from komute.tasks import send_later

logger = logging.getLogger('komute.views')

# Bounds on numbers posted by forms and scripts (audit L-10): without them
# "inf" or a huge value raised OverflowError (a 500) in float/timedelta maths
# or overflowed the database's integer columns.
MAX_PRICE_CENTS = 100_000                 # $1,000 a seat, the price-offer cap too
MAX_DURATION_SECONDS = 7 * 24 * 3600      # longer than any US/Canada drive
MAX_TZ_OFFSET_MINUTES = 14 * 60           # real UTC offsets run -12:00..+14:00
MIN_SEATS, MAX_SEATS = 1, 8

# Ride preference tags, in the order post-ride.html shows them. Edit Ride
# uses this list and search filters match these exact names, so keep all
# three in step (RidePreferenceOptionsTests checks post-ride.html).
RIDE_PREFERENCE_OPTIONS = [
    'Music OK', 'Chatty Driver', 'Quiet Ride', 'Pet Friendly', 'No Smoking',
    'Luggage OK', 'Verified Only', 'On-Time Driver', 'Winter Tires',
    'Flexible with Time',
]

GENDER_PREFS = ("any", "women_only", "men_only")
GENDER_PREF_MSG = "This ride is limited to passengers of a different gender."
PICK_FROM_SUGGESTIONS_MSG = "Please pick a location from the suggestions."
# Different wording on the two pages, kept as it was.
POST_TOO_FAR_MSG = "Rides can only be posted up to 4 months in advance."
EDIT_TOO_FAR_MSG = "Rides can only be scheduled up to 4 months in advance."
POST_PRICE_ZERO_MSG = "Price per seat must be greater than 0."
EDIT_PRICE_ZERO_MSG = "Price must be greater than 0."
ACTIVE_STATUSES = (RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED)


def parse_dollars_to_cents(value):
    """Dollar string → whole cents. Raises ValueError for text, NaN or
    infinity; the caller checks the range."""
    amount = float(value)
    if not math.isfinite(amount):
        raise ValueError('price must be a finite number')
    return int(round(amount * 100))


def parse_duration_seconds(value):
    """The route duration a page script posts, or 0 (unknown) when it's
    missing, malformed or out of range."""
    try:
        seconds = int(value or 0)
    except (ValueError, TypeError):
        return 0
    return seconds if 0 < seconds <= MAX_DURATION_SECONDS else 0


def fmt_ride_local(ride):
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


def local_dt(ride, dt):
    """Convert a UTC datetime to the ride's stored timezone. Returns naive UTC on failure."""
    if ride.tz and dt:
        try:
            from zoneinfo import ZoneInfo
            return dt.astimezone(ZoneInfo(ride.tz))
        except Exception:
            pass
    return dt


def gender_pref_allows(ride, user):
    """Whether `user` may join `ride` under its passenger gender preference.
    Same rule as the search filter in search_rides, which only hides the
    ride; booking and invites check it here (audit L-8)."""
    if ride.passenger_gender_pref == 'women_only':
        return user.gender == 'female'
    if ride.passenger_gender_pref == 'men_only':
        return user.gender == 'male'
    return True


# ──────────────────────────────────────────────────────────────────
# Checks shared by Post Ride, Edit Ride and the API
# ──────────────────────────────────────────────────────────────────

def post_ride_block_reason(user):
    """Why `user` can't post a ride right now, or None."""
    if user.role != "driver":
        return "Only drivers can post rides."
    # Ride emails go to this address (audit S-10).
    if not user.email_verified:
        return EMAIL_UNVERIFIED_MSG
    if not user.dob:
        return "Please add your date of birth in your profile before posting a ride."
    if not user.driver_verification_current:
        if user.driver_verified:
            return "Your identity verification has expired. Please re-verify to post a ride."
        return "You must be verified to post a ride."
    if not user.vehicles.filter(vehicle_verified=True).exists():
        return "You need at least one verified vehicle to post a ride."
    return None


def edit_block_reason(ride):
    """Why `ride` can't be edited, or None."""
    if ride.status != 'posted':
        return "Cannot edit a ride that is cancelled or completed."
    if ride.bookings.filter(status__in=ACTIVE_STATUSES).exists():
        return "Cannot edit a ride that has active bookings."
    return None


def endpoint_error(name, lat, lng, kind, required_msg):
    """The start or end of a ride. 0,0 means the hidden lat/lng were never
    filled, i.e. the driver typed a place but didn't pick one of the
    suggestions -- telling them it's outside the US/Canada would send them
    looking for the wrong problem."""
    if not name:
        return required_msg
    if lat == 0 and lng == 0:
        return PICK_FROM_SUGGESTIONS_MSG
    return service_area_error(lat, lng, kind)


def clean_stops(locations, lats, lngs):
    """The ride's named stops, each with the coordinates of the suggestion
    the driver picked. Returns (stops, error): stops is a list of dicts with
    the stop's index in `locations`, location, lat and lng; error is a
    message for the first stop that wasn't picked or is out of area. A typed
    but unpicked stop used to be saved at 0,0 and never matched a search
    (ISSUES.md M2)."""
    stops = []
    for idx, loc in enumerate(locations):
        loc = loc.strip()
        if not loc:
            continue
        try:
            lat, lng = float(lats[idx]), float(lngs[idx])
        except (IndexError, ValueError, TypeError):
            lat = lng = 0.0
        if lat == 0 and lng == 0:
            return stops, f'Pick "{loc}" from the suggestions list, or remove that stop.'
        if not in_us_canada(lat, lng):
            return stops, f'"{loc}" is outside the US and Canada. We currently only support stops there.'
        if in_quebec(lat, lng):
            return stops, f'"{loc}" is in Quebec. Komüte isn\'t available in Quebec yet, so please choose a stop outside Quebec.'
        stops.append({'idx': idx, 'location': loc, 'lat': lat, 'lng': lng})
    return stops, None


def seats_error(seats_str):
    """(seats, error) for the seat count as typed."""
    if not seats_str:
        return None, "Seat count is required."
    try:
        seats = int(seats_str)
    except ValueError:
        return None, "Invalid seat count."
    if seats < MIN_SEATS or seats > MAX_SEATS:
        return seats, "Seats must be between 1 and 8."
    return seats, None


def price_cents_error(price_cents, zero_msg):
    """The range check on a price per seat in cents (the API sends cents)."""
    if price_cents <= 0:
        return zero_msg
    if price_cents > MAX_PRICE_CENTS:
        return "Price per seat cannot exceed $1,000."
    return None


def price_error(price_str, zero_msg):
    """(price_cents, error) for the price per seat in dollars, as typed."""
    if not price_str:
        return 0, "Price per seat is required."
    try:
        price_cents = parse_dollars_to_cents(price_str)
    except ValueError:
        return 0, "Invalid price."
    return price_cents, price_cents_error(price_cents, zero_msg)


def departure_error(depart_at, too_far_msg):
    """(field, message) if `depart_at` (aware) is too soon or too far out, else None."""
    if depart_at < timezone.now() + timedelta(minutes=30):
        return "depart_time", "Departure must be at least 30 minutes from now."
    if depart_at.date() > max_ride_date():
        return "date", too_far_msg
    return None


def recurring_errors(recurring_end_str, recurring_days_str, depart_at, driver_tz):
    """Checks a recurring ride's end date and weekdays (0 = Monday).
    Returns (errors, recurring_end, recurring_days)."""
    errors = {}
    recurring_end = None
    recurring_days = []
    if not recurring_end_str:
        errors["recurring_end_date"] = "End date is required for recurring rides."
    else:
        try:
            recurring_end = date.fromisoformat(recurring_end_str)
            if depart_at and recurring_end <= depart_at.astimezone(driver_tz).date():
                errors["recurring_end_date"] = "End date must be after the departure date."
            # Same 4-month limit as a single ride (audit D-8). One ride
            # row per matching day is created, so this also stops a
            # one-request database flood.
            elif recurring_end > max_ride_date():
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
    return errors, recurring_end, recurring_days


# ──────────────────────────────────────────────────────────────────
# Create, update, cancel
# ──────────────────────────────────────────────────────────────────

def create_rides(user, *, vehicle, origin, origin_lat, origin_lng, destination, dest_lat, dest_lng,
                 depart_at, seats, price_cents, passenger_gender_pref, notes, preferences_json,
                 tz_name, driver_tz, duration_seconds, stops,
                 recurring_end=None, recurring_days=None):
    """Creates the ride, or one ride per matching day for a recurring ride
    (recurring_end set). Returns the first ride."""

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
        for stop in stops:
            RideStop.objects.create(
                ride=ride, stop_order=stop['idx'] + 1,
                location=stop['location'], lat=stop['lat'], lng=stop['lng'],
            )
        return ride

    if recurring_end is not None:
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
        return first_ride
    return _create_ride(depart_at)


def update_ride(ride, *, origin, origin_lat, origin_lng, destination, dest_lat, dest_lng, depart_at,
                tz_name, duration_seconds, seats, price_cents, notes, preferences_json,
                passenger_gender_pref, stops):
    """Saves an edited ride (edit_block_reason() was None) and replaces its stops.
    Raises EditBlocked if a booking or a cancel landed since that check."""
    with transaction.atomic():
        # Re-check under the ride lock that bookings take (komute.services
        # .bookings.lock_ride): a request made after the caller's check would
        # otherwise have its seats overwritten by the full-row save below,
        # freeing them for someone else, and its ride changed under it.
        locked = Rides.objects.select_for_update().get(id=ride.id)
        ride.status = locked.status
        reason = edit_block_reason(ride)
        if reason:
            raise EditBlocked(reason)
        return _save_edited_ride(
            ride, origin=origin, origin_lat=origin_lat, origin_lng=origin_lng, destination=destination,
            dest_lat=dest_lat, dest_lng=dest_lng, depart_at=depart_at, tz_name=tz_name,
            duration_seconds=duration_seconds, seats=seats, price_cents=price_cents, notes=notes,
            preferences_json=preferences_json, passenger_gender_pref=passenger_gender_pref, stops=stops)


class EditBlocked(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _save_edited_ride(ride, *, origin, origin_lat, origin_lng, destination, dest_lat, dest_lng, depart_at,
                      tz_name, duration_seconds, seats, price_cents, notes, preferences_json,
                      passenger_gender_pref, stops):
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
    for stop in stops:
        RideStop.objects.create(ride=ride, stop_order=stop['idx'] + 1,
                                location=stop['location'], lat=stop['lat'], lng=stop['lng'])
    return ride


def cancel_error(ride):
    """Why the driver can't cancel `ride`, or None."""
    if ride.status in ('completed', 'cancelled'):
        return f"Cannot cancel — ride is already {ride.status}."
    if ride.depart_at <= timezone.now():
        return "Cannot cancel after the ride has departed."
    return None


def cancel_ride(ride):
    """The driver cancels (cancel_error() was None): every active booking is
    cancelled, chats close, riders get a notification and an email."""
    # Under the ride lock bookings take (komute.services.bookings.lock_ride),
    # so a request made at the same moment is either in this list (and
    # emailed) or refused because the ride is cancelled, never left active.
    with transaction.atomic():
        Rides.objects.select_for_update().filter(id=ride.id).first()
        affected_bookings = list(
            ride.bookings
            .filter(status__in=(RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED))
            .select_related('passenger')
        )

        ride.bookings.filter(
            id__in=[b.id for b in affected_bookings]
        ).update(status=RideBooking.STATUS_CANCELLED, cancelled_at=timezone.now())

        ride.status = 'cancelled'
        ride.cancelled_at = timezone.now()
        ride.save(update_fields=['status', 'cancelled_at'])
    for booking in affected_bookings:
        booking.status = RideBooking.STATUS_CANCELLED
        booking.ride = ride
        notify_booking_status(booking)
    # Pre-request chats stay drafts, but their chat closes with the ride.
    drafts = list(ride.bookings.filter(status=RideBooking.STATUS_DRAFT))
    for booking in drafts:
        booking.ride = ride
        notify_booking_status(booking)
    notify_bookings_changed(
        [b.passenger_id for b in affected_bookings + drafts],
        'The driver cancelled a ride you booked or asked about.',
    )

    send_ride_cancelled_emails(ride, affected_bookings)


def send_ride_cancelled_emails(ride, affected_bookings):
    """Driver cancelled the whole ride — notify every affected passenger."""
    try:
        driver         = ride.driver
        depart_display = fmt_ride_local(ride)

        for booking in affected_bookings:
            try:
                rider = booking.passenger
                ctx   = {
                    'rider_name':     (rider.full_name  or rider.email).title(),
                    'driver_name':    display_name(driver),
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
                    to=[rider.mail_to],
                    reply_to=[settings.REPLY_TO_EMAIL_RIDE_CANCELLED],
                )
                msg.attach_alternative(html, 'text/html')
                send_later(msg, EmailLog.KIND_RIDE_CANCELLED, rider)
            except Exception:
                logger.exception('_send_ride_cancelled_emails could not build or queue an email')
    except Exception:
        logger.exception('_send_ride_cancelled_emails could not build or queue an email')


# ──────────────────────────────────────────────────────────────────
# Search (riders)
# ──────────────────────────────────────────────────────────────────

TIME_SLOT_HOURS = {
    'early_morning': (5, 8),
    'morning':       (8, 12),
    'afternoon':     (12, 17),
    'evening':       (17, 21),
}


def search_date_error(parsed_date, rider_today):
    if parsed_date < rider_today:
        return 'Search date cannot be in the past.'
    if parsed_date > max_ride_date(rider_today):
        return 'You can only search up to 4 months ahead.'
    return None


def search_area_errors(search_from, search_to, origin_lat, origin_lng, dest_lat, dest_lng):
    """Only for points picked from the suggestions (typed text has no coordinates)."""
    errors = {}
    if search_from and (origin_lat or origin_lng):
        if area_error := service_area_error(origin_lat, origin_lng, 'pickup'):
            errors['from'] = area_error
    if search_to and (dest_lat or dest_lng):
        if area_error := service_area_error(dest_lat, dest_lng, 'drop-off'):
            errors['to'] = area_error
    return errors


def search_rides(user, *, search_from, search_to, parsed_date, rider_tz, seats_needed, max_price_str,
                 time_slot, preference, origin_lat, origin_lng, dest_lat, dest_lng):
    """Rides `user` (a rider) may see for this search, soonest first: a list
    when matched by coordinates, else a queryset. A search with a date is
    logged as a RideSearch (it feeds drivers' Route Demand)."""
    s_origin_lat, s_origin_lng, s_dest_lat, s_dest_lng = origin_lat, origin_lng, dest_lat, dest_lng
    if parsed_date:
        try:
            RideSearch.objects.create(
                user=user if user.is_authenticated else None,
                origin=search_from,
                origin_lat=s_origin_lat,
                origin_lng=s_origin_lng,
                destination=search_to,
                dest_lat=s_dest_lat,
                dest_lng=s_dest_lng,
                search_date=parsed_date,
                passenger_count=seats_needed or 1,
            )
        except (ValueError, TypeError):
            pass

    qs = (
        Rides.objects
        .filter(status__in=['posted', 'full'], depart_at__gte=timezone.now())
        # Blocked either way: neither sees the other's rides (ISSUES.md A1).
        .exclude(driver_id__in=UserBlock.objects.hidden_ids(user.id))
        .select_related('driver', 'vehicle')
        .prefetch_related('stops')
        .order_by('depart_at')
    )

    # Filter out gender-restricted rides that don't match the rider's gender
    user_gender = user.gender or ''
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

    if parsed_date:
        day_start, day_end = local_day_bounds(parsed_date, rider_tz)
        qs = qs.filter(depart_at__gte=day_start, depart_at__lt=day_end)

    if seats_needed:
        qs = qs.filter(seats_available__gte=seats_needed)
    # Clamped like seats_needed: no ride costs over $1,000 a seat.
    if max_price_str:
        try:
            max_cents = parse_dollars_to_cents(max_price_str)
            qs = qs.filter(price_cents__lte=min(max(max_cents, 0), MAX_PRICE_CENTS))
        except ValueError:
            pass
    if time_slot in TIME_SLOT_HOURS:
        start_h, end_h = TIME_SLOT_HOURS[time_slot]
        qs = (qs.annotate(local_hour=ExtractHour('depart_at', tzinfo=rider_tz))
                .filter(local_hour__gte=start_h, local_hour__lt=end_h))
    if preference:
        qs = qs.filter(preferences_json__icontains=preference)

    # Proximity: the SQL box drops far-away rides (audit F-2), then the
    # haversine test below keeps those within the radius of the ride's
    # endpoint or one of its stops.
    radius = settings.SEARCH_RADIUS_KM
    if use_origin_coords:
        qs = qs.filter(
            geo_box_q(s_origin_lat, s_origin_lng, radius, 'origin_lat', 'origin_lng')
            | geo_box_q(s_origin_lat, s_origin_lng, radius, 'stops__lat', 'stops__lng')
        ).distinct()
    if use_dest_coords:
        qs = qs.filter(
            geo_box_q(s_dest_lat, s_dest_lng, radius, 'dest_lat', 'dest_lng')
            | geo_box_q(s_dest_lat, s_dest_lng, radius, 'stops__lat', 'stops__lng')
        ).distinct()

    def _near(lat, lng, ride_lat, ride_lng, stops):
        return (haversine_km(lat, lng, ride_lat, ride_lng) <= radius
                or any(haversine_km(lat, lng, st.lat, st.lng) <= radius for st in stops))

    if use_origin_coords or use_dest_coords:
        return [
            ride for ride in qs
            if (not use_origin_coords or _near(s_origin_lat, s_origin_lng,
                                               ride.origin_lat, ride.origin_lng, ride.stops.all()))
            and (not use_dest_coords or _near(s_dest_lat, s_dest_lng,
                                              ride.dest_lat, ride.dest_lng, ride.stops.all()))
        ]
    return qs


def vehicle_card(vehicle):
    """Colour, "year make model" and plate for a ride card. A ride keeps
    running after its vehicle is removed (Rides.vehicle is SET_NULL), so the
    vehicle can be missing."""
    if vehicle is None:
        return {'color': 'silver', 'display': '', 'plate': ''}
    return {'color': vehicle.color or 'silver',
            'display': f"{vehicle.year} {vehicle.make} {vehicle.model}",
            'plate': vehicle.plate}


# ──────────────────────────────────────────────────────────────────
# Completed ride summary and receipt
# ──────────────────────────────────────────────────────────────────

CO2_PER_KM_KG = 0.12
CARRIED_STATUSES = (RideBooking.STATUS_CONFIRMED, RideBooking.STATUS_COMPLETED)


def summary_viewer(ride, user):
    """(is_driver, is_passenger): who may see a ride's summary. Passengers
    need a confirmed or completed booking."""
    is_driver = ride.driver_id == user.id
    is_passenger = ride.bookings.filter(
        passenger=user,
        status__in=('confirmed', 'completed'),
    ).exists()
    return is_driver, is_passenger


def receipt_rides(user):
    """The rides `user` may download a receipt for: their own, or ones they
    have a confirmed or completed booking on."""
    return Rides.objects.filter(
        Q(driver=user) |
        Q(bookings__passenger=user, bookings__status__in=('confirmed', 'completed'))
    ).distinct()


def summary_stats(ride):
    """The figures the summary page and the receipt show. Uses prefetched
    bookings and stops when the caller loaded them."""
    origin_city = ride.origin.split(',')[0].strip()
    dest_city   = ride.destination.split(',')[0].strip()

    distance_km = round(haversine_km(
        ride.origin_lat, ride.origin_lng,
        ride.dest_lat,   ride.dest_lng,
    ))

    duration = ''
    if ride.arrive_at and ride.depart_at:
        delta   = ride.arrive_at - ride.depart_at
        hours   = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    # The riders the trip carried. A pre-request chat (draft) or a request the
    # driver never answered stays as it was when the ride completes, and used
    # to be counted here as a passenger and in "Total received".
    active_bookings = [
        b for b in ride.bookings.all()
        if b.status in CARRIED_STATUSES
    ]
    passenger_count = sum(b.seats for b in active_bookings)

    co2_saved_kg  = round(passenger_count * distance_km * CO2_PER_KM_KG, 1)
    trees_equiv   = round(co2_saved_kg / 21, 1)
    km_avoided    = passenger_count * distance_km

    arrival_note = 'arrived on time'
    if ride.arrive_at and ride.depart_at and ride.expected_duration_seconds:
        actual_seconds   = (ride.arrive_at - ride.depart_at).total_seconds()
        expected_seconds = ride.expected_duration_seconds
        diff = int((expected_seconds - actual_seconds) / 60)
        if diff > 2:
            arrival_note = f'arrived {diff} min early'
        elif diff < -2:
            arrival_note = f'arrived {abs(diff)} min late'

    return {
        'origin_city': origin_city,
        'dest_city': dest_city,
        'distance_km': distance_km,
        'duration': duration,
        'active_bookings': active_bookings,
        'passenger_count': passenger_count,
        'co2_saved_kg': co2_saved_kg,
        'trees_equiv': trees_equiv,
        'km_avoided': km_avoided,
        'arrival_note': arrival_note,
        'total_cents': sum(b.agreed_price_cents for b in active_bookings),
        'ride_ref': f"KMT-{ride.depart_at.year}-{ride.id:04d}",
    }
