"""Module 7: reviews, reports and blocks (/api/v1/safety/).

Rules from komute.services.safety, shared with the review page, the report
and block menu, and the profile's blocked list."""
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from api.common.fields import decode_hashid
from api.common.pagination import StandardPagination
from api.common.throttles import rate_limited
from api.v1.bookings.views import SIGNED_IN
from api.v1.safety import serializers as s
from komute.models import RideBooking
from komute.services import safety

TAG = ['safety']


class SafetyRefused(exceptions.APIException):
    def __init__(self, error):
        self.status_code = error.status
        super().__init__(error.message, code=error.code)


class TooManyReports(exceptions.APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = 'throttled'
    default_detail = 'Too many reports sent. Please try again later.'


def _target(request, data):
    """The other person named by a booking or ride, else 404 (as the website)."""
    target, booking = safety.safety_target(request.user, data['booking'].strip(), data['ride'].strip())
    if target is None:
        raise exceptions.NotFound('User not found.')
    return target, booking


@extend_schema(tags=TAG, summary='Trips waiting for my review', responses={200: s.PendingReviewSerializer(many=True)},
               description='Completed trips you took part in and haven\'t reviewed. Newest first. Paginated.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def pending_reviews(request):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(safety.pending_reviews(request.user), request)
    return paginator.get_paginated_response(s.PendingReviewSerializer(page, many=True,
                                                                      context={'request': request}).data)


@extend_schema(tags=TAG, summary='Review a trip', request=s.ReviewRequestSerializer, responses={201: None},
               description="Rate the other person on a completed trip, once, as on the website's review page. "
                           'Error codes: not_completed, already_reviewed (400), not_authorized (403).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def review(request):
    body = s.ReviewRequestSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    data = body.validated_data
    booking = get_object_or_404(RideBooking.objects.select_related('ride__driver', 'passenger'),
                                pk=decode_hashid(data['booking']) or 0)
    try:
        reviewee_id = safety.check_review(booking, request.user)
    except safety.SafetyError as e:
        if e.code == 'not_authorized':
            raise exceptions.NotFound()  # someone else's booking looks like none
        raise SafetyRefused(e)
    try:
        body, tags, subratings = safety.clean_review_extras(data['comment'], data['tags'], data['subratings'])
    except safety.SafetyError as e:
        raise SafetyRefused(e)
    safety.save_review(booking, request.user, reviewee_id, rating=data['rating'], body=body,
                       is_anonymous=data['anonymous'], tags=tags, subratings=subratings)
    return Response(status=status.HTTP_201_CREATED)


@extend_schema(tags=TAG, summary='Report a user', request=s.ReportRequestSerializer, responses={201: None},
               description='Name them by a booking you are on, or a ride you can see (its driver). Staff are '
                           'told. Error codes: details_required, details_too_long (400), throttled (429); 404 if '
                           'the booking or ride names nobody.')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def report(request):
    # The website's bucket (shared count), before anything else.
    if rate_limited(request, 'report-user', extra_key=str(request.user.id)):
        raise TooManyReports()
    body = s.ReportRequestSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    data = body.validated_data
    reported, booking = _target(request, data)
    try:
        safety.report_user(request._request, request.user, reported, booking, data['reason'],
                           data['details'].strip())
    except safety.SafetyError as e:
        raise SafetyRefused(e)
    return Response(status=status.HTTP_201_CREATED)


@extend_schema(tags=TAG, methods=['GET'], summary='Users I blocked', responses={200: s.BlockedUserSerializer(many=True)},
               description='Newest first.')
@extend_schema(tags=TAG, methods=['POST'], summary='Block a user', request=s.TargetSerializer,
               responses={201: s.BlockedUserSerializer},
               description='Name them by a booking or a ride, as for a report. Neither of you then sees the '
                           "other's rides, can book, chat or invite; open chats close. Blocking again is harmless.")
@api_view(['GET', 'POST'])
@permission_classes(SIGNED_IN)
def blocks(request):
    if request.method == 'GET':
        return Response(s.BlockedUserSerializer(safety.blocked_users(request.user), many=True).data)
    body = s.TargetSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    target, _ = _target(request, body.validated_data)
    safety.block(request.user, target)
    block_row = safety.blocked_users(request.user).get(blocked=target)
    return Response(s.BlockedUserSerializer(block_row).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=TAG, summary='Unblock a user', responses={204: None},
               description='An id from GET /safety/blocks/. 404 if you hadn\'t blocked them.')
@api_view(['DELETE'])
@permission_classes(SIGNED_IN)
def unblock(request, user_id):
    pk = decode_hashid(user_id)
    if not pk or not safety.unblock(request.user, pk):
        raise exceptions.NotFound()
    return Response(status=status.HTTP_204_NO_CONTENT)
