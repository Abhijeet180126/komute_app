"""
Throwaway settings for local regression testing only.

Overrides the Postgres/SES/Redis-backed settings with local, disposable
equivalents (sqlite file in the OS temp dir, locmem email, in-memory channel
layer, locmem cache) so the full app can be exercised without any external
services installed. Also forces off the DEBUG-gated HTTPS-only settings
(SECURE_SSL_REDIRECT etc.) so the test Client's plain in-process requests
aren't redirect-looped. Never used for real deployments -- not referenced by
manage.py, asgi.py, wsgi.py, or any cron/deploy script.

Usage: python manage.py <command> --settings=config.settings_regression
"""
import tempfile
from pathlib import Path

from .settings import *  # noqa: F401,F403

_REGRESSION_DB = Path(tempfile.gettempdir()) / 'komute_regression.sqlite3'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(_REGRESSION_DB),
    }
}

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}

# config/settings.py gates SECURE_SSL_REDIRECT / SESSION_COOKIE_SECURE /
# CSRF_COOKIE_SECURE on `not DEBUG` (see "Production security hardening",
# Finding 9). DEBUG is inherited from the real .env here — if it's False,
# those turn on and redirect-loop the test Client's plain in-process
# requests (no TLS involved in tests at all). Force them off for this
# throwaway settings module regardless of DEBUG.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# django.core.cache's Redis backend isn't reachable in this throwaway
# environment either (see CACHES in settings.py) — request throttling
# (komute.views._rate_limited) fails open on a cache error, so this doesn't
# disable the checks, it just means they never trigger during tests.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

# django-axes' AxesStandaloneBackend.authenticate() requires a `request`
# argument; Django's test Client.login() shortcut calls authenticate()
# without one, which raises AxesBackendRequestParameterRequired and breaks
# every test that logs in via Client.login(). Login-attempt lockout has
# nothing to do with what these tests exercise, so it's switched off here
# (test-only) rather than worked around per-test.
AXES_ENABLED = False
