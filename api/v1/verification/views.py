"""Module 8: driver identity verification (/api/v1/verification/).

Rules from komute.services.verification, shared with the profile page's
Verify Identity flow. The licence photo and the selfie are kept in memory,
sent to the verification node and dropped: never written to disk or storage."""
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import exceptions, serializers, status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.response import Response

from api.common.throttles import rate_limited
from api.v1.bookings.views import SIGNED_IN
from komute.client_ip import client_ip
from komute.services import profiles, verification
from komute.services.common import consent_details

TAG = ['verification']
AGREEMENT_TEXT = ('I have read this notice and I agree to Komüte collecting and using my biometric information '
                  'as described above.')


class VerificationRefused(exceptions.APIException):
    def __init__(self, error):
        self.status_code = error.status
        super().__init__(error.message, code=error.code)


class TooManyAttempts(exceptions.APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = 'throttled'
    default_detail = 'Too many verification attempts. Please try again later.'


StatusSerializer = inline_serializer('VerificationStatus', {
    'driver_verified': serializers.BooleanField(help_text='Verified and still current.'),
    'reverify_due_at': serializers.DateTimeField(allow_null=True),
    'license_expired': serializers.BooleanField(),
    'reverify_soon': serializers.BooleanField(),
    'reverify_expired': serializers.BooleanField(),
    'review': serializers.CharField(help_text='"pending" (a person is reviewing), "available" (the last attempt '
                                              'was rejected and can be sent for review) or "".'),
    'notice_version': serializers.CharField(help_text='Send back with the attempt.'),
})

NoticeSerializer = inline_serializer('BiometricNotice', {
    'version': serializers.CharField(),
    'html': serializers.CharField(help_text='The notice, exactly as the website shows it. Show it in full.'),
    'agreement': serializers.CharField(help_text='The sentence next to the checkbox.'),
})

ResultSerializer = inline_serializer('VerificationResult', {
    'verified': serializers.BooleanField(),
    'decision': serializers.CharField(allow_null=True, help_text='APPROVED, REJECTED, or another value when a '
                                                                 'person will decide.'),
    'reasons': serializers.ListField(child=serializers.CharField(), help_text='Why it was rejected, to show.'),
    'checks': serializers.DictField(child=serializers.BooleanField(),
                                    help_text='license_ocr, face_biometrics, vehicle_verification.'),
    'review_pending': serializers.BooleanField(help_text='No clear result: a person decides; the driver waits.'),
    'can_request_review': serializers.BooleanField(),
})


@extend_schema(tags=TAG, summary='My verification status', responses={200: StatusSerializer})
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def verification_status(request):
    user = request.user
    driver = profiles.driver_status(user, [v.vehicle_verified for v in user.vehicles.all()])
    return Response({
        'driver_verified': driver['driver_verified'],
        'reverify_due_at': driver['reverify_due_at'],
        'license_expired': bool(driver['license_expired']),
        'reverify_soon': bool(driver['reverify_soon']),
        'reverify_expired': bool(driver['reverify_expired']),
        'review': verification.review_state(user),
        'notice_version': settings.BIOMETRIC_NOTICE_VERSION,
    })


@extend_schema(tags=TAG, summary='The biometric notice', responses={200: NoticeSerializer},
               description='Show it before every attempt; the driver must agree each time (privacy policy s.12).')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def notice(request):
    html = render_to_string('biometric-notice.html', {
        'privacy_policy_url': request.build_absolute_uri(reverse('privacy-policy'))})
    return Response({'version': settings.BIOMETRIC_NOTICE_VERSION, 'html': html.strip(),
                     'agreement': AGREEMENT_TEXT})


@extend_schema(tags=TAG, summary='Verify my identity',
               request={'multipart/form-data': {'type': 'object', 'properties': {
                   'license_image': {'type': 'string', 'format': 'binary', 'description': 'Front of the licence.'},
                   'license_back_image': {'type': 'string', 'format': 'binary',
                                          'description': 'Back of the licence, with the barcode. Do not shrink it '
                                                         'below 1200 px on the long side.'},
                   'selfie': {'type': 'string', 'format': 'binary', 'description': 'From the camera, now.'},
                   'biometric_consent': {'type': 'boolean', 'description': 'The driver ticked the notice box.'},
                   'notice_version': {'type': 'string', 'description': 'The version of the notice shown.'},
               }, 'required': ['license_image', 'license_back_image', 'selfie', 'biometric_consent',
                               'notice_version']}},
               responses={200: ResultSerializer},
               description='Same as the profile page: needs the current notice agreed, photos of the front and '
                           'the back of the licence, a selfie taken now, and a primary vehicle with a photo; '
                           'licence details come from the profile. Drivers only. Takes up to two minutes. '
                           'Photos are never stored. Error codes: consent_required, notice_changed, '
                           'license_required, invalid_license_image, license_back_required, '
                           'invalid_license_back_image, selfie_required, invalid_selfie, '
                           'vehicle_photo_required, invalid_photo (retake it), profile_incomplete, profile_invalid '
                           '(fix the profile) (400), not_a_driver (403), throttled (429), service_offline, '
                           'service_busy, service_error (503). Busy or offline does not use an attempt.')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
@parser_classes([MultiPartParser])
def verify(request):
    # Before anything reads the body: uploads stay in memory, never on disk
    # (audit P-5/P-13), however large.
    request._request.upload_handlers = [verification.InMemoryUploadHandler(request._request)]

    user = request.user
    if rate_limited(request, 'verify-driver', extra_key=str(user.id)):
        raise TooManyAttempts()

    consent_flag = str(request.data.get('biometric_consent', '')).lower() in ('1', 'true', 'yes', 'on')
    refused = verification.consent_error(consent_flag, request.data.get('notice_version'))
    if refused:
        raise VerificationRefused(verification.VerificationError(refused[0], code=refused[1]))

    try:
        result = verification.verify(
            request._request, user, request.FILES.get('license_image'), request.FILES.get('license_back_image'),
            request.FILES.get('selfie'),
            consent_details(client_ip(request), request.META.get('HTTP_USER_AGENT', '')))
    except verification.VerificationError as e:
        raise VerificationRefused(e)

    return Response({
        'verified': result['verified'],
        'decision': result['decision'],
        'reasons': result['reasons'],
        'checks': result['stage_results'],
        'review_pending': result['review_pending'],
        'can_request_review': result['can_request_review'],
    })


@extend_schema(tags=TAG, summary='Ask a person to review my rejected verification',
               request=inline_serializer('VerificationReviewRequest', {
                   'comment': serializers.CharField(required=False, help_text='Up to 2000 characters.')}),
               responses={200: None},
               description='For the latest attempt, if it was rejected (Quebec Law 25 s.12.1). Staff are told. '
                           'Error codes: nothing_to_review, comment_too_long (400), review_pending (409).')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
@parser_classes([JSONParser])
def request_review(request):
    comment = request.data.get('comment', '') if isinstance(request.data, dict) else ''
    try:
        verification.request_review(request._request, request.user, str(comment or '').strip())
    except verification.VerificationError as e:
        raise VerificationRefused(e)
    return Response({'review': verification.review_state(request.user)})
