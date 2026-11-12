"""Module 9 (app config and the contact form) tests.

    manage.py test api --settings=config.settings_regression

The contact rules are the website's (komute.services.contact, covered by
komute.tests_regression.ContactFormTests); these check the app gets the
same email, the page's options and limits, and the shared rate limit."""
import html
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.test import Client, override_settings

from api.tests.base import ApiTestCase
from komute.services import contact

CONFIG = '/api/v1/app/config/'
CONTACT = '/api/v1/app/contact/'
FORM = {'name': 'Jordan Lee', 'email': 'jordan@example.com', 'role': 'Driver',
        'topic': 'Help with a booking', 'subject': 'Seat count', 'message': 'Hello there.'}


def error(response):
    return response.json()['error']


class ConfigTests(ApiTestCase):
    def test_public_with_defaults(self):
        body = self.client.get(CONFIG).json()
        self.assertEqual(body['ios'], {'min_version': '0.0.0', 'latest_version': '', 'store_url': ''})
        self.assertEqual(body['android']['min_version'], '0.0.0')
        self.assertEqual(body['maintenance']['active'], False)
        legal = body['legal']
        self.assertEqual(legal['terms'], {'version': settings.TERMS_VERSION,
                                          'url': 'http://testserver/terms-condition/'})
        self.assertEqual(legal['privacy'], {'version': settings.PRIVACY_VERSION,
                                            'url': 'http://testserver/privacy-policy/'})
        self.assertEqual(legal['biometric_notice_version'], settings.BIOMETRIC_NOTICE_VERSION)
        self.assertEqual((legal['community_guidelines_url'], legal['accessibility_url']),
                         ('http://testserver/community-guidelines/', 'http://testserver/accessibility/'))
        for url in (legal['terms']['url'], legal['community_guidelines_url'], legal['accessibility_url']):
            self.assertEqual(Client().get(url.replace('http://testserver', '')).status_code, 200, url)

    def test_bad_token_still_reads_it(self):
        self.client.credentials(HTTP_X_SESSION_TOKEN='nonsense')
        self.assertEqual(self.client.get(CONFIG).status_code, 200)

    @override_settings(APP_MIN_VERSION_IOS='1.4.0', APP_LATEST_VERSION_ANDROID='1.6.2',
                       APP_STORE_URL_ANDROID='https://play.example/komute', APP_MAINTENANCE=True,
                       APP_MAINTENANCE_MESSAGE='Back at 6 AM.')
    def test_from_settings(self):
        body = self.client.get(CONFIG).json()
        self.assertEqual(body['ios']['min_version'], '1.4.0')
        self.assertEqual((body['android']['latest_version'], body['android']['store_url']),
                         ('1.6.2', 'https://play.example/komute'))
        self.assertEqual(body['maintenance'], {'active': True, 'message': 'Back at 6 AM.'})

    @override_settings(APP_MIN_VERSION_IOS='1.4', APP_MIN_VERSION_ANDROID='99.0.0 ',
                       APP_LATEST_VERSION_IOS='latest')
    def test_a_bad_version_never_locks_apps_out(self):
        with self.assertLogs('komute.api', 'ERROR') as logs:
            body = self.client.get(CONFIG).json()
        self.assertEqual((body['ios']['min_version'], body['ios']['latest_version']), ('0.0.0', ''))
        self.assertEqual(body['android']['min_version'], '99.0.0')
        self.assertEqual(len(logs.records), 2)

    def test_support_is_what_the_contact_page_shows(self):
        page = html.unescape(Client().get('/contact-us/').content.decode())
        support = self.client.get(CONFIG).json()['support']
        for channel in support['channels']:
            self.assertIn(f"<h3>{channel['name']}</h3>", page)
            self.assertIn(f"mailto:{channel['email']}", page)
        for row in support['hours']:
            self.assertIn(f"<span>{row['days']}</span><span>{row['time']}</span>", page)
        self.assertIn(f"<span>{support['hours_time_zone']}</span>", page)
        form = support['contact_form']
        for value in form['roles'] + form['topics']:
            self.assertIn(f'<option>{value}</option>', page)
        self.assertEqual(page.count('<option>'), len(form['roles']) + len(form['topics']))
        self.assertIn(f'maxlength="{form["message_max_chars"]}"', page)
        self.assertEqual(page.count('class="channel"'), len(support['channels']))


class ContactTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        mail.outbox.clear()

    def post(self, **changes):
        return self.client.post(CONTACT, {**FORM, **changes}, format='json')

    def test_same_email_as_the_website(self):
        response = self.post()
        self.assertEqual((response.status_code, response.json()), (200, {'sent': True}))
        app_mail = mail.outbox[-1]
        Client().post('/contact-us/', FORM)
        web_mail = mail.outbox[-1]
        self.assertEqual(len(mail.outbox), 2)
        for attr in ('subject', 'body', 'to', 'from_email', 'reply_to'):
            self.assertEqual(getattr(app_mail, attr), getattr(web_mail, attr), attr)
        self.assertEqual(app_mail.reply_to, ['Jordan Lee <jordan@example.com>'])

    def test_website_messages(self):
        for changes, code, message in (
                ({'subject': ' '}, 'required', 'Please fill in all required fields.'),
                ({'email': 'nope'}, 'invalid_email', 'Please enter a valid email address.'),
                ({'name': '\x07'}, 'name_required', 'Please enter your name.')):
            response = self.post(**changes)
            self.assertEqual((response.status_code, error(response)['code'], error(response)['message']),
                             (400, code, message), changes)
        self.assertEqual(mail.outbox, [])

    def test_only_the_pages_options(self):
        self.assertEqual(self.post(role='').status_code, 200)
        for changes, code in (({'role': 'Pilot'}, 'invalid_role'), ({'topic': 'Hello'}, 'invalid_topic'),
                              ({'message': 'x' * 1001}, 'message_too_long')):
            response = self.post(**changes)
            self.assertEqual((response.status_code, error(response)['code']), (400, code), changes)
        self.assertEqual(self.post(message='x' * contact.MESSAGE_MAX_CHARS).status_code, 200)
        self.assertEqual(len(mail.outbox), 2)

    def test_odd_bodies(self):
        self.assertEqual(error(self.post(name=['a'], subject=5))['code'], 'required')
        response = self.client.post(CONTACT, ['not', 'an', 'object'], format='json')
        self.assertEqual(error(response)['code'], 'required')
        response = self.client.post(CONTACT, '{bad', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_line_breaks_in_the_subject(self):
        self.assertEqual(self.post(subject='Seat\ncount').status_code, 200)
        self.assertEqual(mail.outbox[-1].subject, '[Komüte Contact] Help with a booking: Seat count')

    def test_mail_failure(self):
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('down')), \
                self.assertLogs('komute', 'ERROR'):
            response = self.post()
        self.assertEqual((response.status_code, error(response)['code']), (500, 'send_failed'))

    @override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'contact': (1, 3600)})
    def test_rate_limit_shared_with_the_website(self):
        Client().post('/contact-us/', FORM)
        response = self.post()
        self.assertEqual((response.status_code, error(response)['code']), (429, 'throttled'))
        self.assertEqual(len(mail.outbox), 1)
