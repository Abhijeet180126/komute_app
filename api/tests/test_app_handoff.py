"""Google / Apple sign-in for the app through the website (api.v1.auth.app_handoff).

    manage.py test api.tests.test_app_handoff --settings=config.settings_regression"""
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

from django.test import Client, override_settings
from rest_framework.test import APIClient

from api.tests.base import ApiTestCase, make_user

A = '/api/v1/auth/app/'
GOOGLE = {'google': {'APPS': [{'client_id': 'test-client', 'secret': 'test-secret'}],
                     'SCOPE': ['profile', 'email']}}


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


class AppHandoffTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('ada@example.com')
        self.verifier, self.challenge = pkce()

    def _code(self):
        browser = Client()
        browser.force_login(self.user)
        res = browser.get(A + 'finish/', {'challenge': self.challenge})
        self.assertEqual(res.status_code, 302)
        target = res['Location']
        self.assertTrue(target.startswith('komute://auth?'))
        return parse_qs(urlparse(target).query)['code'][0]

    def test_finish_sends_signed_out_browser_to_login(self):
        res = Client().get(A + 'finish/', {'challenge': self.challenge})
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login/', res['Location'])
        self.assertIn('next=', res['Location'])

    def test_bad_challenge_is_refused(self):
        self.assertEqual(Client().get(A + 'finish/', {'challenge': 'short'}).status_code, 400)
        self.assertEqual(Client().get(A + 'start/', {'provider': 'google', 'challenge': 'x'}).status_code, 400)

    def test_exchange_gives_a_working_token_once(self):
        code = self._code()
        res = APIClient().post(A + 'exchange/', {'code': code, 'verifier': self.verifier}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        token = res.json()['token']
        me = APIClient()
        me.credentials(HTTP_X_SESSION_TOKEN=token)
        self.assertEqual(me.get('/api/v1/users/me/').json()['email'], 'ada@example.com')
        again = APIClient().post(A + 'exchange/', {'code': code, 'verifier': self.verifier}, format='json')
        self.assertEqual(again.status_code, 400)

    def test_wrong_verifier_burns_the_code(self):
        code = self._code()
        other, _ = pkce()
        self.assertEqual(APIClient().post(A + 'exchange/', {'code': code, 'verifier': other},
                                          format='json').status_code, 400)
        self.assertEqual(APIClient().post(A + 'exchange/', {'code': code, 'verifier': self.verifier},
                                          format='json').status_code, 400)

    def test_inactive_user_gets_no_session(self):
        code = self._code()
        self.user.is_active = False
        self.user.save()
        self.assertEqual(APIClient().post(A + 'exchange/', {'code': code, 'verifier': self.verifier},
                                          format='json').status_code, 400)

    @override_settings(SOCIALACCOUNT_PROVIDERS=GOOGLE)
    def test_start_goes_to_google_and_returns_to_finish(self):
        res = Client().get(A + 'start/', {'provider': 'google', 'challenge': self.challenge})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res['Location'].startswith('https://accounts.google.com/'))

    @override_settings(SOCIALACCOUNT_PROVIDERS={})
    def test_start_without_keys_says_so(self):
        res = Client().get(A + 'start/', {'provider': 'apple', 'challenge': self.challenge})
        self.assertEqual(res.status_code, 400)
