"""Module 1: auth and account (docs/api-modular-breakup.md).

Every rule comes from komute.services.accounts, shared with the website,
and every rate limit is the website's bucket (shared counts). Paths are
under /api/v1/auth/.

Terms: like the website (LegalAcceptanceMiddleware), a signed-in user who
hasn't accepted the current Terms and Privacy Policy can only reach the
session, the Terms, logout and account deletion until they accept."""
import logging

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import exceptions, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from api.common.exceptions import FieldError
from api.common.permissions import HasAcceptedTerms
from api.common.throttles import check_rate_limit, rate_limited
from api.v1.auth import serializers as s
from api.v1.auth import social
from api.v1.auth.sessions import end_app_session, keep_app_session, start_app_session
from komute.models import LegalAcceptance
from komute.services import accounts
from komute.services.common import consent_details
from komute.client_ip import client_ip

logger = logging.getLogger('komute.api')

TAG = ['auth']
SIGNED_IN = [IsAuthenticated, HasAcceptedTerms]


class InvalidCredentials(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = 'invalid_credentials'
    default_detail = 'Invalid email or password'


class LockedOut(exceptions.APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = 'locked_out'
    default_detail = 'Too many failed login attempts. Please try again later.'


class EmailNotSent(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = 'email_not_sent'
    default_detail = 'Could not send the email. Please try again later.'


def _consent(request):
    return consent_details(client_ip(request), request.META.get('HTTP_USER_AGENT', ''))


def _session_payload(token, user, first_login):
    return s.SessionSerializer({'token': token, 'first_login': first_login, 'user': user}).data


def _validated(serializer_class, request):
    serializer = serializer_class(data=request.data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _account_error(error):
    """AccountError from the service, as a 400 on its field."""
    return s.account_error_to_validation(error)


# ── Sign up, log in, log out ──────────────────────────────────────

@extend_schema(tags=TAG, auth=[], request=s.SignupSerializer,
               responses={201: s.SessionSerializer},
               summary='Sign up with email and password',
               description='Same fields and rules as the website. Signs the new user in: the reply has '
                           'their token, and first_login is true (show the rider/driver choice). '
                           'A welcome email is sent; the address still needs verifying '
                           '(POST /auth/email/verify/) before posting or booking.')
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def signup(request):
    check_rate_limit(request, 'signup')
    data = _validated(s.SignupSerializer, request)
    user = accounts.create_account(
        request._request, email=data['email'], password=data['password'], full_name=data['full_name'],
        phone=data['phone'], dob=data['dob'], consent=_consent(request))
    token = start_app_session(request, user)
    return Response(_session_payload(token, user, first_login=True), status=status.HTTP_201_CREATED)


@extend_schema(tags=TAG, auth=[], request=s.LoginSerializer, responses={200: s.SessionSerializer},
               summary='Log in with email and password',
               description='Error codes: invalid_credentials (400), throttled or locked_out (429).')
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def login(request):
    data = _validated(s.LoginSerializer, request)
    email, password = data['email'], data['password']

    # The website's two buckets: per address (one source trying many
    # accounts) and per account (one account tried from many addresses).
    blocked = rate_limited(request, 'login-ip')
    if email:
        blocked = rate_limited(request, 'login-acct', extra_key=email.lower()) or blocked
    if blocked:
        raise exceptions.Throttled(wait=settings.RATE_LIMITS['login-ip'][1])

    django_request = request._request
    user = accounts.authenticate_by_email(django_request, email, password)
    if getattr(django_request, 'axes_locked_out', False):
        # django-axes refused the attempt. Its middleware would replace our
        # reply with its HTML lockout page, so answer here and tell it not to.
        django_request.axes_locked_out = False
        raise LockedOut()
    if user is None:
        raise InvalidCredentials()

    first_login = user.last_login is None
    token = start_app_session(request, user)
    return Response(_session_payload(token, user, first_login))


@extend_schema(tags=TAG, request=None, responses={204: None}, summary='Log out',
               description='The token stops working.')
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    end_app_session(request)
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=TAG, responses={200: s.AuthUserSerializer}, summary='The signed-in user',
               description='Call at app start. 401 means the token has expired: sign in again. '
                           'Check terms_accepted before showing anything else.')
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def session(request):
    return Response(s.AuthUserSerializer(request.user).data)


# ── Google / Apple ────────────────────────────────────────────────

@extend_schema(tags=TAG, auth=[], request=s.SocialTokenSerializer, responses={200: s.SocialTokenResultSerializer},
               summary='Sign in with Google or Apple',
               description='Send the ID token from the Google or Apple SDK. status "signed_in": the reply has '
                           'the token. status "signup_required": a new account; show the finish-signup screen '
                           '(email and name prefilled) and send it to /auth/social/signup/ with signup_token. '
                           'An existing account with the same verified address is signed in.')
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def social_token(request):
    data = _validated(s.SocialTokenSerializer, request)
    result = social.token_login(request, data['provider'], data['id_token'],
                                access_token=data.get('access_token'), client_id=data.get('client_id'))
    return Response(s.SocialTokenResultSerializer(result).data)


@extend_schema(tags=TAG, auth=[], request=s.SocialSignupSerializer, responses={200: s.SessionSerializer},
               summary='Finish signing up with Google or Apple',
               description='Same fields and rules as the website\'s "Almost done" page. Error code '
                           'signup_expired (409): start again with /auth/social/token/.')
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def social_signup(request):
    data = _validated(s.SocialSignupSerializer, request)
    result = social.finish_signup(request, data['signup_token'], data)
    return Response(_session_payload(result['token'], result['user'], first_login=True))


# ── Role ──────────────────────────────────────────────────────────

@extend_schema(tags=TAG, request=s.RoleSerializer, responses={200: s.AuthUserSerializer},
               summary='Choose rider or driver')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def role(request):
    data = _validated(s.RoleSerializer, request)
    accounts.set_role(request.user, data['role'])
    return Response(s.AuthUserSerializer(request.user).data)


# ── Email ─────────────────────────────────────────────────────────

@extend_schema(tags=TAG, request=None, responses={200: s.MessageSerializer},
               summary='Send the email verification link',
               description='To the pending address if an email change is waiting. The link opens the '
                           'website, which confirms the address.')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def send_verification(request):
    user = request.user
    if accounts.is_email_address_verified(user):
        return Response(s.message('Already verified'))
    target_email = accounts.verification_target(user)
    check_rate_limit(request, 'send-verification', extra_key=str(user.id), per_ip=False)
    check_rate_limit(request, 'email-confirm-target', extra_key=target_email.lower(), per_ip=False)
    try:
        accounts.send_verification_email(request._request, user)
    except Exception:
        logger.exception('API send_verification failed for user %s', user.id)
        raise EmailNotSent()
    return Response(s.message('Verification email sent successfully'))


@extend_schema(tags=TAG, request=s.ChangeEmailSerializer, responses={200: s.MessageSerializer},
               summary='Change the sign-in email',
               description='Needs the password. The new address takes over once its emailed link is '
                           'confirmed; until then it shows as pending_email and email_verified is false.')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def change_email(request):
    data = _validated(s.ChangeEmailSerializer, request)
    user = request.user
    new_email, password = data['new_email'].lower(), data['password']
    if not new_email or not password:
        raise FieldError(None, 'Email and password are required.', code='required')
    check_rate_limit(request, 'change-email', extra_key=str(user.id), per_ip=False)
    try:
        accounts.check_email_change(user, new_email, password)
    except accounts.AccountError as e:
        raise _account_error(e)
    check_rate_limit(request, 'email-confirm-target', extra_key=new_email, per_ip=False)
    try:
        accounts.start_email_change(request._request, user, new_email)
    except Exception:
        logger.exception('API change_email failed for user %s', user.id)
        raise EmailNotSent()
    return Response(s.message(f'Verification email sent to {new_email}'))


# ── Passwords ─────────────────────────────────────────────────────

@extend_schema(tags=TAG, auth=[], request=s.ForgotPasswordSerializer, responses={200: s.MessageSerializer},
               summary='Forgot password',
               description='Always the same reply, whether or not the account exists. The emailed link '
                           f'opens the website and is valid for {settings.PASSWORD_RESET_LINK_MINUTES} minutes.')
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def forgot_password(request):
    email = _validated(s.ForgotPasswordSerializer, request)['email'].lower()
    blocked = rate_limited(request, 'forgot-password-ip')
    if email:
        blocked = rate_limited(request, 'forgot-password-email', extra_key=email) or blocked
    if not blocked:
        accounts.send_password_reset_email(request._request, email)
    # Same reply when throttled, as on the website: nothing to learn from it.
    return Response(s.message(accounts.PASSWORD_RESET_SENT_MESSAGE))


@extend_schema(tags=TAG, auth=[], request=s.ResetPasswordSerializer, responses={200: s.MessageSerializer},
               summary='Set a new password from a reset link',
               description='For when the app opens the /reset-password/<token>/ link itself. Error codes: '
                           'link_expired, link_invalid, account_closed (all 400).')
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def reset_password(request):
    data = _validated(s.ResetPasswordSerializer, request)
    try:
        user = accounts.user_for_password_reset(data['token'])
        accounts.reset_password(user, data['new_password'], data['confirm_password'])
    except accounts.AccountError as e:
        raise _account_error(e)
    return Response(s.message('Your password has been reset. Please sign in with your new password.'))


@extend_schema(tags=TAG, request=s.ChangePasswordSerializer, responses={200: s.MessageSerializer},
               summary='Change password',
               description='This token stays signed in; every other session (website and app) ends.')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def change_password(request):
    data = _validated(s.ChangePasswordSerializer, request)
    # Counted before the check, so a stolen token can't be used to guess
    # the current password (audit S-15).
    check_rate_limit(request, 'change-password', extra_key=str(request.user.id), per_ip=False)
    try:
        accounts.change_password(request.user, data['current_password'], data['new_password'],
                                 data['confirm_password'])
    except accounts.AccountError as e:
        raise _account_error(e)
    keep_app_session(request)
    return Response(s.message('Your password has been changed.'))


# ── Terms ─────────────────────────────────────────────────────────

@extend_schema(tags=TAG, methods=['GET'], responses={200: s.TermsStatusSerializer},
               summary='Terms and Privacy Policy status')
@extend_schema(tags=TAG, methods=['POST'], request=s.AcceptTermsSerializer, responses={200: s.TermsStatusSerializer},
               summary='Accept the current Terms and Privacy Policy')
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def terms(request):
    user = request.user
    if request.method == 'POST' and not accounts.has_accepted_current_terms(user):
        if not _validated(s.AcceptTermsSerializer, request)['agreed_to_terms']:
            raise FieldError('agreed_to_terms', 'Please tick the box to accept the Terms & Conditions '
                             'and the Privacy Policy.', code='required')
        accounts.record_legal_acceptance(user, LegalAcceptance.SOURCE_REACCEPT, _consent(request))
    return Response(s.terms_status(request, user))


# ── Account deletion and data ─────────────────────────────────────

@extend_schema(tags=TAG, request=None, responses={200: s.MessageSerializer},
               summary='Delete my account',
               description='Emails a confirmation link; the account closes when it is clicked, and is '
                           f'deleted {settings.ACCOUNT_DELETION_GRACE_DAYS} days later.')
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def delete_account(request):
    user = request.user
    # Same limit as the website's delete links, per account (audit S-15).
    check_rate_limit(request, 'delete-account-email', extra_key=user.email.lower(), per_ip=False)
    try:
        accounts.send_delete_account_email(request._request, user)
    except Exception:
        logger.exception('API delete_account failed for user %s', user.id)
        raise EmailNotSent('Could not send confirmation email. Please try again later.')
    return Response(s.message(
        f'A confirmation link has been sent to {user.mail_to}. Click it to delete your account.'))


@extend_schema(tags=TAG, responses={200: OpenApiResponse(description='Everything held about the account, as JSON.')},
               summary='Download my data')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def my_data(request):
    user = request.user
    check_rate_limit(request, 'data-export', extra_key=str(user.id), per_ip=False)
    response = Response(accounts.build_data_export(user))
    response['Content-Disposition'] = f'attachment; filename="komute-my-data-{timezone.now():%Y-%m-%d}.json"'
    return response
