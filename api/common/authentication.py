"""How the app signs in to the API.

The app logs in through allauth.headless (module 1) and gets a session
token, sent back on every call as the X-Session-Token header. The token is a
Django session key, so logging out or deleting the session revokes it."""
from django.contrib.auth import HASH_SESSION_KEY
from django.utils.crypto import constant_time_compare
from rest_framework.exceptions import AuthenticationFailed

from allauth.headless.contrib.rest_framework.authentication import XSessionTokenAuthentication
from allauth.headless.internal.sessionkit import authenticate_by_x_session_token

TOKEN_HEADER = 'X-Session-Token'


def _session_hash_valid(session, user):
    """The check Django's get_user() makes for the website's cookie: the
    session was made with the user's current password. So changing or
    resetting the password ends the app's sessions as it does the browser's."""
    session_hash = session.get(HASH_SESSION_KEY)
    if not session_hash:
        return False
    if constant_time_compare(session_hash, user.get_session_auth_hash()):
        return True
    # SECRET_KEY_FALLBACKS, as Django allows during a key rotation.
    return any(constant_time_compare(session_hash, fallback)
               for fallback in user.get_session_auth_fallback_hash())


def user_for_token(token):
    """(user, session) for a valid login token, else None. Used for API
    calls and for the app's chat socket."""
    result = authenticate_by_x_session_token(token)
    if result is None or not _session_hash_valid(result[1], result[0]):
        return None
    return result


class SessionTokenAuthentication(XSessionTokenAuthentication):
    """allauth's X-Session-Token authentication, stricter in two ways:

    - a token that is sent but invalid or expired is a 401, so the app knows
      to sign in again (allauth's class treats it as no token at all);
    - the session must match the user's current password (see above).

    Inactive users are refused by allauth's lookup."""

    def authenticate(self, request):
        token = self.get_session_token(request)
        if not token:
            return None
        result = user_for_token(token)
        if result is None:
            raise AuthenticationFailed('Your session has expired. Please sign in again.',
                                       code='invalid_token')
        return result

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when no token is sent.
        return TOKEN_HEADER
