"""Shared helpers for the API tests.

API tests run against api.tests.urls, which mounts the API whatever
API_ENABLED says, plus a few test-only views."""
from datetime import date
from importlib import import_module

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from komute.models import CustomUser, LegalAcceptance

PASSWORD = 'TestPass123!x'


def make_user(email, role='rider', accepted_terms=True, **kwargs):
    defaults = dict(full_name=f'Test {email}', dob=date(1995, 1, 1), phone=None, email_verified=True)
    defaults.update(kwargs)
    user = CustomUser.objects.create_user(username=email, email=email, password=PASSWORD,
                                          role=role, **defaults)
    if accepted_terms:
        LegalAcceptance.objects.create(
            user=user, terms_version=settings.TERMS_VERSION,
            privacy_version=settings.PRIVACY_VERSION, source=LegalAcceptance.SOURCE_SIGNUP)
    return user


def session_token_for(user):
    """A token as allauth.headless issues one: the key of a saved session
    for the user (allauth's SessionTokenStrategy)."""
    store = import_module(settings.SESSION_ENGINE).SessionStore()
    store[SESSION_KEY] = str(user.pk)
    store[BACKEND_SESSION_KEY] = 'django.contrib.auth.backends.ModelBackend'
    store[HASH_SESSION_KEY] = user.get_session_auth_hash()
    store.save()
    return store.session_key


def auth_client(user):
    client = APIClient()
    client.credentials(HTTP_X_SESSION_TOKEN=session_token_for(user))
    return client


@override_settings(ROOT_URLCONF='api.tests.urls')
class ApiTestCase(APITestCase):
    def setUp(self):
        super().setUp()
        # Throttle counts live in the cache; start each test from zero.
        cache.clear()
        self.addCleanup(cache.clear)
