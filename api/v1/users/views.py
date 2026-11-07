"""Module 2: the signed-in user's profile (/api/v1/users/me/) and photo
downloads (/api/v1/media/).

Rules from komute.services.profiles and komute.contact_email, shared with
the profile page."""
import logging

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import exceptions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.common.exceptions import FieldError
from api.common.permissions import HasAcceptedTerms
from api.common.throttles import rate_limited
from api.v1.users import serializers as s
from komute import contact_email, private_media
from komute.private_media import delete_stored_file
from komute.services import profiles

logger = logging.getLogger('komute.api')

TAG = ['profile']
SIGNED_IN = [IsAuthenticated, HasAcceptedTerms]


class EmailNotSent(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = 'email_not_sent'
    default_detail = contact_email.NOT_SENT_MSG


def _profile(request, user, warnings=None):
    profile = s.ProfileSerializer(user, context={'request': request}).data
    if warnings is None:
        return Response(profile)
    return Response({'profile': profile, 'warnings': warnings})


class ProfileView(APIView):
    permission_classes = SIGNED_IN

    @extend_schema(tags=TAG, summary='My profile', responses={200: s.ProfileSerializer})
    def get(self, request):
        return _profile(request, request.user)

    @extend_schema(tags=TAG, summary='Change my profile', request=s.ProfileUpdateSerializer,
                   responses={200: s.ProfileResultSerializer},
                   description="Send only the fields that change; the profile page's checks run on the "
                               "result. A verified driver who changes name, date of birth or licence details "
                               "must verify again (a warning says so). Switching to driver needs a vehicle.")
    def patch(self, request):
        user = request.user
        serializer = s.ProfileUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = {**profiles.form_values(user), **serializer.validated_data}

        errors, phone, dob, license_expiry = profiles.check_profile(
            values['full_name'], values['country_code'], values['phone_number'], values['dob'],
            values['role'], values['license_expiry'], values['gender'], values['bio'])
        if values['role'] == 'driver' and 'role' not in errors and not user.vehicles.exists():
            errors['role'] = profiles.DRIVER_NEEDS_VEHICLE_MSG
        if errors:
            raise exceptions.ValidationError({field: [message] for field, message in errors.items()})

        update_fields, identity_reset = profiles.apply_profile(
            user, full_name=values['full_name'], phone=phone, dob=dob, gender=values['gender'],
            role=values['role'], bio=values['bio'], license_number=values['license_number'],
            license_province=values['license_province'], license_expiry=license_expiry)
        user.save(update_fields=update_fields)
        return _profile(request, user, [profiles.IDENTITY_CHANGED_MSG] if identity_reset else [])


@extend_schema(tags=TAG, summary='Upload my profile photo',
               request={'multipart/form-data': {'type': 'object', 'properties': {
                   'photo': {'type': 'string', 'format': 'binary'}}, 'required': ['photo']}},
               responses={200: s.ProfileSerializer},
               description='multipart/form-data with a "photo" file. Replaces the old photo. '
                           'Error code invalid_image (400): too large or not an image.')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def profile_photo(request):
    user = request.user
    photo = request.FILES.get('photo')
    if not photo or not photo.size:
        raise FieldError('photo', 'Choose a photo to upload.', code='required')
    try:
        update_fields, stale_urls = profiles.set_profile_photo(user, photo)
    except DjangoValidationError as e:
        raise FieldError('photo', e.messages[0], code='invalid_image')
    user.save(update_fields=update_fields)
    for old_url in set(stale_urls):
        delete_stored_file(old_url)
    return _profile(request, user)


@extend_schema(tags=TAG, summary='Set my communication email (Apple "Hide My Email")',
               request=s.ContactEmailSerializer, responses={200: s.ProfileResultSerializer},
               description='Only for accounts whose sign-in address is an Apple relay. Emails a confirmation '
                           'link; our emails go there once it is confirmed. Empty removes it. Error code '
                           'not_private_relay (400).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def set_contact_email(request):
    user = request.user
    if not user.uses_private_relay:
        raise FieldError(None, contact_email.NOT_RELAY_MSG, code='not_private_relay')

    raw = s.ContactEmailSerializer(data=request.data)
    raw.is_valid(raise_exception=True)
    raw = raw.validated_data['contact_email']
    if not raw:
        contact_email.remove(user)
        return _profile(request, user, [contact_email.REMOVED_MSG])

    try:
        address = contact_email.check_new_address(user, raw)
    except DjangoValidationError as e:
        raise FieldError('contact_email', e.messages[0])

    # The profile page's two buckets (shared counts).
    if (rate_limited(request, 'change-email', extra_key=str(user.pk), per_ip=False)
            or rate_limited(request, 'email-confirm-target', extra_key=address, per_ip=False)):
        raise exceptions.Throttled(wait=settings.RATE_LIMITS['change-email'][1])

    try:
        contact_email.start(request._request, user, address)
    except Exception:
        logger.exception('API set_contact_email could not send for user %s', user.pk)
        raise EmailNotSent()
    return _profile(request, user, [contact_email.sent_msg(address)])


@extend_schema(tags=TAG, summary='Download a photo',
               responses={(200, 'image/jpeg'): OpenApiResponse(OpenApiTypes.BINARY)},
               description='The URLs in photo_url fields point here. Only the owner (or staff) gets the '
                           'file; anyone else gets a 404.')
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def media_file(request, path):
    """The token-authenticated twin of the website's /private-media/ (same
    rule, same response; ISSUES.md C1). Open without the new Terms, as on
    the website."""
    rel_path = private_media.clean_rel_path(path)
    if rel_path is None or not private_media.user_can_access(request.user, rel_path):
        raise NotFound()
    response = private_media.file_response(rel_path)
    if response is None:
        raise NotFound()
    return response
