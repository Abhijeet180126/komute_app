"""A booking's chat state, and pushing it to open chat pages (ISSUES.md H3).
Moved unchanged from komute/views.py (old names kept there as aliases);
used by rides, invites and, in API module 4, bookings."""
import logging

from komute.models import RideBooking, UserBlock

logger = logging.getLogger('komute.views')

BLOCKED_CHAT_MESSAGE = "You can't message this user."


def chat_status(booking):
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


def notify_booking_status(booking):
    """Push a booking's new status to its open chat pages (ISSUES.md H3), so
    they lock or unlock without a refresh. Best effort: a channel-layer
    failure is logged, never raised, so the status change itself stands."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from komute.consumers import chat_group_name
    try:
        async_to_sync(get_channel_layer().group_send)(chat_group_name(booking.id), {
            'type':   'booking_status',
            'status': booking.status,
            **chat_status(booking),
        })
    except Exception:
        logger.warning('Could not push status of booking %s to its chat', booking.id, exc_info=True)
