"""A booking's chat: its messages and open price offers (API module 5).
Moved from komute/views.py chat_view, which calls these. The live socket
is komute.consumers.ChatConsumer; the app's version builds on it."""
from django.db.models import Q
from django.utils import timezone

from komute.models import Message, PriceOffer


def other_party(booking, user):
    return booking.ride.driver if user == booking.passenger else booking.passenger


def history(booking, user):
    """The messages between the booking's two people on its ride."""
    other_user = other_party(booking, user)
    return (
        Message.objects
        .filter(ride=booking.ride)
        .filter(
            Q(sender=user, receiver=other_user) |
            Q(sender=other_user, receiver=user)
        )
        .select_related('sender')
    )


def mark_read(messages, user):
    """Opening the chat reads what was sent to `user`."""
    messages.filter(receiver=user, read_at__isnull=True).update(read_at=timezone.now())


def latest_pending_offer(booking, proposed_by):
    return (
        PriceOffer.objects
        .filter(booking=booking, proposed_by=proposed_by, status=PriceOffer.STATUS_PENDING)
        .order_by('-created_at')
        .first()
    )
