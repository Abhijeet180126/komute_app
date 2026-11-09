"""Module 5: chat over REST (state and history). The conversation list is
GET /bookings/inbox/; live messages go through the socket in api/routing.py."""
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from api.common.pagination import StandardPagination
from api.v1.bookings.views import SIGNED_IN, _my_booking
from api.v1.chat import serializers as s
from komute.chat_tokens import make_chat_token
from komute.models import UserBlock
from komute.services import chat as chat_rules
from komute.services.booking_status import chat_status

TAG = ['chat']


@extend_schema(tags=TAG, summary='Chat state and socket details', responses={200: s.ChatStateSerializer},
               description="For the booking's rider or the ride's driver. Says whether the chat and price "
                           'negotiation are open, and how to connect.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def chat_state(request, booking_id):
    booking = _my_booking(request, booking_id)
    user = request.user
    other = chat_rules.other_party(booking, user)
    status = chat_status(booking)
    agreed = status['agreed_price'] is not None
    token = make_chat_token(booking.pk)
    return Response(s.ChatStateSerializer({
        **status,
        'booking_id': booking.pk,
        'booking_status': booking.status,
        'chat_token': token,
        'socket_path': f'/ws/app/chat/{token}/',
        'is_driver': user.pk == booking.ride.driver_id,
        'other': other,
        'blocked_by_me': UserBlock.objects.filter(blocker=user, blocked=other).exists(),
        'ride_price_cents': booking.ride.price_cents,
        'agreed_price_cents': booking.agreed_price_cents if agreed else None,
        'pending_incoming': None if agreed else chat_rules.latest_pending_offer(booking, other),
        'pending_outgoing': None if agreed else chat_rules.latest_pending_offer(booking, user),
    }, context={'request': request}).data)


@extend_schema(tags=TAG, summary='Message history', responses={200: s.ChatMessageSerializer(many=True)},
               description='Newest first. Paginated. Marks what was sent to you as read, as opening the chat '
                           'page does.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def messages(request, booking_id):
    booking = _my_booking(request, booking_id)
    history = chat_rules.history(booking, request.user)
    chat_rules.mark_read(history, request.user)
    paginator = StandardPagination()
    page = paginator.paginate_queryset(history.order_by('-created_at', '-id'), request)
    return paginator.get_paginated_response(s.ChatMessageSerializer(page, many=True, context={'request': request}).data)
