"""Module 3 (rides, search, summary, receipt, route demand, invites) tests.

    manage.py test api --settings=config.settings_regression

The rules are the website's (komute.services.rides / invites / route_demand,
covered by komute.tests_regression); these check the API applies them the
same way. Also covers the website fix for rides whose vehicle was removed."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from api.tests.base import ApiTestCase, auth_client, make_user
from komute.hashid_utils import encode_id
from komute.models import (
    RideBooking, RideInvite, Rides, RideSearch, RideStop, UserBlock, Vehicle,
)
from komute.services import invites as invite_rules

R = '/api/v1/rides/'
I = '/api/v1/invites/'
TORONTO = {'name': 'Toronto, ON', 'lat': 43.6532, 'lng': -79.3832}
OTTAWA = {'name': 'Ottawa, ON', 'lat': 45.4215, 'lng': -75.6972}
KINGSTON = {'name': 'Kingston, ON', 'lat': 44.2312, 'lng': -76.4860}
MONTREAL = {'name': 'Montreal, QC', 'lat': 45.5017, 'lng': -73.5673}
LONDON_UK = {'name': 'London, UK', 'lat': 51.5072, 'lng': -0.1276}


def error(response):
    return response.json()['error']


def soon(days=3, hour=15):
    """An aware departure `days` from now, in Toronto time."""
    local = timezone.now().astimezone(ZoneInfo('America/Toronto')) + timedelta(days=days)
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


def make_driver(email='driver@example.com', **extra):
    fields = dict(role='driver', driver_verified=True, driver_verified_at=timezone.now(),
                  license_expiry=date.today() + timedelta(days=400), phone='+14165550101')
    fields.update(extra)
    user = make_user(email, **fields)
    Vehicle.objects.create(owner=user, make='Toyota', model='Corolla', year=2020, color='Red',
                           plate=f'P{user.pk:05d}', is_primary=True, vehicle_verified=True)
    return user


def make_ride(driver, depart=None, origin=TORONTO, destination=OTTAWA, **extra):
    fields = dict(driver=driver, vehicle=driver.vehicles.first(), origin=origin['name'],
                  origin_lat=origin['lat'], origin_lng=origin['lng'], destination=destination['name'],
                  dest_lat=destination['lat'], dest_lng=destination['lng'], depart_at=depart or soon(),
                  seats_total=3, seats_available=3, price_cents=2500, tz='America/Toronto')
    fields.update(extra)
    return Rides.objects.create(**fields)


def ride_body(driver, **overrides):
    body = {'origin': TORONTO, 'destination': OTTAWA, 'depart_at': soon().isoformat(), 'tz': 'America/Toronto',
            'seats': 3, 'price_cents': 2500, 'vehicle': encode_id(driver.vehicles.first().pk)}
    body.update(overrides)
    return body


class PostRideTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_driver()
        self.client = auth_client(self.driver)

    def post(self, **overrides):
        return self.client.post(R, ride_body(self.driver, **overrides), format='json')

    def test_post_a_ride(self):
        response = self.post(stops=[KINGSTON], preferences=['Music OK', 'No Smoking'], notes='  Hi  ',
                             passenger_gender_pref='women_only', duration_seconds=16200)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body['rides_created'], 1)
        ride = body['ride']
        self.assertEqual((ride['status'], ride['seats_available'], ride['price_cents']), ('posted', 3, 2500))
        self.assertEqual(ride['stops'][0]['name'], 'Kingston, ON')
        self.assertEqual(ride['preferences'], ['Music OK', 'No Smoking'])
        self.assertEqual((ride['notes'], ride['tz'], ride['passenger_gender_pref']),
                         ('Hi', 'America/Toronto', 'women_only'))
        self.assertTrue(ride['is_mine'])
        saved = Rides.objects.get()
        self.assertEqual(saved.arrive_at - saved.depart_at, timedelta(seconds=16200))
        self.assertEqual(saved.vehicle, self.driver.vehicles.first())

    def test_who_may_post(self):
        cases = [
            (make_user('rider@example.com'), 'Only drivers can post rides.'),
            (make_driver('unverified-email@example.com', email_verified=False),
             'Please verify your email address first: click "Verify Now" next to your email on your profile page.'),
            (make_user('notverified@example.com', role='driver'), 'You must be verified to post a ride.'),
        ]
        for user, message in cases:
            response = auth_client(user).post(R, {}, format='json')
            self.assertEqual(response.status_code, 403, message)
            self.assertEqual(error(response), {'code': 'cannot_post', 'message': message, 'fields': {}})
        expired = make_driver('expired@example.com', driver_verified_at=timezone.now() - timedelta(days=400))
        self.assertEqual(error(auth_client(expired).post(R, {}, format='json'))['message'],
                         'Your identity verification has expired. Please re-verify to post a ride.')
        no_vehicle = make_driver('novehicle@example.com')
        no_vehicle.vehicles.update(vehicle_verified=False)
        self.assertEqual(error(auth_client(no_vehicle).post(R, {}, format='json'))['message'],
                         'You need at least one verified vehicle to post a ride.')

    def test_required_fields_use_the_website_messages(self):
        fields = error(self.client.post(R, {}, format='json'))['fields']
        self.assertEqual(fields['origin'], ['Starting point is required.'])
        self.assertEqual(fields['destination'], ['Final destination is required.'])
        self.assertEqual(fields['seats'], ['Seat count is required.'])
        self.assertEqual(fields['price_cents'], ['Price per seat is required.'])
        self.assertEqual(fields['vehicle'], ['Please select a vehicle.'])
        self.assertIn('depart_at', fields)
        self.assertFalse(Rides.objects.exists())

    def test_places(self):
        cases = [
            ({'origin': {'name': 'Somewhere'}}, 'origin', 'Please pick a location from the suggestions.'),
            ({'origin': MONTREAL}, 'origin',
             "Komüte isn't available in Quebec yet. Please choose a pickup location outside Quebec."),
            ({'destination': LONDON_UK}, 'destination',
             'We currently only support drop-off locations in the US and Canada.'),
            ({'stops': [{'name': 'Kingston'}]}, 'stops',
             'Pick "Kingston" from the suggestions list, or remove that stop.'),
        ]
        for overrides, field, message in cases:
            response = self.post(**overrides)
            self.assertEqual(error(response)['fields'][field], [message], overrides)

    def test_numbers_and_dates(self):
        cases = [
            ({'seats': 9}, 'seats', 'Seats must be between 1 and 8.'),
            ({'price_cents': 0}, 'price_cents', 'Price per seat must be greater than 0.'),
            ({'price_cents': 100001}, 'price_cents', 'Price per seat cannot exceed $1,000.'),
            ({'depart_at': (timezone.now() + timedelta(minutes=10)).isoformat()}, 'depart_at',
             'Departure must be at least 30 minutes from now.'),
            ({'depart_at': (timezone.now() + timedelta(days=200)).isoformat()}, 'depart_at',
             'Rides can only be posted up to 4 months in advance.'),
            ({'tz': 'Mars/Olympus'}, 'tz', 'Unknown time zone.'),
        ]
        for overrides, field, message in cases:
            response = self.post(**overrides)
            self.assertEqual(error(response)['fields'][field], [message], overrides)
        self.assertEqual(self.post(preferences=['Karaoke']).status_code, 400)

    def test_vehicle_must_be_my_verified_vehicle(self):
        unverified = Vehicle.objects.create(owner=self.driver, make='Kia', model='Rio', year=2018, color='Grey',
                                            plate='UNVER1', vehicle_verified=False)
        self.assertEqual(error(self.post(vehicle=encode_id(unverified.pk)))['fields']['vehicle'],
                         ["This vehicle isn't verified yet. Choose a verified vehicle."])
        theirs = make_driver('other@example.com').vehicles.first()
        self.assertEqual(error(self.post(vehicle=encode_id(theirs.pk)))['fields']['vehicle'],
                         ['Invalid vehicle selected.'])

    def test_recurring(self):
        first = soon(days=1, hour=8)
        end = (first + timedelta(days=13)).date()
        weekdays = [0, 2, 4]  # Mon, Wed, Fri, local time
        response = self.post(depart_at=first.isoformat(),
                             recurring={'end_date': end.isoformat(), 'days': weekdays})
        self.assertEqual(response.status_code, 201, response.content)
        rides = Rides.objects.order_by('depart_at')
        expected = [first.date() + timedelta(days=n) for n in range(14)
                    if (first.date() + timedelta(days=n)).weekday() in weekdays]
        self.assertEqual(response.json()['rides_created'], len(expected))
        local = [r.depart_at.astimezone(ZoneInfo('America/Toronto')) for r in rides]
        self.assertEqual([d.date() for d in local], expected)
        self.assertTrue(all(d.hour == 8 for d in local))
        self.assertEqual(len({r.recurring_group_id for r in rides}), 1)

    def test_recurring_rules(self):
        response = self.post(recurring={'end_date': soon(days=1).date().isoformat(), 'days': []})
        fields = error(response)['fields']
        self.assertEqual(fields['recurring.end_date'], ['End date must be after the departure date.'])
        self.assertEqual(fields['recurring.days'], ['Select at least one day of the week.'])
        too_far = (date.today() + timedelta(days=200)).isoformat()
        self.assertEqual(error(self.post(recurring={'end_date': too_far, 'days': [1]}))['fields']
                         ['recurring.end_date'], ['Recurring rides can repeat up to 4 months in advance.'])


class RideDetailEditCancelTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_driver()
        self.ride = make_ride(self.driver)
        RideStop.objects.create(ride=self.ride, stop_order=1, location='Kingston, ON',
                                lat=KINGSTON['lat'], lng=KINGSTON['lng'])
        self.url = f'{R}{encode_id(self.ride.pk)}/'

    def test_driver_and_rider_views(self):
        mine = auth_client(self.driver).get(self.url).json()
        self.assertTrue(mine['is_mine'])
        rider = make_user('rider@example.com')
        seen = auth_client(rider).get(self.url).json()
        self.assertFalse(seen['is_mine'])
        self.assertEqual(seen['driver']['name'], 'Test Driver@Example.Com')
        self.assertEqual(seen['vehicle']['plate'], self.driver.vehicles.first().plate)
        self.assertIsNone(seen['driver_phone'])
        self.assertIsNone(seen['my_booking_status'])
        RideBooking.objects.create(ride=self.ride, passenger=rider, seats=1, agreed_price_cents=2500,
                                   status=RideBooking.STATUS_CONFIRMED)
        seen = auth_client(rider).get(self.url).json()
        self.assertEqual((seen['my_booking_status'], seen['driver_phone']), ('confirmed', '+14165550101'))

    def test_who_may_view(self):
        response = auth_client(make_driver('other@example.com')).get(self.url)
        self.assertEqual((response.status_code, error(response)['code']), (403, 'riders_only'))
        rider = make_user('blocked@example.com')
        UserBlock.objects.create(blocker=self.driver, blocked=rider)
        self.assertEqual(auth_client(rider).get(self.url).status_code, 404)
        self.assertEqual(auth_client(rider).get(f'{R}junk/').status_code, 404)

    def test_edit(self):
        client = auth_client(self.driver)
        response = client.patch(self.url, {'price_cents': 3000, 'stops': []}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.ride.refresh_from_db()
        self.assertEqual((self.ride.price_cents, self.ride.stops.count(), self.ride.origin),
                         (3000, 0, 'Toronto, ON'))
        # Unsent fields stay, including the stops.
        client.patch(self.url, {'stops': [KINGSTON]}, format='json')
        client.patch(self.url, {'notes': 'Bring snacks'}, format='json')
        self.ride.refresh_from_db()
        self.assertEqual((self.ride.notes, self.ride.stops.count(), self.ride.price_cents),
                         ('Bring snacks', 1, 3000))
        too_far = (timezone.now() + timedelta(days=200)).isoformat()
        self.assertEqual(error(client.patch(self.url, {'depart_at': too_far}, format='json'))['fields']['depart_at'],
                         ['Rides can only be scheduled up to 4 months in advance.'])
        self.assertEqual(error(client.patch(self.url, {'price_cents': 0}, format='json'))['fields']['price_cents'],
                         ['Price must be greater than 0.'])

    def test_edit_rules(self):
        RideBooking.objects.create(ride=self.ride, passenger=make_user('r@example.com'), seats=1,
                                   agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED)
        response = auth_client(self.driver).patch(self.url, {'price_cents': 3000}, format='json')
        self.assertEqual(error(response)['message'], 'Cannot edit a ride that has active bookings.')
        self.assertEqual(auth_client(make_driver('x@example.com')).patch(self.url, {}, format='json').status_code,
                         404)

    def test_cancel(self):
        rider = make_user('rider@example.com')
        booking = RideBooking.objects.create(ride=self.ride, passenger=rider, seats=1, agreed_price_cents=2500,
                                             status=RideBooking.STATUS_CONFIRMED)
        response = auth_client(self.driver).post(self.url + 'cancel/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['status'], 'cancelled')
        booking.refresh_from_db()
        self.assertEqual(booking.status, RideBooking.STATUS_CANCELLED)
        self.assertIn(['rider@example.com'], [m.to for m in mail.outbox])
        again = auth_client(self.driver).post(self.url + 'cancel/')
        self.assertEqual(error(again)['message'], 'Cannot cancel — ride is already cancelled.')

    def test_my_rides(self):
        make_ride(self.driver, status='cancelled')
        client = auth_client(self.driver)
        self.assertEqual([r['id'] for r in client.get(R + 'mine/').json()['results']], [encode_id(self.ride.pk)])
        self.assertEqual(client.get(R + 'mine/?status=cancelled').json()['count'], 1)


class SearchTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_driver()
        self.ride = make_ride(self.driver)
        self.rider = make_user('rider@example.com', gender='female')
        self.client = auth_client(self.rider)

    def search(self, **params):
        query = {'from': 'Toronto', 'origin_lat': TORONTO['lat'], 'origin_lng': TORONTO['lng'],
                 'to': 'Ottawa', 'dest_lat': OTTAWA['lat'], 'dest_lng': OTTAWA['lng'], 'tz': 'America/Toronto'}
        query.update(params)
        return self.client.get(R + 'search/', query)

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return [r['id'] for r in response.json()['results']]

    def test_finds_nearby_rides(self):
        self.assertEqual(self.ids(self.search()), [encode_id(self.ride.pk)])
        self.assertEqual(self.ids(self.search(dest_lat=LONDON_UK['lat'] - 5, dest_lng=-60.0)), [])

    def test_date_is_logged_for_route_demand(self):
        self.ids(self.search(date=self.ride.depart_at.astimezone(ZoneInfo('America/Toronto')).date().isoformat()))
        self.assertEqual(RideSearch.objects.filter(user=self.rider).count(), 1)
        self.ids(self.search())
        self.assertEqual(RideSearch.objects.count(), 1)  # no date, not logged

    def test_rules_shared_with_the_website(self):
        Rides.objects.filter(pk=self.ride.pk).update(passenger_gender_pref='men_only')
        self.assertEqual(self.ids(self.search()), [])
        Rides.objects.filter(pk=self.ride.pk).update(passenger_gender_pref='any')
        UserBlock.objects.create(blocker=self.rider, blocked=self.driver)
        self.assertEqual(self.ids(self.search()), [])

    def test_errors(self):
        past = (date.today() - timedelta(days=2)).isoformat()
        self.assertEqual(error(self.search(date=past))['fields']['date'], ['Search date cannot be in the past.'])
        quebec = error(self.search(origin_lat=MONTREAL['lat'], origin_lng=MONTREAL['lng']))
        self.assertIn('Quebec', quebec['fields']['from'][0])
        response = auth_client(self.driver).get(R + 'search/', {'from': 'Toronto'})
        self.assertEqual(error(response)['code'], 'riders_only')

    def test_ride_whose_vehicle_was_removed(self):
        self.driver.vehicles.all().delete()
        results = self.search().json()['results']
        self.assertIsNone(results[0]['vehicle'])

    def test_filters(self):
        self.assertEqual(self.ids(self.search(seats=4)), [])
        self.assertEqual(self.ids(self.search(max_price_cents=2000)), [])
        self.assertEqual(self.ids(self.search(max_price_cents=2500)), [encode_id(self.ride.pk)])


class WebsiteRemovedVehicleTests(ApiTestCase):
    """Website fix: search results and the booking page used to crash (500)
    on a ride whose vehicle the driver had removed."""

    def test_pages_render(self):
        driver = make_driver()
        ride = make_ride(driver)
        driver.vehicles.all().delete()
        rider = make_user('rider@example.com')
        web = Client()
        web.force_login(rider)
        response = web.post('/search-rides/', {'from': 'Toronto', 'to': 'Ottawa', 'origin_lat': TORONTO['lat'],
                                               'origin_lng': TORONTO['lng'], 'dest_lat': OTTAWA['lat'],
                                               'dest_lng': OTTAWA['lng'], 'tz': 'America/Toronto'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['rides'].object_list[0]['vehicle_plate'], '')
        self.assertEqual(web.get(f'/ride-booking/{encode_id(ride.pk)}/').status_code, 200)


class SummaryReceiptTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_driver()
        self.ride = make_ride(self.driver, depart=timezone.now() - timedelta(hours=5), status='completed',
                              arrive_at=timezone.now() - timedelta(hours=1))
        self.rider = make_user('rider@example.com')
        RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=2, agreed_price_cents=5000,
                                   status=RideBooking.STATUS_COMPLETED)
        self.url = f'{R}{encode_id(self.ride.pk)}/'

    def test_summary(self):
        body = auth_client(self.driver).get(self.url + 'summary/').json()
        self.assertEqual((body['passenger_count'], body['total_contributions_cents'], body['duration']),
                         (2, 5000, '4h 0m'))
        self.assertTrue(body['viewer_is_driver'])
        self.assertEqual(body['passengers'][0]['pickup'], 'Toronto')
        self.assertGreater(body['distance_km'], 300)
        self.assertFalse(auth_client(self.rider).get(self.url + 'summary/').json()['viewer_is_driver'])
        self.assertEqual(auth_client(make_user('x@example.com')).get(self.url + 'summary/').status_code, 404)

    def test_summary_needs_a_completed_ride(self):
        Rides.objects.filter(pk=self.ride.pk).update(status='posted')
        self.assertEqual(error(auth_client(self.driver).get(self.url + 'summary/'))['code'], 'not_completed')

    def test_receipt(self):
        response = auth_client(self.rider).get(self.url + 'receipt/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertIn(f'KMT-{self.ride.depart_at.year}-{self.ride.pk:04d}', response['Content-Disposition'])
        self.assertEqual(auth_client(make_user('x@example.com')).get(self.url + 'receipt/').status_code, 404)

    def test_receipt_same_as_the_website(self):
        web = Client()
        web.force_login(self.driver)
        web_pdf = web.get(f'/ride-receipt/{encode_id(self.ride.pk)}/pdf/')
        api_pdf = auth_client(self.driver).get(self.url + 'receipt/')
        self.assertEqual(web_pdf['Content-Disposition'], api_pdf['Content-Disposition'])
        self.assertTrue(web_pdf.content.startswith(b'%PDF'))


def searched(rider, origin=TORONTO, destination=OTTAWA):
    return RideSearch.objects.create(user=rider, origin=origin['name'], origin_lat=origin['lat'],
                                     origin_lng=origin['lng'], destination=destination['name'],
                                     dest_lat=destination['lat'], dest_lng=destination['lng'],
                                     search_date=date.today() + timedelta(days=3), passenger_count=2)


class DemandAndInviteTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_driver()
        self.ride = make_ride(self.driver)
        self.rider = make_user('rider@example.com')
        searched(self.rider)

    def send(self, client=None, rider=None):
        return (client or auth_client(self.driver)).post(I, {
            'ride': encode_id(self.ride.pk), 'rider': encode_id((rider or self.rider).pk)}, format='json')

    def test_route_demand(self):
        body = auth_client(self.driver).get(R + 'demand/').json()
        route = body['routes'][0]
        self.assertEqual((route['ride_id'], route['search_count']), (encode_id(self.ride.pk), 1))
        self.assertEqual(route['riders'][0]['id'], encode_id(self.rider.pk))
        self.assertEqual(route['riders'][0]['seats_needed'], 2)
        self.assertEqual(error(auth_client(self.rider).get(R + 'demand/'))['code'], 'drivers_only')

    def test_send_invite(self):
        response = self.send()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['status'], 'sent')
        self.assertEqual(mail.outbox[-1].to, ['rider@example.com'])
        self.assertIn('/invite/', mail.outbox[-1].body)
        self.assertEqual(error(self.send())['code'], 'already_invited')

    def test_send_rules(self):
        stranger = make_user('stranger@example.com')  # never searched this route
        self.assertEqual(error(self.send(rider=stranger))['code'], 'not_eligible')
        response = auth_client(self.driver).post(I, {'ride': encode_id(self.ride.pk), 'rider': 'junk'},
                                                 format='json')
        self.assertEqual(error(response)['code'], 'not_eligible')  # same answer, no oracle
        self.assertEqual(auth_client(self.driver).post(I, {'ride': 'junk', 'rider': 'x'}, format='json').status_code,
                         404)
        self.driver.driver_verified = False
        self.driver.save()
        response = self.send()
        self.assertEqual((response.status_code, error(response)['code']), (403, 'driver_not_verified'))

    @override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'send-invite': (1, 3600)})
    def test_send_rate_limit(self):
        self.send()
        response = self.send()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(error(response)['message'], 'Too many invites sent. Please try again later.')

    def test_accept(self):
        self.send()
        rider = auth_client(self.rider)
        invite_id = rider.get(I).json()['results'][0]['id']
        opened = rider.get(f'{I}{invite_id}/').json()
        self.assertEqual(opened['status'], 'viewed')
        response = rider.post(f'{I}{invite_id}/accept/')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body['booking_status'], body['seats'], body['invite']['status']),
                         ('confirmed', 1, 'accepted'))
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 2)
        self.assertEqual(error(rider.post(f'{I}{invite_id}/accept/'))['code'], 'already_responded')

    def test_only_the_invited_rider(self):
        self.send()
        invite_id = encode_id(RideInvite.objects.get().pk)
        other = auth_client(make_user('other@example.com'))
        for method, url in (('get', f'{I}{invite_id}/'), ('post', f'{I}{invite_id}/accept/'),
                            ('post', f'{I}{invite_id}/decline/')):
            self.assertEqual(getattr(other, method)(url).status_code, 404, url)
        self.assertEqual(RideInvite.objects.get().status, RideInvite.STATUS_SENT)

    def test_accept_rules(self):
        self.send()
        invite = RideInvite.objects.get()
        rider = auth_client(self.rider)
        type(self.rider).objects.filter(pk=self.rider.pk).update(email_verified=False)
        response = rider.post(f'{I}{encode_id(invite.pk)}/accept/')
        self.assertEqual((response.status_code, error(response)['code']), (403, 'email_unverified'))
        type(self.rider).objects.filter(pk=self.rider.pk).update(email_verified=True)
        RideInvite.objects.filter(pk=invite.pk).update(sent_at=timezone.now() - timedelta(days=30))
        response = rider.post(f'{I}{encode_id(invite.pk)}/accept/')
        self.assertEqual((response.status_code, error(response)['code']), (410, 'invite_expired'))
        self.assertEqual(RideInvite.objects.get().status, RideInvite.STATUS_EXPIRED)

    def test_decline(self):
        self.send()
        invite_id = encode_id(RideInvite.objects.get().pk)
        rider = auth_client(self.rider)
        self.assertEqual(rider.post(f'{I}{invite_id}/decline/').json()['status'], 'declined')
        self.assertEqual(error(rider.post(f'{I}{invite_id}/decline/'))['code'], 'already_responded')

    def test_by_token(self):
        self.send()
        token = invite_rules.make_invite_token(RideInvite.objects.get().pk)
        rider = auth_client(self.rider)
        self.assertEqual(rider.get(f'{I}by-token/{token}/').json()['status'], 'viewed')
        self.assertEqual(rider.get(f'{I}by-token/junk/').status_code, 404)
        self.assertEqual(auth_client(make_user('x@example.com')).get(f'{I}by-token/{token}/').status_code, 404)
