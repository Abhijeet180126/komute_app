"""Module 5 (chat) tests: REST state and history, and the app's socket.

    manage.py test api --settings=config.settings_regression

The socket's rules are the website's ChatConsumer (covered by
komute.tests_regression); these check how the app connects and that it
speaks hashids and cents."""
from asgiref.sync import async_to_sync, sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings

from api.routing import with_app_sockets
from api.tests.base import ApiTestCase, auth_client, make_user, session_token_for
from api.tests.test_rides import make_driver, make_ride
from config import asgi
from komute.chat_tokens import make_chat_token
from komute.consumers import ChatConsumer
from komute.hashid_utils import encode_id
from komute.models import Message, PriceOffer, RideBooking, Rides, UserBlock

B = '/api/v1/bookings/'


def make_booking(driver, rider, status=RideBooking.STATUS_CONFIRMED):
    return RideBooking.objects.create(ride=make_ride(driver), passenger=rider, seats=1,
                                      agreed_price_cents=2500, status=status)


class ChatRestTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_driver()
        self.rider = make_user('rider@example.com')
        self.booking = make_booking(self.driver, self.rider)
        self.url = f'{B}{encode_id(self.booking.pk)}/chat/'

    def test_state(self):
        body = auth_client(self.rider).get(self.url).json()
        self.assertEqual((body['label'], body['chat_locked'], body['nego_locked']), ('Confirmed', False, False))
        self.assertEqual(body['socket_path'], f"/ws/app/chat/{body['chat_token']}/")
        self.assertEqual(body['other']['id'], encode_id(self.driver.pk))
        self.assertFalse(body['is_driver'])
        self.assertEqual(body['agreed_price_cents'], 2500)
        self.assertTrue(auth_client(self.driver).get(self.url).json()['is_driver'])

    def test_locks_follow_the_booking(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_REQUESTED)
        body = auth_client(self.rider).get(self.url).json()
        self.assertEqual((body['nego_locked'], body['agreed_price_cents']), (True, None))
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_CANCELLED)
        body = auth_client(self.rider).get(self.url).json()
        self.assertEqual((body['chat_locked'], body['closed_message']),
                         (True, 'Booking was cancelled — messaging is closed.'))

    def test_pending_offers(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_DRAFT)
        offer = PriceOffer.objects.create(booking=self.booking, proposed_by=self.driver, price_cents=2000)
        body = auth_client(self.rider).get(self.url).json()
        self.assertEqual(body['pending_incoming'], {'id': encode_id(offer.pk), 'price_cents': 2000})
        self.assertIsNone(body['pending_outgoing'])

    def test_history_and_read_marks(self):
        ride = self.booking.ride
        first = Message.objects.create(ride=ride, sender=self.rider, receiver=self.driver, body='Hi')
        second = Message.objects.create(ride=ride, sender=self.driver, receiver=self.rider, body='Hello')
        other_rider = make_user('other@example.com')
        Message.objects.create(ride=ride, sender=other_rider, receiver=self.driver, body='Private')
        body = auth_client(self.rider).get(self.url + 'messages/').json()
        self.assertEqual(body['count'], 2)  # not the other rider's conversation
        self.assertEqual([(m['id'], m['from_me']) for m in body['results']],
                         [(encode_id(second.pk), False), (encode_id(first.pk), True)])
        second.refresh_from_db()
        first.refresh_from_db()
        self.assertIsNotNone(second.read_at)
        self.assertIsNone(first.read_at)  # sent by the reader

    def test_access(self):
        stranger = auth_client(make_user('x@example.com'))
        self.assertEqual(stranger.get(self.url).status_code, 404)
        self.assertEqual(stranger.get(self.url + 'messages/').status_code, 404)
        new_terms = make_user('new@example.com', accepted_terms=False)
        booking = make_booking(self.driver, new_terms)
        response = auth_client(new_terms).get(f'{B}{encode_id(booking.pk)}/chat/')
        self.assertEqual(response.json()['error']['code'], 'terms_not_accepted')


class AppSocketTests(TransactionTestCase):
    def setUp(self):
        self.driver = make_driver()
        self.rider = make_user('rider@example.com')
        self.booking = make_booking(self.driver, self.rider)
        self.app = with_app_sockets(asgi.website_sockets)
        self.path = f'/ws/app/chat/{make_chat_token(self.booking.pk)}/'

    def _comm(self, user=None, token=None, path=None):
        headers = []
        if user is not None:
            token = session_token_for(user)
        if token:
            headers.append((b'x-session-token', token.encode()))
        return WebsocketCommunicator(self.app, path or self.path, headers=headers)

    def close_code(self, **kwargs):
        comm = self._comm(**kwargs)

        async def run():
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            output = await comm.receive_output(timeout=5)
            self.assertEqual(output['type'], 'websocket.close')
            return output['code']
        return async_to_sync(run)()

    def test_refused_connections(self):
        with self.assertLogs('komute.chat', 'WARNING'):
            self.assertEqual(self.close_code(), 4401)
            self.assertEqual(self.close_code(token='not-a-session'), 4401)
            self.assertEqual(self.close_code(user=make_user('x@example.com')), 4403)
            bad_path = '/ws/app/chat/not-a-token/'
            self.assertEqual(self.close_code(user=self.rider, path=bad_path), 4403)

    def test_changed_password_ends_the_token(self):
        token = session_token_for(self.rider)
        self.rider.set_password('AnotherPass456!y')
        self.rider.save()
        with self.assertLogs('komute.chat', 'WARNING'):
            self.assertEqual(self.close_code(token=token), 4401)

    def test_terms_must_be_current(self):
        self.rider.legal_acceptances.all().delete()
        with self.assertLogs('komute.chat', 'WARNING'):
            self.assertEqual(self.close_code(user=self.rider), 4412)

    def test_blocked(self):
        UserBlock.objects.create(blocker=self.driver, blocked=self.rider)
        with self.assertLogs('komute.chat', 'WARNING'):
            self.assertEqual(self.close_code(user=self.rider), 4410)

    def test_website_cookie_does_not_open_the_app_socket(self):
        comm = WebsocketCommunicator(self.app, self.path)
        comm.scope['user'] = self.rider  # what a cookie would have set

        async def run():
            await comm.connect()
            return (await comm.receive_output(timeout=5))['code']
        with self.assertLogs('komute.chat', 'WARNING'):
            self.assertEqual(async_to_sync(run)(), 4401)

    def test_website_sockets_still_need_an_origin(self):
        comm = WebsocketCommunicator(self.app, f'/ws/chat/{make_chat_token(self.booking.pk)}/')

        async def run():
            connected, _ = await comm.connect()
            return connected
        self.assertFalse(async_to_sync(run)())

    def test_app_and_browser_share_the_chat(self):
        rider, token = self._comm(user=self.rider), make_chat_token(self.booking.pk)
        browser = WebsocketCommunicator(ChatConsumer.as_asgi(), f'/ws/chat/{token}/')
        browser.scope['user'] = self.driver
        browser.scope['url_route'] = {'kwargs': {'token': token}}

        async def run():
            self.assertTrue((await rider.connect())[0])
            self.assertTrue((await browser.connect())[0])
            await rider.send_json_to({'type': 'message', 'body': 'On my way'})
            on_phone = await rider.receive_json_from(timeout=5)
            on_browser = await browser.receive_json_from(timeout=5)
            await rider.disconnect()
            await browser.disconnect()
            return on_phone, on_browser
        with self.assertLogs('komute.chat', 'INFO'):
            on_phone, on_browser = async_to_sync(run)()

        message = Message.objects.get()
        self.assertEqual((message.body, message.sender, message.receiver), ('On my way', self.rider, self.driver))
        self.assertEqual(on_phone['id'], encode_id(message.pk))
        self.assertEqual(on_phone['sender_id'], encode_id(self.rider.pk))
        self.assertTrue(on_phone['from_me'])
        self.assertEqual(on_phone['sent_at'], message.created_at.isoformat())
        # The website's frame is unchanged.
        self.assertEqual(set(on_browser), {'type', 'body', 'sender_id', 'initials', 'time'})
        self.assertEqual(on_browser['sender_id'], self.rider.pk)

    def negotiate_on_draft(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_DRAFT)
        driver, rider = self._comm(user=self.driver), self._comm(user=self.rider)

        async def run():
            await driver.connect()
            await rider.connect()
            await driver.send_json_to({'type': 'price_offer', 'price_cents': 2000})
            offer = await rider.receive_json_from(timeout=5)
            await driver.receive_json_from(timeout=5)
            pk = await sync_to_async(lambda: PriceOffer.objects.get().pk)()
            self.assertEqual(offer['offer_id'], encode_id(pk))
            await rider.send_json_to({'type': 'price_accepted', 'offer_id': offer['offer_id']})
            accepted = await rider.receive_json_from(timeout=5)
            await rider.disconnect()
            await driver.disconnect()
            return offer, accepted
        return async_to_sync(run)()

    def test_negotiate_in_cents_and_hashids(self):
        with self.assertLogs('komute.chat', 'INFO'):
            offer, accepted = self.negotiate_on_draft()
        self.assertEqual((offer['price_cents'], offer['from_me']), (2000, False))
        self.assertEqual(accepted, {'type': 'price_accepted', 'price_cents': 2000})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_CONFIRMED)
        self.assertEqual(Rides.objects.get(pk=self.booking.ride_id).seats_available, 2)

    @override_settings(ROOT_URLCONF='api.tests.urls')
    def test_offer_made_through_the_api_reaches_an_open_browser_chat(self):
        token = make_chat_token(self.booking.pk)
        browser = WebsocketCommunicator(ChatConsumer.as_asgi(), f'/ws/chat/{token}/')
        browser.scope['user'] = self.driver
        browser.scope['url_route'] = {'kwargs': {'token': token}}
        client = auth_client(self.rider)
        url = f'{B}{encode_id(self.booking.pk)}/offers/'

        async def run():
            await browser.connect()
            response = await sync_to_async(client.post)(url, {'price_cents': 2000}, format='json')
            self.assertEqual(response.status_code, 201, response.content)
            frame = await browser.receive_json_from(timeout=5)
            await browser.disconnect()
            return frame
        with self.assertLogs('komute.chat', 'INFO'):
            frame = async_to_sync(run)()
        self.assertEqual((frame['type'], frame['price'], frame['sender_id']), ('price_offer', 20.0, self.rider.pk))

    def test_bad_frames_get_an_error_and_keep_the_socket(self):
        comm = self._comm(user=self.rider)

        async def run():
            await comm.connect()
            answers = []
            await comm.send_to(text_data='not json')
            answers.append(await comm.receive_json_from(timeout=5))
            await comm.send_json_to({'type': 'price_offer', 'price_cents': 'free'})
            answers.append(await comm.receive_json_from(timeout=5))
            await comm.send_json_to({'type': 'price_accepted', 'offer_id': 'junk'})
            answers.append(await comm.receive_json_from(timeout=5))
            await comm.send_json_to({'type': 'ping'})
            answers.append(await comm.receive_json_from(timeout=5))
            await comm.disconnect()
            return answers
        with self.assertLogs('komute.chat', 'INFO'):
            answers = async_to_sync(run)()
        self.assertEqual([a.get('code', a['type']) for a in answers],
                         ['bad_request', 'bad_price', 'offer_stale', 'pong'])

    def test_switch(self):
        with override_settings(API_ENABLED=False):
            self.assertIs(asgi.build_websocket_application(), asgi.website_sockets)
        with override_settings(API_ENABLED=True):
            self.assertIsNot(asgi.build_websocket_application(), asgi.website_sockets)
