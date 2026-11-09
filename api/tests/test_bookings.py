"""Module 4 (bookings, price offers, inbox) tests.

    manage.py test api --settings=config.settings_regression

The rules are the website's (komute.services.bookings, covered by
komute.tests_regression); these check the API applies them the same way."""
from datetime import timedelta

from django.conf import settings
from django.core import mail
from django.test import Client
from django.utils import timezone

from api.tests.base import ApiTestCase, auth_client, make_user
from api.tests.test_rides import KINGSTON, make_driver, make_ride
from komute.chat_tokens import resolve_chat_token
from komute.hashid_utils import encode_id
from komute.models import Message, PriceOffer, RideBooking, Rides, RideStop, UserBlock

B = '/api/v1/bookings/'
O = '/api/v1/offers/'


def error(response):
    return response.json()['error']


class BookingTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_driver()
        self.ride = make_ride(self.driver)  # 3 seats at $25
        self.rider = make_user('rider@example.com', gender='female')
        self.rider_client = auth_client(self.rider)
        self.driver_client = auth_client(self.driver)

    def book(self, client=None, **fields):
        body = {'ride': encode_id(self.ride.pk), **fields}
        return (client or self.rider_client).post(B, body, format='json')

    def booked(self, **fields):
        response = self.book(**fields)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class BookTests(BookingTestCase):
    def test_book_seats(self):
        stop = RideStop.objects.create(ride=self.ride, stop_order=1, location='Kingston, ON',
                                       lat=KINGSTON['lat'], lng=KINGSTON['lng'])
        body = self.booked(seats=2, pickup_stop=encode_id(stop.pk))
        self.assertEqual((body['status'], body['seats'], body['agreed_price_cents']),
                         ('requested', 2, 2 * 2500 + settings.SERVICE_FEE_CENTS))
        self.assertEqual(body['pickup_stop'], encode_id(stop.pk))
        self.assertIsNone(body['dropoff_stop'])
        self.assertTrue(body['chat_token'])
        self.assertIsNone(body['driver_phone'])
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 1)
        self.assertEqual(mail.outbox[-1].to, ['driver@example.com'])  # request email
        self.assertEqual(error(self.book())['code'], 'already_requested')

    def test_last_seats_make_the_ride_full(self):
        self.booked(seats=3)
        self.ride.refresh_from_db()
        self.assertEqual((self.ride.seats_available, self.ride.status), (0, 'full'))
        other = auth_client(make_user('other@example.com'))
        self.assertEqual(error(self.book(client=other))['code'], 'not_enough_seats')

    def test_chat_first_then_book(self):
        draft = self.booked(intent='chat', seats=2)
        self.assertEqual(draft['status'], 'draft')
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 3)  # no seat held
        self.assertEqual(len(mail.outbox), 0)  # driver not told
        self.assertEqual(self.booked(intent='chat')['id'], draft['id'])  # the same chat
        booked = self.booked(seats=2)
        self.assertEqual((booked['id'], booked['status']), (draft['id'], 'requested'))
        # The chat opened as a draft still works (tokens carry their signing
        # time, so a new one can differ; both name this booking).
        self.assertEqual(resolve_chat_token(draft['chat_token']), RideBooking.objects.get().pk)

    def test_who_may_book(self):
        self.assertEqual(error(self.book(client=self.driver_client))['code'], 'riders_only')
        UserBlock.objects.create(blocker=self.driver, blocked=self.rider)
        response = self.book()
        self.assertEqual((response.status_code, error(response)['code']), (403, 'blocked'))
        UserBlock.objects.all().delete()
        type(self.driver).objects.filter(pk=self.driver.pk).update(role='rider')
        self.assertEqual(error(self.book(client=auth_client(self.driver)))['code'], 'own_ride')

    def test_booking_rules(self):
        cases = [
            (make_user('unverified@example.com', email_verified=False), {}, 403, 'email_unverified'),
            (make_user('nodob@example.com', dob=None), {}, 403, 'dob_missing'),
            (self.rider, {'seats': 9}, 400, 'invalid_seats'),
        ]
        for user, fields, status, code in cases:
            response = self.book(client=auth_client(user), **fields)
            self.assertEqual((response.status_code, error(response)['code']), (status, code), code)
        Rides.objects.filter(pk=self.ride.pk).update(passenger_gender_pref='men_only')
        self.assertEqual(error(self.book())['message'], 'This ride is limited to passengers of a different gender.')
        Rides.objects.filter(pk=self.ride.pk).update(passenger_gender_pref='any',
                                                     depart_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(error(self.book())['code'], 'ride_unavailable')
        self.assertFalse(RideBooking.objects.exists())

    def test_website_and_app_share_bookings(self):
        web = Client()
        web.force_login(self.rider)
        self.assertEqual(web.post(f'/ride-booking/{encode_id(self.ride.pk)}/', {'seats': 1}).json()['ok'], True)
        self.assertEqual(self.rider_client.get(B).json()['count'], 1)
        self.assertEqual(error(self.book())['code'], 'already_requested')


class BookingListTests(BookingTestCase):
    def test_lists_and_detail(self):
        booking = self.booked()
        self.booked(client=auth_client(make_user('chat@example.com')), intent='chat')
        self.assertEqual([b['id'] for b in self.rider_client.get(B).json()['results']], [booking['id']])
        on_mine = self.driver_client.get(B + 'on-my-rides/').json()
        self.assertEqual([b['id'] for b in on_mine['results']], [booking['id']])  # draft left out
        self.assertEqual(self.driver_client.get(B + 'on-my-rides/', {'status': 'confirmed'}).json()['count'], 0)
        for client in (self.rider_client, self.driver_client):
            self.assertEqual(client.get(f"{B}{booking['id']}/").status_code, 200)
        self.assertEqual(auth_client(make_user('x@example.com')).get(f"{B}{booking['id']}/").status_code, 404)

    def test_requests_first_for_the_driver(self):
        first = self.booked()
        RideBooking.objects.filter(pk=RideBooking.objects.get().pk).update(status=RideBooking.STATUS_CONFIRMED)
        second = self.booked(client=auth_client(make_user('later@example.com')))
        ids = [b['id'] for b in self.driver_client.get(B + 'on-my-rides/').json()['results']]
        self.assertEqual(ids, [second['id'], first['id']])


class BookingActionTests(BookingTestCase):
    def setUp(self):
        super().setUp()
        self.booking = self.booked(seats=2)
        self.url = f"{B}{self.booking['id']}/"
        mail.outbox.clear()

    def test_rider_cancels(self):
        response = self.rider_client.post(self.url + 'cancel/')
        self.assertEqual(response.json()['status'], 'cancelled')
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 3)
        self.assertEqual(mail.outbox[-1].to, ['driver@example.com'])
        self.assertEqual(error(self.rider_client.post(self.url + 'cancel/'))['code'], 'already_closed')

    def test_only_the_rider_cancels(self):
        response = self.driver_client.post(self.url + 'cancel/')
        self.assertEqual((response.status_code, error(response)['code']), (403, 'not_authorized'))
        self.assertEqual(auth_client(make_user('x@example.com')).post(self.url + 'cancel/').status_code, 404)

    def test_driver_accepts(self):
        response = self.driver_client.post(self.url + 'accept/')
        self.assertEqual(response.json()['status'], 'confirmed', response.content)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['driver@example.com', 'rider@example.com'])
        self.assertEqual(self.rider_client.get(self.url).json()['driver_phone'], '+14165550101')
        self.assertEqual(error(self.driver_client.post(self.url + 'accept/'))['code'], 'not_pending')

    def test_driver_rejects(self):
        self.assertEqual(self.driver_client.post(self.url + 'reject/').json()['status'], 'rejected')
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 3)
        self.assertEqual(mail.outbox[-1].to, ['rider@example.com'])

    def test_only_the_driver_accepts_or_rejects(self):
        for action in ('accept/', 'reject/'):
            self.assertEqual(error(self.rider_client.post(self.url + action))['code'], 'not_authorized')

    def test_accept_rules(self):
        self.driver.driver_verified = False
        self.driver.save()
        response = self.driver_client.post(self.url + 'accept/')
        self.assertEqual((response.status_code, error(response)['code']), (403, 'driver_not_verified'))
        self.driver.driver_verified = True
        self.driver.save()
        UserBlock.objects.create(blocker=self.rider, blocked=self.driver)
        self.assertEqual(error(self.driver_client.post(self.url + 'accept/'))['code'], 'blocked')
        self.assertEqual(self.driver_client.post(self.url + 'reject/').status_code, 200)  # still possible


class OfferTests(BookingTestCase):
    def setUp(self):
        super().setUp()
        self.booking = self.booked(seats=2)
        self.offers_url = f"{B}{self.booking['id']}/offers/"

    def test_negotiate(self):
        response = self.rider_client.post(self.offers_url, {'price_cents': 2000}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        offer = response.json()
        self.assertTrue(offer['proposed_by_me'])
        listed = self.driver_client.get(self.offers_url).json()
        self.assertEqual((listed[0]['id'], listed[0]['proposed_by_me']), (offer['id'], False))

        countered = self.driver_client.post(f"{O}{offer['id']}/respond/",
                                            {'action': 'counter', 'price_cents': 2200}, format='json')
        self.assertEqual(countered.status_code, 201)
        counter = countered.json()
        self.assertEqual(PriceOffer.objects.get(pk=PriceOffer.objects.order_by('id').first().pk).status, 'countered')

        accepted = self.rider_client.post(f"{O}{counter['id']}/respond/", {'action': 'accept'}, format='json')
        self.assertEqual(accepted.json()['status'], 'accepted')
        booking = RideBooking.objects.get()
        self.assertEqual(booking.agreed_price_cents, 2200 * 2 + settings.SERVICE_FEE_CENTS)

    def test_offer_rules(self):
        offer = self.rider_client.post(self.offers_url, {'price_cents': 2000}, format='json').json()
        response = self.rider_client.post(f"{O}{offer['id']}/respond/", {'action': 'accept'}, format='json')
        self.assertEqual((response.status_code, error(response)['code']), (403, 'own_offer'))
        for price, message in ((0, 'Price must be greater than 0.'), (100001, 'Offer cannot exceed $1,000.')):
            response = self.rider_client.post(self.offers_url, {'price_cents': price}, format='json')
            self.assertEqual(error(response)['message'], message)
        self.driver_client.post(f"{O}{offer['id']}/respond/", {'action': 'decline'}, format='json')
        self.assertEqual(error(self.driver_client.post(f"{O}{offer['id']}/respond/", {'action': 'accept'},
                                                       format='json'))['code'], 'not_pending')
        stranger = auth_client(make_user('x@example.com'))
        self.assertEqual(stranger.get(self.offers_url).status_code, 404)
        self.assertEqual(stranger.post(f"{O}{offer['id']}/respond/", {'action': 'accept'},
                                       format='json').status_code, 404)

    def test_no_negotiation_on_a_draft(self):
        draft = self.booked(client=auth_client(make_user('chat@example.com')), intent='chat')
        response = auth_client(make_user('chat2@example.com'))  # not part of it
        self.assertEqual(response.get(f"{B}{draft['id']}/offers/").status_code, 404)
        client = auth_client(type(self.rider).objects.get(email='chat@example.com'))
        self.assertEqual(error(client.post(f"{B}{draft['id']}/offers/", {'price_cents': 1000},
                                           format='json'))['code'], 'negotiation_closed')


class InboxTests(BookingTestCase):
    def test_inboxes(self):
        booking = self.booked()
        self.rider_client.post(f"{B}{booking['id']}/offers/", {'price_cents': 2000}, format='json')
        Message.objects.create(ride=self.ride, sender=self.rider, receiver=self.driver, body='Hi!')
        # A pre-request chat with nothing in it stays out of the driver's inbox.
        self.booked(client=auth_client(make_user('quiet@example.com')), intent='chat')

        rows = self.driver_client.get(B + 'inbox/').json()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['booking_id'], row['booking_status'], row['unread_count']),
                         (booking['id'], 'requested', 1))
        self.assertEqual((row['nego_status'], row['nego_cents'], row['nego_by_other']), ('pending', 2000, True))
        self.assertEqual(row['last_message'], {'body': 'Hi!', 'sent_at': row['last_message']['sent_at'],
                                               'from_me': False})
        self.assertEqual(row['other']['id'], encode_id(self.rider.pk))

        rider_rows = self.rider_client.get(B + 'inbox/').json()
        self.assertEqual((rider_rows[0]['other']['id'], rider_rows[0]['nego_by_other']),
                         (encode_id(self.driver.pk), False))
        self.assertTrue(rider_rows[0]['last_message']['from_me'])

    def test_admin_has_no_inbox(self):
        admin = make_user('admin@example.com', role='admin')
        self.assertEqual(error(auth_client(admin).get(B + 'inbox/'))['code'], 'no_inbox')
