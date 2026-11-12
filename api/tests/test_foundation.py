"""Module 0 (foundation) tests: the API_ENABLED switch, token auth, error
format, pagination, hashids, throttles, permissions, schema/docs, and the
website middleware's behaviour around /api/.

    manage.py test api --settings=config.settings_regression
"""
import importlib
from unittest import mock

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import clear_url_caches, resolve, Resolver404
from django.utils import timezone
from rest_framework.test import APIClient

from drf_spectacular.extensions import OpenApiAuthenticationExtension

import config.urls
from api.common.authentication import SessionTokenAuthentication
from api.common.fields import decode_hashid
from api.common.throttles import AnonBurstThrottle, UserBurstThrottle
from api.tests.base import PASSWORD, ApiTestCase, auth_client, make_user, session_token_for
from komute import views as web_views
from komute.hashid_utils import _hashids, encode_id
from komute.models import LegalAcceptance

HEALTH = '/api/v1/health/'
WHOAMI = '/api/v1/_test/whoami/'


def _reload_site_urls():
    importlib.reload(config.urls)
    clear_url_caches()


class ApiSwitchTests(SimpleTestCase):
    """API_ENABLED=False (the default) must leave the site's URLs exactly as
    they were: no /api/ route at all."""

    def tearDown(self):
        _reload_site_urls()  # back to whatever the real settings say

    @override_settings(API_ENABLED=False)
    def test_off_registers_nothing(self):
        self.assertEqual(config.urls.api_urlpatterns(), [])
        _reload_site_urls()
        with self.assertRaises(Resolver404):
            resolve(HEALTH, urlconf='config.urls')
        # The website's own routes are untouched.
        self.assertEqual(resolve('/login/', urlconf='config.urls').url_name, 'login')

    @override_settings(API_ENABLED=True)
    def test_on_mounts_api(self):
        self.assertEqual(len(config.urls.api_urlpatterns()), 1)
        _reload_site_urls()
        match = resolve(HEALTH, urlconf='config.urls')
        self.assertEqual(match.view_name, 'api:v1:health')
        self.assertEqual(resolve('/login/', urlconf='config.urls').url_name, 'login')


class HealthTests(ApiTestCase):
    def test_health_needs_no_token(self):
        response = self.client.get(HEALTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok', 'api_version': 'v1'})

    def test_health_ignores_a_bad_token(self):
        response = self.client.get(HEALTH, HTTP_X_SESSION_TOKEN='nope')
        self.assertEqual(response.status_code, 200)

    def test_wrong_method_is_json_405(self):
        response = self.client.post(HEALTH)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()['error']['code'], 'method_not_allowed')


class TokenAuthTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com')

    def test_no_token_is_401(self):
        response = self.client.get(WHOAMI)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['WWW-Authenticate'], 'X-Session-Token')
        self.assertEqual(response.json()['error']['code'], 'not_authenticated')

    def test_valid_token(self):
        response = auth_client(self.user).get(WHOAMI)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'id': encode_id(self.user.pk), 'email': 'rider@example.com'})

    def test_unknown_token_is_401(self):
        response = self.client.get(WHOAMI, HTTP_X_SESSION_TOKEN='not-a-session')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'invalid_token')

    def test_password_change_ends_app_sessions(self):
        client = auth_client(self.user)
        self.user.set_password('AnotherPass456!y')
        self.user.save()
        response = client.get(WHOAMI)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'invalid_token')

    def test_session_without_password_hash_is_refused(self):
        token = session_token_for(self.user)
        session = Session.objects.get(session_key=token)
        data = session.get_decoded()
        data.pop('_auth_user_hash')
        Session.objects.save(token, data, session.expire_date)
        response = self.client.get(WHOAMI, HTTP_X_SESSION_TOKEN=token)
        self.assertEqual(response.status_code, 401)

    def test_inactive_user_is_401(self):
        client = auth_client(self.user)
        self.user.is_active = False
        self.user.save()
        self.assertEqual(client.get(WHOAMI).status_code, 401)

    def test_deleted_session_is_401(self):
        token = session_token_for(self.user)
        Session.objects.filter(session_key=token).delete()
        self.assertEqual(self.client.get(WHOAMI, HTTP_X_SESSION_TOKEN=token).status_code, 401)

    def test_website_cookie_does_not_authenticate_the_api(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(WHOAMI).status_code, 401)

    def test_token_from_allauth_headless_login(self):
        """End to end: allauth.headless's app login hands out a token that
        this API accepts (module 1 will put its own endpoint in front)."""
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)
        client = APIClient()
        response = client.post('/_allauth/app/v1/auth/login',
                               {'email': self.user.email, 'password': PASSWORD}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()['meta']['session_token']
        response = client.get(WHOAMI, HTTP_X_SESSION_TOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['email'], self.user.email)


class ErrorFormatTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com')
        self.client = auth_client(self.user)

    def test_validation_error_shape(self):
        response = self.client.post('/api/v1/_test/echo/', {'name': 'x' * 11, 'ref': 'bad'})
        self.assertEqual(response.status_code, 400)
        error = response.json()['error']
        self.assertEqual(error['code'], 'invalid')
        self.assertEqual(set(error['fields']), {'name', 'ref', 'user'})
        self.assertEqual(error['fields']['ref'], ['Not a valid id.'])
        self.assertEqual(error['fields']['user'], ['This field is required.'])
        self.assertTrue(error['message'])

    def test_non_field_error(self):
        response = self.client.post('/api/v1/_test/echo/', {
            'name': 'clash', 'ref': encode_id(5), 'user': encode_id(self.user.pk)})
        self.assertEqual(response.status_code, 400)
        error = response.json()['error']
        self.assertEqual(error['fields'], {'non_field_errors': ['These values clash.']})
        self.assertEqual(error['message'], 'These values clash.')

    def test_hashids_round_trip(self):
        response = self.client.post('/api/v1/_test/echo/', {
            'name': 'ok', 'ref': encode_id(5), 'user': encode_id(self.user.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'name': 'ok', 'ref_pk': 5, 'user_pk': self.user.pk,
                                           'user': encode_id(self.user.pk)})

    def test_unknown_related_id_does_not_echo_the_database_id(self):
        response = self.client.post('/api/v1/_test/echo/', {
            'name': 'ok', 'ref': encode_id(5), 'user': encode_id(987654)})
        self.assertEqual(response.json()['error']['fields']['user'], ['Not found.'])
        self.assertNotIn('987654', response.content.decode())

    def test_unhandled_error_is_json_500_and_logged(self):
        with self.assertLogs('komute.api', level='ERROR') as logs:
            response = self.client.get('/api/v1/_test/boom/')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['error']['code'], 'server_error')
        self.assertNotIn('kaboom', response.content.decode())
        self.assertIn('kaboom', '\n'.join(logs.output))

    def test_django_404_has_a_generic_message(self):
        response = self.client.get('/api/v1/_test/django-404/')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {'error': {'code': 'not_found', 'message': 'Not found.',
                                                     'fields': {}}})

    def test_unknown_api_url_is_json_404(self):
        for url in ('/api/v1/nope/', '/api/v2/', '/api/v1/health'):
            response = APIClient().get(url)
            self.assertEqual(response.status_code, 404, url)
            self.assertEqual(response.json()['error']['code'], 'not_found')


class HashidTests(SimpleTestCase):
    def test_decode(self):
        self.assertEqual(decode_hashid(encode_id(42)), 42)
        for bad in ('', None, 42, 'zzzz', encode_id(42) + 'x'):
            self.assertIsNone(decode_hashid(bad), bad)

    def test_multi_number_hashid_is_refused(self):
        # decode_id() alone would return 42 for this.
        self.assertIsNone(decode_hashid(_hashids.encode(42, 7)))


class PaginationTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.client = auth_client(make_user('rider@example.com'))

    def test_pages(self):
        body = self.client.get('/api/v1/_test/numbers/').json()
        self.assertEqual(body['count'], 45)
        self.assertEqual(body['results'], list(range(1, 21)))
        self.assertIn('page=2', body['next'])
        self.assertIsNone(body['previous'])
        body = self.client.get('/api/v1/_test/numbers/?page=3').json()
        self.assertEqual(body['results'], list(range(41, 46)))
        self.assertIsNone(body['next'])

    def test_page_size_is_capped(self):
        with mock.patch('api.common.pagination.StandardPagination.max_page_size', 30):
            body = self.client.get('/api/v1/_test/numbers/?page_size=1000').json()
        self.assertEqual(len(body['results']), 30)

    def test_page_out_of_range(self):
        response = self.client.get('/api/v1/_test/numbers/?page=9')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error']['code'], 'not_found')


class ThrottleTests(ApiTestCase):
    LIMITED = '/api/v1/_test/action-limited/'

    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com')
        self.client = auth_client(self.user)

    @override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'contact': (2, 60)})
    def test_action_bucket(self):
        self.assertEqual(self.client.post(self.LIMITED).status_code, 200)
        self.assertEqual(self.client.post(self.LIMITED).status_code, 200)
        response = self.client.post(self.LIMITED)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '60')
        error = response.json()['error']
        self.assertEqual(error['code'], 'throttled')
        self.assertEqual(error['retry_after'], 60)
        self.assertEqual(error['message'], 'Too many attempts. Please try again later.')

    @override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'contact': (2, 60)})
    def test_action_bucket_is_shared_with_the_website(self):
        web_request = RequestFactory().post('/contact-us/', REMOTE_ADDR='127.0.0.1')
        self.assertFalse(web_views._rate_limited(web_request, 'contact'))
        self.assertFalse(web_views._rate_limited(web_request, 'contact'))
        self.assertEqual(self.client.post(self.LIMITED, REMOTE_ADDR='127.0.0.1').status_code, 429)

    def test_action_bucket_fails_open_without_cache(self):
        with mock.patch('api.common.throttles.cache.add', side_effect=ConnectionError):
            self.assertEqual(self.client.post(self.LIMITED).status_code, 200)

    def test_burst_throttle_counts_the_address_nginx_saw(self):
        with mock.patch.object(AnonBurstThrottle, 'THROTTLE_RATES', {'anon': '2/min'}), \
                mock.patch.object(UserBurstThrottle, 'THROTTLE_RATES', {'user': '100/min'}):
            client = APIClient()
            # The client picks the first entries; Nginx appends the last.
            for spoof in ('1.1.1.1', '2.2.2.2'):
                response = client.get(HEALTH, HTTP_X_FORWARDED_FOR=f'{spoof}, 9.9.9.9')
                self.assertEqual(response.status_code, 200)
            response = client.get(HEALTH, HTTP_X_FORWARDED_FOR='3.3.3.3, 9.9.9.9')
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json()['error']['code'], 'throttled')
            # Another real address has its own count.
            response = client.get(HEALTH, HTTP_X_FORWARDED_FOR='9.9.9.8')
            self.assertEqual(response.status_code, 200)

    def test_burst_throttle_fails_open_without_cache(self):
        broken = mock.Mock()
        broken.get.side_effect = ConnectionError
        with mock.patch.object(AnonBurstThrottle, 'cache', broken), \
                mock.patch.object(UserBurstThrottle, 'cache', broken):
            with self.assertLogs('komute.api', level='WARNING'):
                self.assertEqual(APIClient().get(HEALTH).status_code, 200)


class PermissionTests(ApiTestCase):
    def test_terms_gate(self):
        user = make_user('new@example.com', accepted_terms=False)
        response = auth_client(user).get('/api/v1/_test/terms-gate/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'terms_not_accepted')
        LegalAcceptance.objects.create(user=user, terms_version=settings.TERMS_VERSION,
                                       privacy_version=settings.PRIVACY_VERSION,
                                       source=LegalAcceptance.SOURCE_REACCEPT)
        self.assertEqual(auth_client(user).get('/api/v1/_test/terms-gate/').status_code, 200)

    def test_old_terms_version_is_not_enough(self):
        user = make_user('old@example.com', accepted_terms=False)
        LegalAcceptance.objects.create(user=user, terms_version='2000-01-01',
                                       privacy_version=settings.PRIVACY_VERSION,
                                       source=LegalAcceptance.SOURCE_SIGNUP)
        self.assertEqual(auth_client(user).get('/api/v1/_test/terms-gate/').status_code, 403)

    def test_email_gate(self):
        user = make_user('unverified@example.com', email_verified=False)
        response = auth_client(user).get('/api/v1/_test/email-gate/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'email_unverified')
        self.assertEqual(auth_client(make_user('ok@example.com')).get('/api/v1/_test/email-gate/').status_code,
                         200)

    def test_driver_gate(self):
        rider = make_user('rider@example.com')
        response = auth_client(rider).get('/api/v1/_test/driver-gate/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'driver_not_verified')
        unverified = make_user('driver1@example.com', role='driver')
        self.assertEqual(auth_client(unverified).get('/api/v1/_test/driver-gate/').status_code, 403)
        verified = make_user('driver2@example.com', role='driver', driver_verified=True,
                             driver_verified_at=timezone.now())
        self.assertEqual(auth_client(verified).get('/api/v1/_test/driver-gate/').status_code, 200)

    def test_unauthenticated_is_401_not_403(self):
        self.assertEqual(APIClient().get('/api/v1/_test/terms-gate/').status_code, 401)

    def test_is_owner(self):
        owner = make_user('owner@example.com')
        other = make_user('other@example.com')
        acceptance = LegalAcceptance.objects.get(user=owner)
        url = f'/api/v1/_test/acceptance/{encode_id(acceptance.pk)}/'
        self.assertEqual(auth_client(owner).get(url).status_code, 200)
        response = auth_client(other).get(url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'not_owner')
        self.assertEqual(auth_client(owner).get('/api/v1/_test/acceptance/bad/').status_code, 400)


class WebsiteMiddlewareTests(ApiTestCase):
    def test_terms_middleware_does_not_redirect_api_calls(self):
        """A browser signed in to the website, with new Terms to accept, still
        gets JSON from the API rather than a redirect to /accept-terms/."""
        user = make_user('pending@example.com', accepted_terms=False)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/profile/').status_code, 302)  # the website still asks
        self.assertEqual(self.client.get(HEALTH).status_code, 200)

    def test_signed_in_api_responses_are_not_cached(self):
        response = auth_client(make_user('rider@example.com')).get(WHOAMI)
        self.assertIn('no-store', response['Cache-Control'])


class SchemaAndDocsTests(ApiTestCase):
    SCHEMA = '/api/v1/schema/'
    DOCS = '/api/v1/docs/'

    def test_staff_only_by_default(self):
        self.assertEqual(APIClient().get(self.SCHEMA).status_code, 401)
        rider = make_user('rider@example.com')
        self.assertEqual(auth_client(rider).get(self.SCHEMA).status_code, 403)
        self.client.force_login(rider)
        self.assertEqual(self.client.get(self.DOCS).status_code, 403)

    def test_staff_signed_in_on_the_website(self):
        staff = make_user('staff@example.com', is_staff=True)
        self.client.force_login(staff)
        response = self.client.get(self.SCHEMA, HTTP_ACCEPT='application/vnd.oai.openapi+json')
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertIn('/api/v1/health/', schema['paths'])
        # The catch-all 404 and the test-only views aren't documented.
        self.assertFalse([p for p in schema['paths'] if not p.startswith('/api/v1/') or '*' in p])
        docs = self.client.get(self.DOCS)
        self.assertEqual(docs.status_code, 200)
        # Swagger UI from our own static files, not a CDN (CSP).
        self.assertIn('drf_spectacular_sidecar', docs.content.decode())
        self.assertNotIn('cdn.jsdelivr.net', docs.content.decode())

    def test_token_scheme_is_documented(self):
        extension = OpenApiAuthenticationExtension.get_match(SessionTokenAuthentication())
        self.assertEqual(extension.name, 'sessionToken')
        self.assertEqual(extension.get_security_definition(None),
                         {'type': 'apiKey', 'in': 'header', 'name': 'X-Session-Token',
                          'description': 'The session token returned by login.'})

    @override_settings(API_DOCS_PUBLIC=True)
    def test_public_when_switched_on(self):
        self.assertEqual(APIClient().get(self.SCHEMA).status_code, 200)
