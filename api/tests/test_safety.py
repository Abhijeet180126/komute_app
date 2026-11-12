"""Module 7 (reviews, reports, blocks) tests.

    manage.py test api --settings=config.settings_regression

The rules are the website's (komute.services.safety, covered by
komute.tests_regression); these check the API applies them the same way."""
from django.conf import settings
from django.core import mail
from django.test import Client, override_settings

from api.tests.base import ApiTestCase, auth_client, make_user
from api.tests.test_rides import make_driver, make_ride
from komute.hashid_utils import encode_id
from komute.models import Review, RideBooking, UserBlock, UserReport

S = '/api/v1/safety/'


def error(response):
    return response.json()['error']


class SafetyTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_driver()
        self.rider = make_user('rider@example.com')
        self.ride = make_ride(self.driver, status='completed')
        self.booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                                  agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED)
        self.rider_client = auth_client(self.rider)
        self.driver_client = auth_client(self.driver)


class ReviewTests(SafetyTestCase):
    def review(self, client, **fields):
        return client.post(S + 'reviews/', {'booking': encode_id(self.booking.pk), 'rating': 5, **fields},
                           format='json')

    def test_pending_then_review(self):
        pending = self.rider_client.get(S + 'reviews/pending/').json()['results']
        self.assertEqual([(p['booking_id'], p['reviewee_role'], p['reviewee']['id']) for p in pending],
                         [(encode_id(self.booking.pk), 'driver', encode_id(self.driver.pk))])
        response = self.review(self.rider_client, rating=4, comment=' Great ', tags=['punctual', 'safe-driver'],
                               subratings={'punctuality': 5})
        self.assertEqual(response.status_code, 201, response.content)
        review = Review.objects.get()
        self.assertEqual((review.reviewer, review.reviewee, review.rating, review.body, review.tags_json),
                         (self.rider, self.driver, 4, 'Great', ['punctual', 'safe-driver']))
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.avg_rating, 4)
        self.assertEqual(self.rider_client.get(S + 'reviews/pending/').json()['count'], 0)
        self.assertEqual(error(self.review(self.rider_client))['code'], 'already_reviewed')

    def test_both_sides_review(self):
        self.assertEqual(self.driver_client.get(S + 'reviews/pending/').json()['results'][0]['reviewee_role'],
                         'passenger')
        self.assertEqual(self.review(self.driver_client, rating=3).status_code, 201)
        self.assertEqual(Review.objects.get().reviewee, self.rider)

    def test_rules(self):
        self.assertEqual(error(self.review(self.rider_client, rating=6))['fields']['rating'],
                         ['Rating must be between 1 and 5.'])
        self.assertEqual(self.review(self.rider_client, tags=['amazing']).status_code, 400)
        self.assertEqual(self.review(self.rider_client, subratings={'vibes': 5}).status_code, 400)
        self.assertEqual(self.review(auth_client(make_user('x@example.com'))).status_code, 404)
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_CANCELLED)
        self.assertEqual(error(self.review(self.rider_client))['code'], 'not_completed')
        self.assertFalse(Review.objects.exists())

    def test_website_and_app_share_the_once_only_rule(self):
        web = Client()
        web.force_login(self.rider)
        web.post('/reviews/submit/', {'booking_id': encode_id(self.booking.pk), 'reviewee_id': self.driver.pk,
                                      'rating': 5}, content_type='application/json')
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(error(self.review(self.rider_client))['code'], 'already_reviewed')


class ReportTests(SafetyTestCase):
    def report(self, client=None, **fields):
        body = {'booking': encode_id(self.booking.pk), 'reason': 'no_show', **fields}
        return (client or self.rider_client).post(S + 'reports/', body, format='json')

    def test_report(self):
        response = self.report(details='Never showed up')
        self.assertEqual(response.status_code, 201, response.content)
        report = UserReport.objects.get()
        self.assertEqual((report.reporter, report.reported, report.booking, report.details),
                         (self.rider, self.driver, self.booking, 'Never showed up'))
        self.assertEqual(mail.outbox[-1].to, [settings.ADMIN_EMAIL])

    def test_by_ride(self):
        other = make_user('other@example.com')
        response = self.report(client=auth_client(other), booking='', ride=encode_id(self.ride.pk))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(UserReport.objects.get().reported, self.driver)

    def test_rules(self):
        self.assertEqual(error(self.report(reason='bogus'))['fields']['reason'], ['Please choose a reason.'])
        self.assertEqual(error(self.report(reason='other'))['code'], 'details_required')
        self.assertEqual(error(self.report(details='x' * 2001))['code'], 'details_too_long')
        self.assertEqual(self.report(client=auth_client(make_user('x@example.com'))).status_code, 404)
        self.assertEqual(self.report(booking='', ride=encode_id(self.ride.pk), client=self.driver_client).status_code,
                         404)  # your own ride names nobody
        self.assertFalse(UserReport.objects.exists())

    @override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'report-user': (1, 3600)})
    def test_rate_limit(self):
        self.report()
        response = self.report()
        self.assertEqual((response.status_code, error(response)['message']),
                         (429, 'Too many reports sent. Please try again later.'))


class BlockTests(SafetyTestCase):
    def test_block_list_unblock(self):
        response = self.rider_client.post(S + 'blocks/', {'booking': encode_id(self.booking.pk)}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['id'], encode_id(self.driver.pk))
        self.assertTrue(UserBlock.objects.between(self.rider.pk, self.driver.pk))
        self.assertEqual(self.rider_client.post(S + 'blocks/', {'booking': encode_id(self.booking.pk)},
                                                format='json').status_code, 201)  # again: harmless
        listed = self.rider_client.get(S + 'blocks/').json()
        self.assertEqual([b['id'] for b in listed], [encode_id(self.driver.pk)])
        self.assertEqual(self.rider_client.delete(f"{S}blocks/{listed[0]['id']}/").status_code, 204)
        self.assertFalse(UserBlock.objects.exists())
        self.assertEqual(self.rider_client.delete(f"{S}blocks/{listed[0]['id']}/").status_code, 404)

    def test_blocking_hides_the_ride(self):
        self.rider_client.post(S + 'blocks/', {'booking': encode_id(self.booking.pk)}, format='json')
        live_ride = make_ride(self.driver)
        self.assertEqual(self.rider_client.get(f'/api/v1/rides/{encode_id(live_ride.pk)}/').status_code, 404)

    def test_needs_a_booking_or_ride_you_can_see(self):
        self.assertEqual(self.rider_client.post(S + 'blocks/', {}, format='json').status_code, 404)
        stranger = auth_client(make_user('x@example.com'))
        self.assertEqual(stranger.post(S + 'blocks/', {'booking': encode_id(self.booking.pk)},
                                       format='json').status_code, 404)

    def test_website_blocked_list_matches(self):
        self.rider_client.post(S + 'blocks/', {'booking': encode_id(self.booking.pk)}, format='json')
        web = Client()
        web.force_login(self.rider)
        keys = [b['key'] for b in web.get('/profile/').context['blocked_users']]
        self.assertEqual(keys, [b['id'] for b in self.rider_client.get(S + 'blocks/').json()])
