"""Module 6, first part: the app's live notice socket (/ws/app/notify/).

    manage.py test api --settings=config.settings_regression

The notices themselves come from the website's booking flows (covered by
komute.tests_regression); these check the app receives them."""
from asgiref.sync import async_to_sync, sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase

from api.routing import with_app_sockets
from api.tests.base import make_user, session_token_for
from api.tests.test_rides import make_driver, make_ride
from config import asgi
from komute.models import RideBooking
from komute.notifications import booking_fingerprint
from komute.services import bookings as booking_rules

PATH = '/ws/app/notify/'


class AppNotifySocketTests(TransactionTestCase):
    def setUp(self):
        self.app = with_app_sockets(asgi.website_sockets)
        self.driver = make_driver()
        self.rider = make_user('rider@example.com')

    def comm(self, user=None, token=None):
        if user is not None:
            token = session_token_for(user)
        headers = [(b'x-session-token', token.encode())] if token else []
        return WebsocketCommunicator(self.app, PATH, headers=headers)

    def close_code(self, **kwargs):
        comm = self.comm(**kwargs)

        async def run():
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            output = await comm.receive_output(timeout=5)
            self.assertEqual(output['type'], 'websocket.close')
            return output['code']
        return async_to_sync(run)()

    def test_refused(self):
        self.assertEqual(self.close_code(), 4401)
        self.assertEqual(self.close_code(token='not-a-session'), 4401)
        self.rider.legal_acceptances.all().delete()
        with self.assertLogs('komute.chat', 'WARNING'):
            self.assertEqual(self.close_code(user=self.rider), 4412)

    def test_state_then_live_notice_from_a_website_flow(self):
        ride = make_ride(self.driver)
        booking = RideBooking.objects.create(ride=ride, passenger=self.rider, seats=1, agreed_price_cents=2500,
                                             status=RideBooking.STATUS_REQUESTED)
        comm = self.comm(user=self.rider)

        async def run():
            self.assertTrue((await comm.connect())[0])
            state = await comm.receive_json_from(timeout=5)
            # The driver accepts, exactly as the website's button does.
            await sync_to_async(booking_rules.accept_booking)(
                await sync_to_async(RideBooking.objects.select_related('ride__driver', 'passenger').get)(
                    pk=booking.pk))
            notice = await comm.receive_json_from(timeout=5)
            await comm.send_json_to({'type': 'ping'})
            pong = await comm.receive_json_from(timeout=5)
            await comm.disconnect()
            return state, notice, pong
        state, notice, pong = async_to_sync(run)()
        self.assertEqual(state['type'], 'state')
        self.assertEqual(len(state['fingerprint']), 16)
        self.assertNotEqual(state['fingerprint'], booking_fingerprint(self.rider))  # it changed: refresh
        self.assertEqual(notice, {'type': 'bookings_changed', 'message': 'Your booking request was accepted.'})
        self.assertEqual(pong, {'type': 'pong'})

    def test_website_notify_socket_unchanged(self):
        comm = WebsocketCommunicator(self.app, '/ws/notify/')  # no Origin header

        async def run():
            connected, _ = await comm.connect()
            return connected
        self.assertFalse(async_to_sync(run)())
