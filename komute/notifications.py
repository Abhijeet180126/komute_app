"""Per-user "your bookings changed" notices (ISSUES.md M4).

The inboxes and my-rides page open a socket to NotifyConsumer, which joins
the user's group. When someone else requests, accepts, rejects or cancels a
booking, the other party gets a short notice and the page offers a refresh.

A fingerprint of the user's bookings is embedded in the page and sent again
when the socket connects, so a change made while the page was loading or
the socket was down still shows the notice.
"""
import hashlib
import logging

from django.db.models import Q

logger = logging.getLogger('komute.chat')


def user_group_name(user_id):
    return f'user_{user_id}'


def booking_fingerprint(user):
    """Short hash of what the inboxes and my-rides show about the user's
    bookings. Changes whenever a booking's status or price, a ride's status
    or its free seats change."""
    from .models import RideBooking, Rides

    bookings = (
        RideBooking.objects
        .filter(Q(passenger=user) | (Q(ride__driver=user) & ~Q(status=RideBooking.STATUS_DRAFT)))
        .order_by('id')
        .values_list('id', 'status', 'agreed_price_cents', 'ride__status')
    )
    rides = (
        Rides.objects.filter(driver=user)
        .order_by('id')
        .values_list('id', 'status', 'seats_available')
    )
    raw = repr((list(bookings), list(rides))).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def notify_bookings_changed(user_ids, message):
    """Tell each user's open inbox / my-rides pages that their bookings
    changed. Best effort: a channel-layer failure is logged, never raised,
    so the change itself stands."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    for user_id in set(user_ids):
        try:
            async_to_sync(layer.group_send)(user_group_name(user_id), {
                'type':    'bookings_changed',
                'message': message,
            })
        except Exception:
            logger.warning('Could not notify user %s of a booking change', user_id, exc_info=True)
