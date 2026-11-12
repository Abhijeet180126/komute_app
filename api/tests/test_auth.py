"""Module 1 (auth and account) tests.

    manage.py test api --settings=config.settings_regression

The rules themselves are the website's (komute.services.accounts, covered by
komute.tests_regression); these check the API applies them the same way,
shares the website's rate-limit buckets, and hands out working tokens."""
import time
from datetime import date
from unittest import mock

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.providers.apple.provider import AppleProvider
from allauth.socialaccount.providers.google.provider import GoogleProvider
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, override_settings
from rest_framework.test import APIClient

from api.tests.base import PASSWORD, ApiTestCase, auth_client, make_user, session_token_for
from komute import views as web_views
from komute.hashid_utils import encode_id
from komute.models import CustomUser, LegalAcceptance, RideSearch
from komute.services import accounts

A = '/api/v1/auth/'
NEW_PASSWORD = 'BrandNewPass456!z'


def _adult_dob(years=30):
    return date(date.today().year - years, 1, 15).isoformat()


def signup_body(**overrides):
    body = {'full_name': 'Ada Lovelace', 'email': 'Ada@Example.com', 'country_code': '+1',
            'phone_number': '4155552671', 'dob': _adult_dob(), 'password': PASSWORD,
            'confirm_password': PASSWORD, 'agreed_to_terms': True}
    body.update(overrides)
    return body


def limits(**buckets):
    """override_settings for some RATE_LIMITS buckets, e.g. limits(signup=(1, 60))."""
    return override_settings(RATE_LIMITS={**settings.RATE_LIMITS,
                                          **{k.replace('_', '-'): v for k, v in buckets.items()}})


def error(response):
    return response.json()['error']


class SignupTests(ApiTestCase):
    def test_signup_signs_in(self):
        response = self.client.post(A + 'signup/', signup_body(), HTTP_USER_AGENT='KomuteApp/1.0')
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue(body['first_login'])
        user = CustomUser.objects.get(email='ada@example.com')
        self.assertEqual(body['user']['id'], encode_id(user.pk))
        self.assertEqual((user.username, user.phone, user.full_name), ('ada@example.com', '+14155552671',
                                                                       'Ada Lovelace'))
        self.assertTrue(body['user']['terms_accepted'])
        self.assertFalse(body['user']['email_verified'])
        acceptance = LegalAcceptance.objects.get(user=user)
        self.assertEqual((acceptance.source, acceptance.terms_version, acceptance.user_agent),
                         (LegalAcceptance.SOURCE_SIGNUP, settings.TERMS_VERSION, 'KomuteApp/1.0'))
        self.assertEqual([m.to for m in mail.outbox], [['ada@example.com']])  # welcome email
        # The token works, and no website cookie was set.
        self.assertNotIn('sessionid', response.cookies)
        self.assertNotIn('csrftoken', response.cookies)
        me = APIClient().get(A + 'session/', HTTP_X_SESSION_TOKEN=body['token'])
        self.assertEqual(me.json()['email'], 'ada@example.com')

    def test_signup_uses_the_website_messages(self):
        response = self.client.post(A + 'signup/', {})
        self.assertEqual(response.status_code, 400)
        fields = error(response)['fields']
        self.assertEqual(fields['full_name'], ['Full name is required.'])
        self.assertEqual(fields['email'], ['Email address is required.'])
        self.assertEqual(fields['password'], ['Password is required.'])
        self.assertEqual(fields['dob'], ['Date of birth is required.'])
        self.assertEqual(fields['agreed_to_terms'],
                         ['You must accept the Terms & Conditions and the Privacy Policy to sign up.'])
        self.assertFalse(CustomUser.objects.exists())

    def test_signup_rules(self):
        make_user('taken@example.com')
        cases = [
            ({'email': 'TAKEN@example.com'}, 'email', 'An account with this email already exists.'),
            ({'dob': _adult_dob(17)}, 'dob', 'You must be at least 18 years old.'),
            ({'country_code': '+999'}, 'country_code', 'Please select a valid country code.'),
            ({'phone_number': '12ab'}, 'phone_number', 'Enter a valid phone number (digits only).'),
            ({'confirm_password': 'different'}, 'confirm_password', 'Passwords do not match.'),
        ]
        for overrides, field, message in cases:
            response = self.client.post(A + 'signup/', signup_body(**overrides))
            self.assertEqual(response.status_code, 400, overrides)
            self.assertEqual(error(response)['fields'][field], [message], overrides)
        weak = self.client.post(A + 'signup/', signup_body(password='short', confirm_password='short'))
        self.assertIn('password', error(weak)['fields'])

    def test_password_is_not_trimmed(self):
        padded = f' {PASSWORD} '
        self.client.post(A + 'signup/', signup_body(password=padded, confirm_password=padded))
        self.assertTrue(CustomUser.objects.get(email='ada@example.com').check_password(padded))

    @limits(signup=(1, 3600))
    def test_signup_rate_limit(self):
        self.assertEqual(self.client.post(A + 'signup/', signup_body()).status_code, 201)
        response = self.client.post(A + 'signup/', signup_body(email='other@example.com'))
        self.assertEqual(response.status_code, 429)
        self.assertFalse(CustomUser.objects.filter(email='other@example.com').exists())

    def test_a_stale_token_does_not_block_signup(self):
        response = self.client.post(A + 'signup/', signup_body(), HTTP_X_SESSION_TOKEN='expired')
        self.assertEqual(response.status_code, 201)


class LoginTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('Rider@Example.com')

    def login(self, email='rider@example.com', password=PASSWORD, **extra):
        return self.client.post(A + 'login/', {'email': email, 'password': password}, **extra)

    def test_login(self):
        response = self.login(email='RIDER@example.COM')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['first_login'])
        self.assertEqual(body['user']['email'], 'Rider@example.com')  # Django lowercases the domain
        self.assertNotIn('sessionid', response.cookies)
        self.assertEqual(APIClient().get(A + 'session/', HTTP_X_SESSION_TOKEN=body['token']).status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)
        self.assertFalse(self.login().json()['first_login'])

    def test_each_login_is_its_own_session(self):
        first, second = self.login().json()['token'], self.login().json()['token']
        self.assertNotEqual(first, second)

    def test_wrong_password(self):
        response = self.login(password='wrong')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(error(response), {'code': 'invalid_credentials', 'message': 'Invalid email or password',
                                           'fields': {}})

    def test_closed_account_cannot_log_in(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(error(self.login())['code'], 'invalid_credentials')

    @limits(login_acct=(2, 300))
    def test_account_bucket_is_shared_with_the_website(self):
        web = Client()  # a browser posting the website's login form
        for _ in range(2):
            web.post('/login/', {'email': 'rider@example.com', 'password': 'wrong'})
        response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(error(response)['code'], 'throttled')

    @override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=2)
    def test_axes_lockout_is_json(self):
        from axes.utils import reset
        self.addCleanup(reset)
        self.login(password='wrong')
        self.login(password='wrong')
        response = self.login()  # right password, but locked out
        self.assertEqual(response.status_code, 429)
        self.assertEqual(error(response)['code'], 'locked_out')
        self.assertEqual(response['Content-Type'], 'application/json')


class LogoutAndSessionTests(ApiTestCase):
    def test_logout_ends_the_token(self):
        user = make_user('rider@example.com')
        token = session_token_for(user)
        other = session_token_for(user)
        response = self.client.post(A + 'logout/', HTTP_X_SESSION_TOKEN=token)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Session.objects.filter(session_key=token).exists())
        self.assertEqual(self.client.get(A + 'session/', HTTP_X_SESSION_TOKEN=token).status_code, 401)
        # Other devices stay signed in.
        self.assertEqual(self.client.get(A + 'session/', HTTP_X_SESSION_TOKEN=other).status_code, 200)

    def test_session_needs_a_token(self):
        self.assertEqual(self.client.get(A + 'session/').status_code, 401)

    def test_session_shows_terms_status(self):
        user = make_user('new@example.com', accepted_terms=False)
        body = auth_client(user).get(A + 'session/').json()
        self.assertFalse(body['terms_accepted'])
        self.assertEqual(body['role'], 'rider')


class TermsTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com', accepted_terms=False)
        LegalAcceptance.objects.create(user=self.user, terms_version='2000-01-01', privacy_version='2000-01-01',
                                       source=LegalAcceptance.SOURCE_SIGNUP)
        self.client = auth_client(self.user)

    def test_gate_and_accept(self):
        # Gated like the website until the new version is accepted...
        response = self.client.post(A + 'role/', {'role': 'driver'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(error(response)['code'], 'terms_not_accepted')
        status_body = self.client.get(A + 'terms/').json()
        self.assertEqual((status_body['accepted'], status_body['is_update'], status_body['terms_version']),
                         (False, True, settings.TERMS_VERSION))
        self.assertTrue(status_body['terms_url'].endswith('/terms-condition/'))
        # ...the box must be ticked...
        response = self.client.post(A + 'terms/', {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(error(response)['code'], 'required')
        # ...and then everything opens.
        self.assertTrue(self.client.post(A + 'terms/', {'agreed_to_terms': True}).json()['accepted'])
        self.assertEqual(LegalAcceptance.objects.filter(user=self.user, source=LegalAcceptance.SOURCE_REACCEPT,
                                                        terms_version=settings.TERMS_VERSION).count(), 1)
        self.assertEqual(self.client.post(A + 'role/', {'role': 'driver'}).status_code, 200)
        # Accepting again adds nothing.
        self.client.post(A + 'terms/', {'agreed_to_terms': True})
        self.assertEqual(LegalAcceptance.objects.filter(user=self.user).count(), 2)

    def test_open_without_the_new_terms(self):
        """As on the website: session, Terms, logout and deletion stay open."""
        self.assertEqual(self.client.get(A + 'session/').status_code, 200)
        self.assertEqual(self.client.post(A + 'account/delete/').status_code, 200)
        for url in ('email/verify/', 'password/change/', 'email/change/'):
            self.assertEqual(self.client.post(A + url, {}).status_code, 403, url)
        self.assertEqual(self.client.get(A + 'account/data/').status_code, 403)


class RoleTests(ApiTestCase):
    def test_role(self):
        user = make_user('rider@example.com')
        client = auth_client(user)
        self.assertEqual(client.post(A + 'role/', {'role': 'driver'}).json()['role'], 'driver')
        for bad in ('admin', '', 'pilot'):
            response = client.post(A + 'role/', {'role': bad})
            self.assertEqual(response.status_code, 400, bad)
        self.assertEqual(error(client.post(A + 'role/', {'role': 'admin'}))['fields']['role'],
                         ['Choose rider or driver.'])
        user.refresh_from_db()
        self.assertEqual(user.role, 'driver')


class EmailTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com', email_verified=False)
        self.client = auth_client(self.user)

    def test_send_verification(self):
        response = self.client.post(A + 'email/verify/')
        self.assertEqual(response.json(), {'message': 'Verification email sent successfully'})
        self.assertEqual(mail.outbox[-1].to, ['rider@example.com'])
        self.assertIn('/accounts/confirm-email/', mail.outbox[-1].body)

    def test_already_verified(self):
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)
        self.assertEqual(self.client.post(A + 'email/verify/').json(), {'message': 'Already verified'})
        self.assertEqual(len(mail.outbox), 0)

    @limits(send_verification=(1, 3600))
    def test_verification_bucket_is_shared_with_the_website(self):
        web = self.client_class()
        web.force_login(self.user)
        self.assertEqual(web.post('/profile/send-verification/').status_code, 200)
        self.assertEqual(self.client.post(A + 'email/verify/').status_code, 429)

    def test_send_failure(self):
        with mock.patch('komute.services.accounts.EmailMultiAlternatives.send', side_effect=OSError), \
                self.assertLogs('komute.api', 'ERROR'):
            response = self.client.post(A + 'email/verify/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(error(response)['code'], 'email_not_sent')

    def test_change_email(self):
        response = self.client.post(A + 'email/change/', {'new_email': 'New@Example.com', 'password': PASSWORD})
        self.assertEqual(response.json(), {'message': 'Verification email sent to new@example.com'})
        self.user.refresh_from_db()
        self.assertEqual((self.user.email, self.user.pending_email), ('rider@example.com', 'new@example.com'))
        self.assertEqual(mail.outbox[-1].to, ['new@example.com'])
        self.assertEqual(self.client.get(A + 'session/').json()['pending_email'], 'new@example.com')

    def test_change_email_rules(self):
        make_user('taken@example.com')
        cases = [
            ({}, 'non_field_errors', 'required', 'Email and password are required.'),
            ({'new_email': 'x@example.com', 'password': 'wrong'}, 'password', 'incorrect_password',
             'Incorrect password.'),
            ({'new_email': 'not-an-email', 'password': PASSWORD}, 'new_email', 'invalid',
             'Enter a valid email address.'),
            ({'new_email': 'taken@example.com', 'password': PASSWORD}, 'new_email', 'email_taken',
             'This email is already in use.'),
            ({'new_email': 'RIDER@example.com', 'password': PASSWORD}, 'new_email', 'invalid',
             'This is already your current email.'),
        ]
        for body, field, code, message in cases:
            err = error(self.client.post(A + 'email/change/', body))
            self.assertEqual((err['code'], err['fields']), (code, {field: [message]}), body)
        self.assertEqual(len(mail.outbox), 0)


class PasswordTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com')

    def test_change_password_keeps_this_session_only(self):
        client = auth_client(self.user)
        other_device = auth_client(self.user)
        web = self.client_class()
        web.force_login(self.user)
        response = client.post(A + 'password/change/', {
            'current_password': PASSWORD, 'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(client.get(A + 'session/').status_code, 200)
        self.assertEqual(other_device.get(A + 'session/').status_code, 401)
        self.assertEqual(web.get('/profile/').status_code, 302)  # signed out of the website
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))

    def test_change_password_rules(self):
        client = auth_client(self.user)
        cases = [
            ({'current_password': 'wrong', 'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD},
             'incorrect_password', 'current_password'),
            ({'current_password': PASSWORD, 'new_password': NEW_PASSWORD, 'confirm_password': 'x'},
             'invalid', 'confirm_password'),
            ({'current_password': PASSWORD, 'new_password': 'short', 'confirm_password': 'short'},
             'invalid', 'new_password'),
        ]
        for body, code, field in cases:
            err = error(client.post(A + 'password/change/', body))
            self.assertEqual(err['code'], code, body)
            self.assertIn(field, err['fields'], body)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    @limits(change_password=(1, 900))
    def test_change_password_rate_limit(self):
        client = auth_client(self.user)
        client.post(A + 'password/change/', {'current_password': 'wrong'})
        self.assertEqual(client.post(A + 'password/change/', {'current_password': PASSWORD}).status_code, 429)

    def test_forgot_password(self):
        sent = "If an account exists for that email, we've sent a link to reset your password."
        response = self.client.post(A + 'password/forgot/', {'email': 'RIDER@example.com'})
        self.assertEqual(response.json(), {'message': sent})
        self.assertEqual(mail.outbox[-1].to, ['rider@example.com'])
        self.assertIn('/reset-password/', mail.outbox[-1].body)
        # Unknown address: same reply, no email.
        self.assertEqual(self.client.post(A + 'password/forgot/', {'email': 'nobody@example.com'}).json(),
                         {'message': sent})
        self.assertEqual(len(mail.outbox), 1)

    @limits(forgot_password_email=(1, 3600))
    def test_forgot_password_throttled_says_the_same(self):
        self.client.post(A + 'password/forgot/', {'email': 'rider@example.com'})
        response = self.client.post(A + 'password/forgot/', {'email': 'rider@example.com'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_reset_password(self):
        token = accounts.make_password_reset_token(self.user)
        old_session = auth_client(self.user)
        response = self.client.post(A + 'password/reset/', {
            'token': token, 'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertEqual(old_session.get(A + 'session/').status_code, 401)
        # The link works once: the password it was bound to has changed.
        again = self.client.post(A + 'password/reset/', {
            'token': token, 'new_password': PASSWORD, 'confirm_password': PASSWORD})
        self.assertEqual(error(again)['code'], 'link_invalid')

    def test_reset_password_link_problems(self):
        token = accounts.make_password_reset_token(self.user)
        body = {'token': token, 'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD}
        later = time.time() + settings.PASSWORD_RESET_LINK_MINUTES * 60 + 100
        with mock.patch('django.core.signing.time.time', return_value=later):
            self.assertEqual(error(self.client.post(A + 'password/reset/', body))['code'], 'link_expired')
        self.assertEqual(error(self.client.post(A + 'password/reset/', {**body, 'token': 'junk'}))['code'],
                         'link_invalid')
        self.assertIn('confirm_password', error(self.client.post(
            A + 'password/reset/', {**body, 'confirm_password': 'x'}))['fields'])
        self.user.is_active = False
        self.user.save()
        self.assertEqual(error(self.client.post(A + 'password/reset/', body))['code'], 'account_closed')

    def test_web_and_api_reset_tokens_are_the_same(self):
        self.assertIs(web_views._make_password_reset_token, accounts.make_password_reset_token)


class DeleteAccountTests(ApiTestCase):
    def test_delete_flow(self):
        user = make_user('rider@example.com')
        RideSearch.objects.create(user=user, origin='A', destination='B', origin_lat=45, origin_lng=-73,
                                  dest_lat=45.1, dest_lng=-73.1, search_date=date.today(), passenger_count=1)
        client = auth_client(user)
        response = client.post(A + 'account/delete/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('rider@example.com', response.json()['message'])
        link = next(word for word in mail.outbox[-1].body.split() if '/delete-account/confirm/' in word)
        # The emailed link is the website's; clicking it closes the account.
        self.client_class().get(link[link.index('/delete-account/'):])
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(RideSearch.objects.filter(user=user).exists())
        self.assertEqual(client.get(A + 'session/').status_code, 401)

    @limits(delete_account_email=(1, 3600))
    def test_delete_rate_limit(self):
        client = auth_client(make_user('rider@example.com'))
        client.post(A + 'account/delete/')
        self.assertEqual(client.post(A + 'account/delete/').status_code, 429)


class DataExportTests(ApiTestCase):
    def test_export(self):
        user = make_user('rider@example.com')
        response = auth_client(user).get(A + 'account/data/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="komute-my-data-', response['Content-Disposition'])
        body = response.json()
        self.assertEqual(body['account']['email'], 'rider@example.com')
        self.assertEqual(len(body['terms_acceptances']), 1)
        self.assertNotIn('password', str(body['account']))

    def test_same_export_as_the_website(self):
        user = make_user('rider@example.com')
        api_body = auth_client(user).get(A + 'account/data/').json()
        web_body = accounts.build_data_export(user)
        api_body.pop('exported_at'), web_body.pop('exported_at')
        self.assertEqual(api_body.keys(), web_body.keys())

    @limits(data_export=(1, 3600))
    def test_export_rate_limit(self):
        client = auth_client(make_user('rider@example.com'))
        client.get(A + 'account/data/')
        self.assertEqual(client.get(A + 'account/data/').status_code, 429)


def _google(data):
    def verify_token(self, request, token):
        return self.sociallogin_from_response(request, data)
    return mock.patch.object(GoogleProvider, 'verify_token', autospec=True, side_effect=verify_token)


def _apple(data):
    def verify_token(self, request, token):
        return self.sociallogin_from_response(request, data)
    return mock.patch.object(AppleProvider, 'verify_token', autospec=True, side_effect=verify_token)


GOOGLE_NEW = {'sub': 'g-123', 'email': 'new.person@gmail.com', 'email_verified': True,
              'given_name': 'New', 'family_name': 'Person'}


class SocialSignInTests(ApiTestCase):
    def token(self, provider='google', **extra):
        return self.client.post(A + 'social/token/', {'provider': provider, 'id_token': 'x.y.z', **extra})

    def finish(self, signup_token, **overrides):
        body = {'signup_token': signup_token, 'full_name': 'New Person', 'country_code': '+1',
                'phone_number': '4155552671', 'dob': _adult_dob(), 'agreed_to_terms': True}
        body.update(overrides)
        return self.client.post(A + 'social/signup/', body)

    def test_new_google_user(self):
        with _google(GOOGLE_NEW):
            response = self.token()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['status'], 'signup_required')
        self.assertEqual((body['email'], body['full_name'], body['asks_contact_email']),
                         ('new.person@gmail.com', 'New Person', False))
        self.assertFalse(CustomUser.objects.exists())

        response = self.finish(body['signup_token'])
        self.assertEqual(response.status_code, 200, response.content)
        session = response.json()
        self.assertTrue(session['first_login'])
        user = CustomUser.objects.get(email='new.person@gmail.com')
        self.assertEqual((user.username, user.phone, user.dob.isoformat()),
                         ('new.person@gmail.com', '+14155552671', _adult_dob()))
        self.assertTrue(user.email_verified)  # Google vouched for it
        self.assertFalse(user.has_usable_password())
        self.assertTrue(SocialAccount.objects.filter(user=user, provider='google', uid='g-123').exists())
        self.assertTrue(LegalAcceptance.objects.filter(user=user, source=LegalAcceptance.SOURCE_SIGNUP).exists())
        self.assertEqual(mail.outbox[-1].to, ['new.person@gmail.com'])  # welcome email
        me = APIClient().get(A + 'session/', HTTP_X_SESSION_TOKEN=session['token'])
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()['terms_accepted'])
        self.assertNotIn('sessionid', response.cookies)

    def test_finish_signup_uses_the_website_rules(self):
        with _google(GOOGLE_NEW):
            signup_token = self.token().json()['signup_token']
        response = self.finish(signup_token, phone_number='', agreed_to_terms=False, dob=_adult_dob(16))
        self.assertEqual(response.status_code, 400)
        fields = error(response)['fields']
        self.assertEqual(fields['phone_number'], ['Phone number is required.'])
        self.assertEqual(fields['dob'], ['You must be at least 18 years old.'])
        self.assertIn('agreed_to_terms', fields)
        self.assertFalse(CustomUser.objects.exists())
        # Corrected, the same signup token still works.
        self.assertEqual(self.finish(signup_token).status_code, 200)

    def test_unknown_signup_token(self):
        response = self.finish('no-such-session')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(error(response)['code'], 'signup_expired')

    def test_existing_account_with_the_verified_address_signs_in(self):
        user = make_user('new.person@gmail.com')
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
        with _google(GOOGLE_NEW):
            response = self.token()
        body = response.json()
        self.assertEqual(body['status'], 'signed_in', body)
        self.assertFalse(body['first_login'])
        self.assertEqual(body['user']['id'], encode_id(user.pk))
        self.assertTrue(SocialAccount.objects.filter(user=user, provider='google').exists())
        self.assertEqual(APIClient().get(A + 'session/', HTTP_X_SESSION_TOKEN=body['token']).status_code, 200)

    def test_returning_social_user_signs_in(self):
        with _google(GOOGLE_NEW):
            self.finish(self.token().json()['signup_token'])
            body = self.token().json()
        self.assertEqual(body['status'], 'signed_in')

    def test_closed_account_is_refused(self):
        with _google(GOOGLE_NEW):
            self.finish(self.token().json()['signup_token'])
        CustomUser.objects.update(is_active=False)
        with _google(GOOGLE_NEW):
            response = self.token()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(error(response)['code'], 'account_inactive')

    def test_bad_token(self):
        with mock.patch.object(GoogleProvider, 'verify_token',
                               side_effect=ValidationError('Invalid token.', code='invalid_token')):
            response = self.token()
        self.assertEqual(response.status_code, 400)
        self.assertIn('token', error(response)['fields'])

    def test_wrong_client_id(self):
        response = self.token(client_id='someone-elses-client')
        self.assertEqual(response.status_code, 400)

    def test_apple_hide_my_email(self):
        relay = 'abc123@privaterelay.appleid.com'
        with _apple({'sub': 'a-1', 'email': relay, 'email_verified': 'true', 'is_private_email': 'true'}):
            body = self.token(provider='apple', client_id='net.komute.test').json()
        self.assertEqual(body['status'], 'signup_required', body)
        self.assertTrue(body['asks_contact_email'])
        response = self.finish(body['signup_token'], contact_email='me@realmail.com')
        self.assertEqual(response.status_code, 200, response.content)
        user = CustomUser.objects.get(email=relay)
        self.assertEqual((user.contact_email, user.contact_email_confirmed), ('me@realmail.com', False))
        self.assertIn(['me@realmail.com'], [m.to for m in mail.outbox])  # confirmation link


class AppleAppClientTests(ApiTestCase):
    """APPLE_APP_BUNDLE_ID adds a hidden second Apple app for tokens from the
    iOS app; the website's button must keep using the Services ID."""

    @override_settings(SOCIALACCOUNT_PROVIDERS={**settings.SOCIALACCOUNT_PROVIDERS, 'apple': {'APPS': [
        {'client_id': 'net.komute.web', 'secret': 'K', 'key': 'T', 'settings': {'certificate_key': 'x'}},
        {'client_id': 'net.komute.ios', 'secret': 'K', 'key': 'T',
         'settings': {'certificate_key': 'x', 'hidden': True}},
    ]}})
    def test_website_keeps_the_services_id(self):
        from allauth.socialaccount.adapter import get_adapter
        request = RequestFactory().get('/')
        adapter = get_adapter(request)
        self.assertEqual(adapter.get_provider(request, 'apple').app.client_id, 'net.komute.web')
        self.assertEqual(adapter.get_provider(request, 'apple', client_id='net.komute.ios').app.client_id,
                         'net.komute.ios')


class RefactorGuardTests(ApiTestCase):
    """The website's views now call komute.services.accounts; its old names
    must still point there (komute.social and tests use them)."""

    def test_aliases(self):
        self.assertIs(web_views._check_signup_details, accounts.check_signup_details)
        self.assertIs(web_views._send_welcome_email, accounts.send_welcome_email)
        self.assertIs(web_views._make_deactivate_token, accounts.make_deactivate_token)
        self.assertIs(web_views._latest_adult_dob, accounts.latest_adult_dob)
        self.assertEqual(web_views._DELETE_ACCOUNT_SENT_MESSAGE, accounts.DELETE_ACCOUNT_SENT_MESSAGE)

    def test_dob_bounds(self):
        self.assertEqual(accounts.parse_dob(''), (None, 'Date of birth is required.'))
        self.assertEqual(accounts.parse_dob(_adult_dob(102))[1],
                         'Please enter a valid date of birth (age must be 100 or under).')
