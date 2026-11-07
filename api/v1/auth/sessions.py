"""App sessions: what the login token is.

A token is the key of a Django session that holds the signed-in user, the
same kind of session the website keeps in its cookie, and the same kind
allauth.headless hands out for Google/Apple sign-in. So one authentication
class (api.common.authentication) reads every token, and logging out,
closing the account or changing the password ends it."""
from importlib import import_module

from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, login, user_logged_out

MODEL_BACKEND = 'django.contrib.auth.backends.ModelBackend'


def _session_store(session_key=None):
    return import_module(settings.SESSION_ENGINE).SessionStore(session_key=session_key)


def load_session(session_key):
    """The saved session with this key, or None."""
    if not session_key or not _session_store().exists(session_key):
        return None
    return _session_store(session_key)


def start_app_session(request, user):
    """Signs `user` in on a new session and returns its key (the token).

    Django's login() does the work, so everything that hooks into a login
    runs as on the website (last_login, the axes reset, allauth's signals).
    The request's own cookie session is put back untouched: a browser that
    calls the API isn't signed in to the website by it, and no cookie is set."""
    django_request = getattr(request, '_request', request)
    cookie_session = django_request.session
    csrf_flag = django_request.META.get('CSRF_COOKIE_NEEDS_UPDATE', False)
    django_request.session = _session_store()
    try:
        login(django_request, user, backend=getattr(user, 'backend', None) or MODEL_BACKEND)
        django_request.session.save()
        return django_request.session.session_key
    finally:
        django_request.session = cookie_session
        # login() rotates the CSRF token, which would set a csrftoken cookie.
        django_request.META['CSRF_COOKIE_NEEDS_UPDATE'] = csrf_flag


def end_app_session(request):
    """Logout: the token stops working. Sends user_logged_out as Django's
    logout() does (axes records it)."""
    django_request = getattr(request, '_request', request)
    user_logged_out.send(sender=request.user.__class__, request=django_request, user=request.user)
    request.auth.delete()


def keep_app_session(request):
    """After a password change: the other sessions (website and app) end,
    this one stays signed in. Like update_session_auth_hash() on the website."""
    session = request.auth
    session[HASH_SESSION_KEY] = request.user.get_session_auth_hash()
    session.save()
