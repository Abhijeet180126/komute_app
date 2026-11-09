"""Module 3: ride invites (/api/v1/invites/).

Rules from komute.services.invites, shared with the website's invite views.
The website reaches an invite only through the emailed link; the app can
also list a rider's own invites. Either way only the invited rider can see
or answer one (anyone else gets a 404, so ids can't be probed)."""
from django.conf import settings
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from api.common.fields import decode_hashid
from api.common.pagination import StandardPagination
from api.common.permissions import HasAcceptedTerms
from api.common.throttles import rate_limited
from api.v1.invites import serializers as s
from komute.models import RideInvite, Rides
from komute.services import invites

TAG = ['invites']
SIGNED_IN = [IsAuthenticated, HasAcceptedTerms]


class InviteRefused(exceptions.APIException):
    def __init__(self, error):
        self.status_code = error.status
        super().__init__(error.message, code=error.code)


class TooManyInvites(exceptions.APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = 'throttled'


def _invite_qs():
    return RideInvite.objects.select_related('driver', 'passenger', 'ride__driver', 'ride__vehicle') \
        .prefetch_related('ride__stops')


def _my_invite(request, invite_id):
    """An invite sent to the signed-in rider, else 404."""
    pk = decode_hashid(invite_id)
    if pk is None:
        raise exceptions.NotFound()
    return get_object_or_404(_invite_qs(), pk=pk, passenger=request.user)


def _data(request, invite):
    return s.InviteSerializer(invite, context={'request': request}).data


@extend_schema(tags=TAG, methods=['GET'], summary='Invites I received', responses={200: s.InviteSerializer(many=True)},
               description='Newest first. Paginated.')
@extend_schema(tags=TAG, methods=['POST'], summary='Invite a rider to my ride', request=s.SendInviteSerializer,
               responses={201: s.InviteSerializer},
               description='Same rules as Route Demand\'s Invite button: a verified driver; a rider who searched '
                           'this route in the last 7 days or has booked with you; the ride\'s gender preference; '
                           'seats left; one open invite per rider. The rider is emailed a link. Error codes: '
                           'not_eligible, ride_full, already_invited (400), driver_not_verified (403), throttled (429).')
@api_view(['GET', 'POST'])
@permission_classes(SIGNED_IN)
def invite_list(request):
    if request.method == 'GET':
        qs = _invite_qs().filter(passenger=request.user).order_by('-sent_at')
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(s.InviteSerializer(page, many=True,
                                                                   context={'request': request}).data)

    # The website's buckets (shared counts), checked before anything else.
    if rate_limited(request, 'send-invite', extra_key=str(request.user.id)):
        raise TooManyInvites('Too many invites sent. Please try again later.')
    if rate_limited(request, 'send-invite-daily', extra_key=str(request.user.id)):
        raise TooManyInvites('Daily invite limit reached. Please try again tomorrow.')

    body = s.SendInviteSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    ride_pk = decode_hashid(body.validated_data['ride'])
    ride = get_object_or_404(Rides, id=ride_pk or 0, driver=request.user, status__in=['posted', 'full'])
    # An id that doesn't decode gets the same answer as an ineligible rider.
    rider_pk = decode_hashid(body.validated_data['rider']) or 0
    try:
        invite = invites.send_invite(request._request, request.user, ride, rider_pk)
    except invites.InviteError as e:
        raise InviteRefused(e)
    return Response(_data(request, _invite_qs().get(pk=invite.pk)), status=status.HTTP_201_CREATED)


@extend_schema(tags=TAG, summary='Open an invite', responses={200: s.InviteSerializer},
               description='Marks it viewed (or expired, past its time), as opening the emailed link does.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def invite_detail(request, invite_id):
    invite = _my_invite(request, invite_id)
    invites.open_invite(invite)
    return Response(_data(request, invite))


@extend_schema(tags=TAG, summary='Open an invite from its emailed link', responses={200: s.InviteSerializer},
               description='For the app opening /invite/<token>/ itself. Error code invite_expired (410) for '
                           f'a link older than {settings.INVITE_EXPIRY_HOURS} hours.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def invite_by_token(request, token):
    invite_id, err = invites.resolve_invite_token(token)
    if err == 'expired_token':
        raise InviteRefused(invites.InviteExpired())
    if err:
        raise exceptions.NotFound()
    invite = get_object_or_404(_invite_qs(), pk=invite_id, passenger=request.user)
    invites.open_invite(invite)
    return Response(_data(request, invite))


@extend_schema(tags=TAG, summary='Accept an invite', request=None, responses={200: s.AcceptedSerializer},
               description='Confirms a booking on the ride, with the same checks as the website: verified email, '
                           'date of birth, the gender preference, one booking per ride, seats left, the driver\'s '
                           'verification current. Error codes: already_responded, ride_unavailable, ride_full, '
                           'already_booked, driver_not_verified (400), email_unverified, dob_missing, '
                           'gender_preference (403), booking_changed (409), invite_expired (410).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def accept(request, invite_id):
    invite = _my_invite(request, invite_id)
    try:
        booking = invites.accept_invite(invite, request.user)
    except invites.InviteError as e:
        raise InviteRefused(e)
    invite = _invite_qs().get(pk=invite.pk)
    return Response(s.AcceptedSerializer({
        'invite': invite, 'booking_id': booking.pk, 'booking_status': booking.status,
        'seats': booking.seats, 'agreed_price_cents': booking.agreed_price_cents,
    }, context={'request': request}).data)


@extend_schema(tags=TAG, summary='Decline an invite', request=None, responses={200: s.InviteSerializer},
               description='Error code already_responded (400).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def decline(request, invite_id):
    invite = _my_invite(request, invite_id)
    try:
        invites.decline_invite(invite)
    except invites.InviteError as e:
        raise InviteRefused(e)
    return Response(_data(request, _invite_qs().get(pk=invite.pk)))
