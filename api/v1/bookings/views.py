"""Module 4: bookings and price offers (/api/v1/bookings/, /api/v1/offers/).

Rules from komute.services.bookings, shared with the booking page, My Rides,
the inboxes and the price negotiation in chat. A booking is visible to its
rider and to the ride's driver only; anyone else gets a 404."""
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import exceptions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from api.common.fields import decode_hashid
from api.common.pagination import StandardPagination
from api.common.permissions import HasAcceptedTerms
from api.v1.bookings import serializers as s
from komute.chat_tokens import make_chat_token
from komute.models import PriceOffer, RideBooking, Rides, UserBlock
from komute.services import bookings as booking_rules

TAG = ['bookings']
SIGNED_IN = [IsAuthenticated, HasAcceptedTerms]


class BookingRefused(exceptions.APIException):
    def __init__(self, error):
        self.status_code = error.status
        super().__init__(error.message, code=error.code)


def _booking_qs():
    return RideBooking.objects.select_related(
        'ride__driver', 'ride__vehicle', 'passenger').prefetch_related('ride__stops')


def _pk(value):
    pk = decode_hashid(value)
    if pk is None:
        raise exceptions.NotFound()
    return pk


def _my_booking(request, booking_id):
    """A booking the user is the rider or the driver of, else 404."""
    booking = get_object_or_404(_booking_qs(), pk=_pk(booking_id))
    if request.user.id not in (booking.passenger_id, booking.ride.driver_id):
        raise exceptions.NotFound()
    return booking


def _data(request, booking):
    return s.BookingSerializer(_booking_qs().get(pk=booking.pk), context={'request': request}).data


def _page(request, qs, serializer_class):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    return paginator.get_paginated_response(serializer_class(page, many=True, context={'request': request}).data)


STATUS_PARAM = OpenApiParameter('status', str, description='Only this status (e.g. requested, confirmed).')


@extend_schema(tags=TAG, methods=['GET'], summary='My bookings (as a rider)', parameters=[STATUS_PARAM],
               responses={200: s.BookingSerializer(many=True)},
               description='Newest first; pre-request chats (drafts) left out, as on My Rides. Paginated.')
@extend_schema(tags=TAG, methods=['POST'], summary='Book seats on a ride', request=s.BookRequestSerializer,
               responses={201: s.BookingSerializer},
               description='Same rules as the booking page: riders only, not your own ride, not a driver who '
                           'blocked you (or you them); verified email and date of birth; an open ride; its gender '
                           'preference; 1-8 seats; one booking per ride. Error codes: riders_only, own_ride, '
                           'blocked, email_unverified, dob_missing, gender_preference (403); ride_unavailable, '
                           'invalid_seats, already_requested, not_enough_seats (400).')
@api_view(['GET', 'POST'])
@permission_classes(SIGNED_IN)
def booking_list(request):
    if request.method == 'GET':
        qs = _booking_qs().filter(passenger=request.user).exclude(status=RideBooking.STATUS_DRAFT)
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        return _page(request, qs.order_by('-booked_at'), s.BookingSerializer)

    body = s.BookRequestSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    data = body.validated_data
    if request.user.role != 'rider':
        raise exceptions.PermissionDenied('Only riders can book rides.', code='riders_only')
    ride = get_object_or_404(Rides.objects.select_related('driver'), pk=_pk(data['ride']))
    if ride.driver_id == request.user.id:
        raise exceptions.PermissionDenied('You cannot book your own ride.', code='own_ride')
    if UserBlock.objects.between(ride.driver_id, request.user.id):
        raise exceptions.PermissionDenied("You can't book this ride.", code='blocked')
    try:
        booking = booking_rules.book_ride(
            ride, request.user, intent=data['intent'], seats_raw=data['seats'],
            # A stop id that doesn't decode is "no stop", as on the website.
            pickup_stop_id=decode_hashid(data['pickup_stop']) if data['pickup_stop'] else None,
            dropoff_stop_id=decode_hashid(data['dropoff_stop']) if data['dropoff_stop'] else None)
    except booking_rules.BookingError as e:
        raise BookingRefused(e)
    return Response(_data(request, booking), status=status.HTTP_201_CREATED)


@extend_schema(tags=TAG, summary='Bookings on my rides (as a driver)',
               parameters=[STATUS_PARAM, OpenApiParameter('ride', str, description='Only this ride (id).')],
               responses={200: s.BookingSerializer(many=True)},
               description='Requests first, then newest. Pre-request chats are in the inbox instead. Paginated.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def bookings_on_my_rides(request):
    qs = _booking_qs().filter(ride__driver=request.user).exclude(status=RideBooking.STATUS_DRAFT)
    if request.query_params.get('ride'):
        qs = qs.filter(ride_id=_pk(request.query_params['ride']))
    if request.query_params.get('status'):
        qs = qs.filter(status=request.query_params['status'])
    qs = sorted(qs.order_by('-booked_at'), key=lambda b: b.status != RideBooking.STATUS_REQUESTED)
    return _page(request, qs, s.BookingSerializer)


@extend_schema(tags=TAG, summary='One booking', responses={200: s.BookingSerializer},
               description='For its rider or the ride\'s driver.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def booking_detail(request, booking_id):
    return Response(_data(request, _my_booking(request, booking_id)))


def _act(request, booking_id, *, as_driver, action):
    booking = _my_booking(request, booking_id)
    owner_id = booking.ride.driver_id if as_driver else booking.passenger_id
    if owner_id != request.user.id:
        raise exceptions.PermissionDenied('Not authorized.', code='not_authorized')
    try:
        action(booking)
    except booking_rules.BookingError as e:
        raise BookingRefused(e)
    return Response(_data(request, booking))


@extend_schema(tags=TAG, summary='Cancel my booking (rider)', request=None, responses={200: s.BookingSerializer},
               description='The seats go back and the driver is emailed. Error codes: already_closed, ride_closed, '
                           'departed (400), not_authorized (403, the driver).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def cancel(request, booking_id):
    return _act(request, booking_id, as_driver=False, action=booking_rules.cancel_booking)


@extend_schema(tags=TAG, summary='Accept a booking request (driver)', request=None,
               responses={200: s.BookingSerializer},
               description='Both are emailed. Error codes: not_pending (400), driver_not_verified, blocked, '
                           'not_authorized (403).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def accept(request, booking_id):
    return _act(request, booking_id, as_driver=True, action=booking_rules.accept_booking)


@extend_schema(tags=TAG, summary='Reject a booking request (driver)', request=None,
               responses={200: s.BookingSerializer},
               description='The seats go back and the rider is emailed. Error codes: not_pending (400), '
                           'not_authorized (403).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def reject(request, booking_id):
    return _act(request, booking_id, as_driver=True, action=booking_rules.reject_booking)


@extend_schema(tags=TAG, methods=['GET'], summary='Price offers on a booking', responses={200: s.OfferSerializer(many=True)},
               description='Newest first.')
@extend_schema(tags=TAG, methods=['POST'], summary='Make a price offer', request=s.OfferRequestSerializer,
               responses={201: s.OfferSerializer},
               description='Per seat, by the rider or the driver, on a requested or confirmed booking. Your own '
                           'earlier pending offer is withdrawn. Error codes: negotiation_closed, invalid_price '
                           '(400), blocked (403).')
@api_view(['GET', 'POST'])
@permission_classes(SIGNED_IN)
def offers(request, booking_id):
    booking = _my_booking(request, booking_id)
    if request.method == 'GET':
        qs = PriceOffer.objects.filter(booking=booking).order_by('-created_at')
        return Response(s.OfferSerializer(qs, many=True, context={'request': request}).data)
    body = s.OfferRequestSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    try:
        offer = booking_rules.create_offer(booking, request.user, body.validated_data['price_cents'])
    except booking_rules.BookingError as e:
        raise BookingRefused(e)
    return Response(s.OfferSerializer(offer, context={'request': request}).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['bookings'], summary='Answer a price offer', request=s.RespondOfferSerializer,
               responses={200: s.OfferSerializer, 201: s.OfferSerializer},
               description='accept (sets the booking\'s agreed price: offer × seats + service fee), decline, or '
                           'counter with a new price (201, the new offer). Only the other person may answer. '
                           'Error codes: not_pending, negotiation_closed, invalid_price (400), own_offer, blocked (403).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def respond(request, offer_id):
    offer = get_object_or_404(PriceOffer.objects.select_related('booking__ride__driver', 'booking__passenger'),
                              pk=_pk(offer_id))
    booking = offer.booking
    if request.user.id not in (booking.passenger_id, booking.ride.driver_id):
        raise exceptions.NotFound()
    body = s.RespondOfferSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    action = body.validated_data['action']
    try:
        result = booking_rules.respond_offer(offer, request.user, action, body.validated_data.get('price_cents'))
    except booking_rules.BookingError as e:
        raise BookingRefused(e)
    return Response(s.OfferSerializer(result, context={'request': request}).data,
                    status=status.HTTP_201_CREATED if action == 'counter' else status.HTTP_200_OK)


@extend_schema(tags=TAG, summary='Inbox', responses={200: s.InboxRowSerializer(many=True)},
               description="One conversation per ride and person, as the website's inbox: for a driver, requests "
                           "and chats on their rides (requests first); for a rider, their bookings (unread first).")
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def inbox(request):
    user = request.user
    if user.role not in ('driver', 'rider'):
        raise exceptions.PermissionDenied('Only drivers and riders have an inbox.', code='no_inbox')
    rows = [{**row, 'booking_id': row['booking'].pk, 'booking_status': row['booking'].status,
             'chat_token': make_chat_token(row['booking'].pk), 'viewer_id': user.id}
            for row in booking_rules.inbox(user, as_driver=user.role == 'driver')]
    return Response(s.InboxRowSerializer(rows, many=True, context={'request': request}).data)
