"""Sign in with Google / Apple from the app, through allauth.headless.

The app signs in with the platform SDK and sends us the ID token. allauth's
own headless views check it (signature, audience, expiry) and run the same
pipeline as the website's buttons: komute.social.SocialAccountAdapter
(linking an existing account by its verified address, refusing closed
accounts) and, for a new account, komute.social.SocialSignupForm (phone,
date of birth, Terms, optional contact email for Apple relay addresses).

The views here only translate: our JSON in, allauth's reply out in the API's
format. The first step returns either a session token, or a short-lived
signup token that holds the pending sign-in until /auth/social/signup/."""
import json

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.headless.constants import Client
from allauth.headless.socialaccount.views import ProviderSignupView, ProviderTokenView
from allauth.socialaccount.adapter import get_adapter as get_socialaccount_adapter
from allauth.socialaccount.models import SocialApp, SocialLogin
from django.contrib.auth import get_user_model
from rest_framework import exceptions, serializers, status

from api.common.authentication import TOKEN_HEADER
from api.v1.auth.sessions import load_session
from komute.models import is_private_relay
from komute.social import SocialSignupForm


class KomuteProviderSignupView(ProviderSignupView):
    """allauth's provider signup with our finish-signup form (allauth's
    default form has none of our fields, and our adapter's save_user needs them)."""
    input_class = SocialSignupForm


_token_view = ProviderTokenView.as_api_view(client=Client.APP)
_signup_view = KomuteProviderSignupView.as_api_view(client=Client.APP)


class SocialSignInFailed(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = 'social_sign_in_failed'
    default_detail = "That sign-in didn't work. Please try again."


class SignupExpired(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = 'signup_expired'
    default_detail = 'This sign-up has expired. Please sign in with Google or Apple again.'


def _call(view, request, body, session_token=None):
    """Runs an allauth.headless view on this request with `body` as its JSON.
    Returns (status, parsed JSON). DRF has read the request body already, so
    the view is handed ours; the X-Session-Token it sees is `session_token`."""
    django_request = request._request
    django_request._body = json.dumps(body).encode()
    django_request.META['CONTENT_TYPE'] = 'application/json'
    header = 'HTTP_' + TOKEN_HEADER.upper().replace('-', '_')
    if session_token:
        django_request.META[header] = session_token
    else:
        django_request.META.pop(header, None)
    django_request.__dict__.pop('headers', None)  # rebuilt from META on next use
    try:
        response = view(django_request)
    except ImmediateHttpResponse as e:
        response = e.response
    try:
        data = json.loads(response.content or b'{}')
    except ValueError:
        data = {}
    return response.status_code, data


def _raise_for(status_code, data):
    """allauth's error reply as an API error."""
    if 'error' in data:
        # Already ours (komute.social: closed account).
        error = data['error']
        exc = exceptions.APIException(error['message'], code=error['code'])
        exc.status_code = status_code
        raise exc
    if status_code == 400 and data.get('errors'):
        fields = {}
        for err in data['errors']:
            fields.setdefault(err.get('param') or 'non_field_errors', []).append(err['message'])
        raise serializers.ValidationError(fields)
    if status_code == 409:
        raise SignupExpired()
    if status_code == 403:
        raise exceptions.PermissionDenied('Sign-up is closed.', code='signup_closed')
    raise SocialSignInFailed()


def _pending_sociallogin(session_token):
    session = load_session(session_token)
    if session is None:
        return None
    data = session.get('socialaccount_sociallogin')
    try:
        return SocialLogin.deserialize(data) if data else None
    except ValueError:
        return None


def _signed_in(data, first_login, signup_token=None):
    token = data.get('meta', {}).get('session_token') or signup_token
    user = get_user_model().objects.get(pk=data['data']['user']['id'])
    return {'status': 'signed_in', 'token': token, 'first_login': first_login, 'user': user}


def token_login(request, provider_id, id_token, access_token=None, client_id=None):
    """Step 1. Returns the result dict for SocialTokenResultSerializer."""
    if client_id is None:
        try:
            # Google: the app asks for a token for our web client
            # (serverClientId), so its audience is the website's client ID.
            client_id = get_socialaccount_adapter().get_provider(request._request, provider_id).app.client_id
        except SocialApp.DoesNotExist:
            client_id = None
    if client_id is None:
        raise SocialSignInFailed(f'Sign-in with {provider_id.title()} is not available.',
                                 code='provider_unavailable')

    token = {'client_id': client_id, 'id_token': id_token}
    if access_token:
        token['access_token'] = access_token
    status_code, data = _call(_token_view, request,
                              {'provider': provider_id, 'process': 'login', 'token': token})

    if status_code == 200:
        return _signed_in(data, first_login=False)
    flows = data.get('data', {}).get('flows', [])
    if status_code == 401 and any(f.get('id') == 'provider_signup' and f.get('is_pending') for f in flows):
        signup_token = data.get('meta', {}).get('session_token')
        sociallogin = _pending_sociallogin(signup_token) if signup_token else None
        if sociallogin is None:
            raise SocialSignInFailed()
        email = sociallogin.user.email or ''
        return {
            'status': 'signup_required',
            'signup_token': signup_token,
            'email': email,
            'full_name': sociallogin.user.full_name or '',
            'asks_contact_email': is_private_relay(email),
        }
    _raise_for(status_code, data)


def finish_signup(request, signup_token, fields):
    """Step 2, for a new account. Returns the signed-in result dict."""
    if _pending_sociallogin(signup_token) is None:
        raise SignupExpired()
    body = {
        'full_name': fields['full_name'],
        'country_code': fields['country_code'],
        'phone_number': fields['phone_number'],
        'dob': fields['dob'],
        'agreed_to_terms': '1' if fields['agreed_to_terms'] else '',
        'contact_email': fields['contact_email'],
    }
    status_code, data = _call(_signup_view, request, body, session_token=signup_token)
    if status_code == 200:
        return _signed_in(data, first_login=True, signup_token=signup_token)
    _raise_for(status_code, data)
