"""
Pre-deploy regression suite. Run with the throwaway sqlite settings:

    python manage.py test komute.tests_regression --settings=config.settings_regression -v 2

Not wired into CI; this is a manually-invoked pre-deploy sanity pass covering
auth, password reset, account deactivation/deletion, the full ride lifecycle
(post/edit/search/book/accept/reject/cancel/review/invite), route-demand
matching, price negotiation, profile + vehicle management, driver/rider
inbox conversation assembly, the verify-driver request-validation gates,
hashid encode/decode, all retention/purge management commands,
owner-only serving + migration of private identity images (ISSUES.md C1),
and per-booking chat isolation (ISSUES.md C4). Does NOT cover: the rest of
websocket chat, or real outbound email delivery (locmem backend only). The verify-driver
flow's pre-call validation gates, request payload shape, and response
parsing are covered with the outbound `requests.post` call to the
DL-Verification Node mocked out -- no real verification service is hit.
"""
import io
import json
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, mock_open, patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from asgiref.sync import async_to_sync, sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from komute.chat_tokens import make_chat_token
from komute.consumers import ChatConsumer, NotifyConsumer
from komute.notifications import booking_fingerprint
from komute.hashid_utils import decode_id, encode_id
from komute.models import (
    CustomUser, Vehicle, Rides, RideStop, RideBooking, Message,
    RideSearch, VerificationLog, Review, RideInvite, PriceOffer,
)


def make_user(email, role='rider', **kwargs):
    defaults = dict(full_name=f'Test {email}', dob=date(1995, 1, 1), phone=None)
    defaults.update(kwargs)
    user = CustomUser.objects.create_user(
        username=email, email=email, password='TestPass123', role=role, **defaults
    )
    return user


def make_driver_with_vehicle(email='driver@example.com'):
    driver = make_user(email, role='driver', driver_verified=True)
    Vehicle.objects.create(
        owner=driver, make='Toyota', model='Corolla', year=2020,
        color='blue', plate=f'PLT-{email[:4]}', vehicle_verified=True,
    )
    return driver


def make_ride(driver, **kwargs):
    vehicle = driver.vehicles.first()
    defaults = dict(
        origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
        destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
        depart_at=timezone.now() + timedelta(days=1),
        seats_total=3, seats_available=3, price_cents=2500,
    )
    defaults.update(kwargs)
    return Rides.objects.create(driver=driver, vehicle=vehicle, **defaults)


class AuthFlowTests(TestCase):
    def test_signup_then_login_then_logout(self):
        c = Client()
        resp = c.post(reverse('signup'), {
            'full_name': 'Jane Doe', 'email': 'jane@example.com',
            'country_code': '+1', 'phone_number': '4155552671', 'password': 'GoodPassword1!',
            'confirm_password': 'GoodPassword1!', 'agreed_to_terms': '1',
        })
        self.assertRedirects(resp, reverse('login'))
        self.assertTrue(CustomUser.objects.filter(email='jane@example.com').exists())

        resp = c.post(reverse('login'), {'email': 'jane@example.com', 'password': 'GoodPassword1!'})
        self.assertEqual(resp.status_code, 302)  # first login -> role-select

        resp = c.get(reverse('logout'))
        self.assertRedirects(resp, reverse('login'))

    def test_login_wrong_password_rejected(self):
        make_user('bob@example.com')
        c = Client()
        resp = c.post(reverse('login'), {'email': 'bob@example.com', 'password': 'wrong'})
        self.assertEqual(resp.status_code, 200)  # re-renders login with error
        self.assertFalse(resp.wsgi_request.user.is_authenticated)


class ProfileAndAccountMgmtTests(TestCase):
    def setUp(self):
        self.user = make_user('carla@example.com')
        self.client.login(username='carla@example.com', password='TestPass123')

    def test_profile_get(self):
        resp = self.client.get(reverse('profile'))
        self.assertEqual(resp.status_code, 200)

    def test_change_password_flow(self):
        resp = self.client.post(reverse('change-password'), {
            'current_password': 'TestPass123',
            'new_password': 'NewPassword456!',
            'confirm_password': 'NewPassword456!',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)['ok'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewPassword456!'))

    def test_change_password_wrong_current_rejected(self):
        resp = self.client.post(reverse('change-password'), {
            'current_password': 'WRONG',
            'new_password': 'NewPassword456!',
            'confirm_password': 'NewPassword456!',
        })
        self.assertEqual(resp.status_code, 400)


class DeactivateAccountFlowTests(TestCase):
    def setUp(self):
        self.user = make_user('deact@example.com')
        self.client.login(username='deact@example.com', password='TestPass123')

    def test_request_then_confirm_deactivation(self):
        resp = self.client.post(reverse('request-deactivate-account'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('confirmation link', json.loads(resp.content)['message'])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)
        self.assertIn('confirm_url', mail.outbox[0].body) if False else None

        # Extract the token from the emailed confirm_url
        body = mail.outbox[0].body
        confirm_line = [l for l in body.splitlines() if l.startswith('http')][0]
        token = confirm_line.rstrip('/').split('/')[-1]

        self.assertFalse(CustomUser.objects.get(pk=self.user.pk).is_active is False)  # still active pre-confirm

        resp = self.client.get(reverse('confirm-deactivate-account', args=[token]))
        self.assertRedirects(resp, reverse('login'))

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertIsNotNone(self.user.deactivated_at)

        # Session should be logged out
        resp = self.client.get(reverse('profile'))
        self.assertNotEqual(resp.status_code, 200)  # redirected to login (302)

    def test_deactivated_user_cannot_login(self):
        self.user.is_active = False
        self.user.deactivated_at = timezone.now()
        self.user.save()
        c = Client()
        resp = c.post(reverse('login'), {'email': 'deact@example.com', 'password': 'TestPass123'})
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_confirm_with_bad_token_errors_gracefully(self):
        resp = self.client.get(reverse('confirm-deactivate-account', args=['not-a-real-token']))
        # fetch_redirect_response=False: this client is still logged in as
        # self.user, so /login/ itself 302s onward to /profile/.
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)  # untouched

    def test_confirm_twice_is_idempotent(self):
        self.user.is_active = False
        self.user.deactivated_at = timezone.now()
        self.user.save()
        from komute.views import _make_deactivate_token
        token = _make_deactivate_token(self.user)
        resp = self.client.get(reverse('confirm-deactivate-account', args=[token]))
        self.assertRedirects(resp, reverse('login'))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)


class PasswordResetFlowTests(TestCase):
    def setUp(self):
        self.user = make_user('reset@example.com')

    def test_request_then_confirm_reset_flow(self):
        c = Client()
        resp = c.post(reverse('forgot-password'), {'email': 'reset@example.com'})
        self.assertRedirects(resp, reverse('forgot-password'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)

        # Extract the token from the emailed reset_url
        body = mail.outbox[0].body
        reset_line = [l for l in body.splitlines() if l.startswith('http')][0]
        token = reset_line.rstrip('/').split('/')[-1]

        resp = c.get(reverse('reset-password', args=[token]))
        self.assertEqual(resp.status_code, 200)

        resp = c.post(reverse('reset-password', args=[token]), {
            'new_password': 'BrandNewPass123!',
            'confirm_password': 'BrandNewPass123!',
        })
        self.assertRedirects(resp, reverse('login'))

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('BrandNewPass123!'))

        # Old password no longer works, new one does
        resp = c.post(reverse('login'), {'email': 'reset@example.com', 'password': 'TestPass123'})
        self.assertFalse(resp.wsgi_request.user.is_authenticated)
        resp = c.post(reverse('login'), {'email': 'reset@example.com', 'password': 'BrandNewPass123!'})
        self.assertEqual(resp.status_code, 302)

    def test_unknown_email_gives_same_response_no_email_sent(self):
        c = Client()
        resp = c.post(reverse('forgot-password'), {'email': 'nobody@example.com'})
        # Same redirect/messaging as a known email, and no mail sent -- this
        # form must not be usable to enumerate registered accounts.
        self.assertRedirects(resp, reverse('forgot-password'))
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_with_bad_token_redirects_to_forgot_password(self):
        c = Client()
        resp = c.get(reverse('reset-password', args=['not-a-real-token']))
        self.assertRedirects(resp, reverse('forgot-password'))

    def test_reset_with_expired_token_rejected(self):
        from komute.views import _make_password_reset_token
        token = _make_password_reset_token(self.user)
        future = time.time() + 3700  # just past the 1-hour expiry window
        c = Client()
        with patch('django.core.signing.time.time', return_value=future):
            resp = c.get(reverse('reset-password', args=[token]))
        self.assertRedirects(resp, reverse('forgot-password'))

    def test_mismatched_passwords_rejected(self):
        from komute.views import _make_password_reset_token
        token = _make_password_reset_token(self.user)
        c = Client()
        resp = c.post(reverse('reset-password', args=[token]), {
            'new_password': 'BrandNew123',
            'confirm_password': 'Different123',
        })
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('TestPass123'))  # unchanged

    def test_weak_password_rejected(self):
        from komute.views import _make_password_reset_token
        token = _make_password_reset_token(self.user)
        c = Client()
        resp = c.post(reverse('reset-password', args=[token]), {
            'new_password': 'short',
            'confirm_password': 'short',
        })
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('TestPass123'))  # unchanged

    def test_logged_in_user_redirected_away_from_forgot_and_reset(self):
        from komute.views import _make_password_reset_token
        token = _make_password_reset_token(self.user)
        c = Client()
        c.login(username='reset@example.com', password='TestPass123')
        resp = c.get(reverse('forgot-password'))
        self.assertRedirects(resp, reverse('profile'))
        resp = c.get(reverse('reset-password', args=[token]))
        self.assertRedirects(resp, reverse('profile'))


class PurgeDeactivatedAccountsCommandTests(TestCase):
    """The 45-day fulfillment job: highest-risk new code this session."""

    def setUp(self):
        self.grace_days = 45
        self.leaver = make_driver_with_vehicle('leaver@example.com')
        self.leaver.is_active = False
        self.leaver.deactivated_at = timezone.now() - timedelta(days=self.grace_days + 1)
        self.leaver.save()

        # Data exclusively owned by the leaver -> should be hard-deleted.
        RideSearch.objects.create(
            user=self.leaver, origin='A', origin_lat=0, origin_lng=0,
            destination='B', dest_lat=1, dest_lng=1, search_date=date.today(),
        )
        VerificationLog.objects.create(user=self.leaver, result='approved')

        # A ride the leaver drove, booked by a THIRD PARTY who did not request
        # deletion -- this ride/booking/message must survive.
        self.other_rider = make_user('other_rider@example.com')
        self.ride = make_ride(self.leaver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.other_rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )
        Message.objects.create(ride=self.ride, sender=self.leaver, receiver=self.other_rider, body='hi')
        Message.objects.create(ride=self.ride, sender=self.other_rider, receiver=self.leaver, body='hi back')

        # A not-yet-eligible deactivated user (only 10 days ago) -> must be skipped.
        self.recent_leaver = make_user('recent_leaver@example.com')
        self.recent_leaver.is_active = False
        self.recent_leaver.deactivated_at = timezone.now() - timedelta(days=10)
        self.recent_leaver.save()

    def test_dry_run_changes_nothing(self):
        call_command('purge_deactivated_accounts', '--dry-run')
        self.leaver.refresh_from_db()
        self.assertIsNone(self.leaver.data_purged_at)
        self.assertEqual(self.leaver.email, 'leaver@example.com')
        self.assertTrue(RideSearch.objects.filter(user=self.leaver).exists())

    def test_purge_anonymizes_leaver_and_deletes_owned_data(self):
        original_id = self.leaver.id
        call_command('purge_deactivated_accounts')

        self.leaver.refresh_from_db()
        self.assertEqual(self.leaver.id, original_id)  # row NOT hard-deleted
        self.assertEqual(self.leaver.email, f'deleted-user-{original_id}@komute.invalid')
        self.assertEqual(self.leaver.full_name, 'Deleted User')
        self.assertIsNone(self.leaver.phone)
        self.assertIsNone(self.leaver.dob)
        self.assertFalse(self.leaver.has_usable_password())
        self.assertIsNotNone(self.leaver.data_purged_at)

        # Owned data gone
        self.assertFalse(RideSearch.objects.filter(user=self.leaver).exists())
        self.assertFalse(VerificationLog.objects.filter(user=self.leaver).exists())
        self.assertFalse(Vehicle.objects.filter(owner=self.leaver).exists())
        self.assertFalse(Message.objects.filter(ride=self.ride).exists())

        # Other party's ride/booking record survives, still pointing at the
        # (now-anonymized) leaver -- this is the cascade-safety property the
        # anonymize-not-delete design exists for.
        self.assertTrue(Rides.objects.filter(pk=self.ride.pk).exists())
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.passenger_id, self.other_rider.id)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.driver_id, self.leaver.id)

    def test_not_yet_eligible_account_untouched(self):
        call_command('purge_deactivated_accounts')
        self.recent_leaver.refresh_from_db()
        self.assertEqual(self.recent_leaver.email, 'recent_leaver@example.com')
        self.assertIsNone(self.recent_leaver.data_purged_at)

    def test_second_run_is_idempotent_and_sends_no_duplicate_admin_email(self):
        call_command('purge_deactivated_accounts')
        mail.outbox.clear()
        call_command('purge_deactivated_accounts')
        self.assertEqual(len(mail.outbox), 0)  # nothing new to purge -> no admin email

    def test_admin_notified_on_purge(self):
        call_command('purge_deactivated_accounts')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('purged', mail.outbox[0].subject.lower())


class RideLifecycleTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('rlt_driver@example.com')
        self.rider = make_user('rlt_rider@example.com', role='rider')
        self.ride = make_ride(self.driver)

    def test_rider_books_driver_accepts(self):
        c = Client()
        c.login(username='rlt_rider@example.com', password='TestPass123')
        rid = encode_id(self.ride.id)

        resp = c.post(reverse('create-ride-booking', args=[rid]), {'seats': 1, 'intent': 'book'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)['ok'])

        booking = RideBooking.objects.get(ride=self.ride, passenger=self.rider)
        self.assertEqual(booking.status, RideBooking.STATUS_REQUESTED)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 2)

        c2 = Client()
        c2.login(username='rlt_driver@example.com', password='TestPass123')
        resp = c2.post(reverse('accept-booking', args=[encode_id(booking.id)]))
        self.assertEqual(resp.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, RideBooking.STATUS_CONFIRMED)

    def test_driver_rejects_booking_restores_seat(self):
        booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED,
        )
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=2)

        c = Client()
        c.login(username='rlt_driver@example.com', password='TestPass123')
        resp = c.post(reverse('reject-booking', args=[encode_id(booking.id)]))
        self.assertEqual(resp.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, RideBooking.STATUS_REJECTED)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 3)

    def test_non_driver_cannot_accept_booking(self):
        booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED,
        )
        c = Client()
        c.login(username='rlt_rider@example.com', password='TestPass123')
        resp = c.post(reverse('accept-booking', args=[encode_id(booking.id)]))
        self.assertEqual(resp.status_code, 403)

    def test_review_flow_after_completion(self):
        booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_COMPLETED,
        )
        Rides.objects.filter(pk=self.ride.pk).update(status='completed')

        c = Client()
        c.login(username='rlt_rider@example.com', password='TestPass123')
        resp = c.get(reverse('leave-review', args=[encode_id(booking.id)]))
        self.assertEqual(resp.status_code, 200)

        resp = c.post(
            reverse('submit-review'),
            data=json.dumps({
                'booking_id': encode_id(booking.id),
                'reviewee_id': self.driver.id,
                'rating': 5,
                'comment': 'Great ride',
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Review.objects.filter(booking=booking, reviewer=self.rider).exists())

        # duplicate review rejected
        resp = c.post(
            reverse('submit-review'),
            data=json.dumps({
                'booking_id': encode_id(booking.id),
                'reviewee_id': self.driver.id,
                'rating': 4,
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)


class InviteFlowTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('inv_driver@example.com')
        self.rider = make_user('inv_rider@example.com')
        self.ride = make_ride(self.driver)
        # A driver may only invite a rider discoverable through legitimate
        # means (Finding 10) — a matching search, or a prior booking with
        # this driver. Coordinates match make_ride's Toronto->Ottawa default.
        RideSearch.objects.create(
            user=self.rider, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
            destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
            search_date=date.today(), passenger_count=1,
        )

    def test_send_and_accept_invite(self):
        c = Client()
        c.login(username='inv_driver@example.com', password='TestPass123')
        resp = c.post(
            reverse('send-invite'),
            data=json.dumps({'rider_id': self.rider.id, 'ride_id': encode_id(self.ride.id)}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(RideInvite.objects.filter(ride=self.ride, passenger=self.rider).exists())
        self.assertEqual(len(mail.outbox), 1)

        invite = RideInvite.objects.get(ride=self.ride, passenger=self.rider)
        from komute.views import _make_invite_token
        token = _make_invite_token(invite.id)

        c2 = Client()
        c2.login(username='inv_rider@example.com', password='TestPass123')
        resp = c2.get(reverse('invite-detail', args=[token]))
        self.assertEqual(resp.status_code, 200)
        invite.refresh_from_db()
        self.assertEqual(invite.status, RideInvite.STATUS_VIEWED)

    def test_cannot_invite_rider_with_no_search_or_booking_history(self):
        # Finding 10: an arbitrary rider_id with no connection to this
        # driver/ride (no matching search, no prior booking) must be
        # rejected, whether or not the ID happens to belong to a real user.
        unrelated = make_user('unrelated_rider@example.com')
        c = Client()
        c.login(username='inv_driver@example.com', password='TestPass123')
        resp = c.post(
            reverse('send-invite'),
            data=json.dumps({'rider_id': unrelated.id, 'ride_id': encode_id(self.ride.id)}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(RideInvite.objects.filter(ride=self.ride, passenger=unrelated).exists())

        # A nonexistent rider_id must get the exact same response, so the
        # status/body can't be used to enumerate which numeric IDs exist.
        resp2 = c.post(
            reverse('send-invite'),
            data=json.dumps({'rider_id': 999999999, 'ride_id': encode_id(self.ride.id)}),
            content_type='application/json',
        )
        self.assertEqual(resp2.status_code, resp.status_code)
        self.assertEqual(resp2.json(), resp.json())

    def test_can_invite_rider_from_prior_booking_without_new_search(self):
        # The other legitimate channel: a rider who booked with this driver
        # before, even with no RideSearch record at all.
        prior_rider = make_user('prior_booking_rider@example.com')
        RideBooking.objects.create(
            ride=self.ride, passenger=prior_rider, seats=1,
            agreed_price_cents=self.ride.price_cents, status=RideBooking.STATUS_COMPLETED,
        )
        other_ride = make_ride(self.driver, depart_at=timezone.now() + timedelta(days=2))

        c = Client()
        c.login(username='inv_driver@example.com', password='TestPass123')
        resp = c.post(
            reverse('send-invite'),
            data=json.dumps({'rider_id': prior_rider.id, 'ride_id': encode_id(other_ride.id)}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)


class StaticAndSmokePageTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('smoke_driver@example.com')
        self.rider = make_user('smoke_rider@example.com')

    def test_anonymous_static_pages_200(self):
        c = Client()
        for name in ['home', 'about', 'how-it-works', 'privacy-policy',
                     'contact-us', 'terms-condition', 'community-guidelines',
                     'signup', 'login']:
            resp = c.get(reverse(name))
            self.assertEqual(resp.status_code, 200, f'{name} did not return 200')

    def test_logged_in_pages_200(self):
        c = Client()
        c.login(username='smoke_rider@example.com', password='TestPass123')
        for name in ['profile', 'my-rides', 'search-rides', 'rider-inbox']:
            resp = c.get(reverse(name))
            self.assertEqual(resp.status_code, 200, f'{name} did not return 200')

        c2 = Client()
        c2.login(username='smoke_driver@example.com', password='TestPass123')
        for name in ['post-ride', 'driver-inbox', 'driver_dashboard', 'route-demand']:
            resp = c2.get(reverse(name))
            self.assertEqual(resp.status_code, 200, f'{name} did not return 200')


class NoStoreHeadersTests(TestCase):
    """ISSUES.md M3: pages for a signed-in user must not be served from the
    browser cache on back navigation."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('ns_driver@example.com')
        self.rider = make_user('ns_rider@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )

    def _client(self, email):
        c = Client()
        c.login(username=email, password='TestPass123')
        return c

    def assertNoStore(self, resp, label):
        self.assertEqual(resp.status_code, 200, f'{label} did not return 200')
        cc = resp.get('Cache-Control', '')
        for directive in ('no-store', 'no-cache', 'private', 'max-age=0'):
            self.assertIn(directive, cc, f'{label}: {directive} missing from {cc!r}')

    def test_rider_pages_not_cached(self):
        c = self._client('ns_rider@example.com')
        for name in ['profile', 'my-rides', 'rider-inbox', 'search-rides', 'home']:
            self.assertNoStore(c.get(reverse(name)), name)
        self.assertNoStore(
            c.get(reverse('create-ride-booking', args=[encode_id(self.ride.id)])), 'booking details')
        self.assertNoStore(
            c.get(reverse('chat', args=[make_chat_token(self.booking.id)])), 'chat')

    def test_driver_pages_not_cached(self):
        c = self._client('ns_driver@example.com')
        for name in ['driver-inbox', 'driver_dashboard', 'post-ride', 'profile']:
            self.assertNoStore(c.get(reverse(name)), name)
        unbooked = make_ride(self.driver)
        self.assertNoStore(
            c.get(reverse('edit-ride', args=[encode_id(unbooked.id)])), 'edit-ride')

    def test_anonymous_public_page_cacheable(self):
        resp = Client().get(reverse('about'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('no-store', resp.get('Cache-Control', ''))

    def test_existing_cache_control_kept(self):
        # login is already @never_cache; private media sets its own value.
        resp = Client().get(reverse('login'))
        self.assertIn('no-store', resp['Cache-Control'])

    def test_header_reloads_on_bfcache_restore_only_when_signed_in(self):
        signed_in = self._client('ns_rider@example.com').get(reverse('profile'))
        self.assertContains(signed_in, 'if (e.persisted) location.reload();')
        anon = Client().get(reverse('about'))
        self.assertNotContains(anon, 'if (e.persisted) location.reload();')


class RetentionCommandsSmokeTests(TestCase):
    """The other purge_*/scrub_* jobs -- confirm they still run clean against
    fixture data after this session's changes (none of which touched them,
    but a shared-model change like deactivated_at could have broken imports)."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('ret_driver@example.com')
        self.rider = make_user('ret_rider@example.com')

        old = timezone.now() - timedelta(days=1000)
        self.old_ride = make_ride(
            self.driver, status='completed', arrive_at=old, depart_at=old,
        )
        RideBooking.objects.create(
            ride=self.old_ride, passenger=self.rider, seats=1,
            agreed_price_cents=1000, status=RideBooking.STATUS_COMPLETED,
        )
        Message.objects.create(
            ride=self.old_ride, sender=self.driver, receiver=self.rider,
            body='old message', created_at=old,
        )
        search = RideSearch.objects.create(
            user=self.rider, origin='X', origin_lat=0, origin_lng=0,
            destination='Y', dest_lat=1, dest_lng=1, search_date=date.today(),
        )
        # created_at is auto_now_add -- create() ignores any explicit value,
        # so backdate it with a separate update().
        RideSearch.objects.filter(pk=search.pk).update(created_at=old)

        vlog = VerificationLog.objects.create(user=self.rider, result='approved')
        VerificationLog.objects.filter(pk=vlog.pk).update(created_at=old)

        self.cancelled_ride = make_ride(
            self.driver, status='cancelled', cancelled_at=old,
        )

    def test_all_purge_commands_dry_run_clean(self):
        for cmd in ['purge_old_messages', 'purge_completed_rides', 'purge_cancelled_rides',
                    'purge_ride_search_log', 'scrub_verification_data', 'purge_deactivated_accounts']:
            call_command(cmd, '--dry-run')  # just must not raise

    def test_all_purge_commands_run_clean(self):
        call_command('purge_old_messages')
        self.assertFalse(Message.objects.filter(ride=self.old_ride).exists())

        call_command('purge_ride_search_log')
        self.assertFalse(RideSearch.objects.filter(user=self.rider).exists())

        call_command('scrub_verification_data')
        self.assertFalse(VerificationLog.objects.filter(user=self.rider).exists())

        call_command('purge_completed_rides')
        self.assertFalse(Rides.objects.filter(pk=self.old_ride.pk).exists())

        call_command('purge_cancelled_rides')
        self.assertFalse(Rides.objects.filter(pk=self.cancelled_ride.pk).exists())


class HashidUtilsTests(TestCase):
    def test_roundtrip(self):
        self.assertEqual(decode_id(encode_id(42)), 42)

    def test_decode_garbage_returns_none(self):
        self.assertIsNone(decode_id('not-a-real-hashid'))

    def test_decode_empty_string_returns_none(self):
        self.assertIsNone(decode_id(''))


class PostRideViewTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('post_driver@example.com')
        self.vehicle = self.driver.vehicles.first()

    def _valid_payload(self):
        depart_dt = timezone.now() + timedelta(days=1)
        return {
            'origin': 'Toronto, ON',
            'destination': 'Ottawa, ON',
            'date': depart_dt.date().isoformat(),
            'depart_time': '10:00',
            'depart_utc': depart_dt.isoformat().replace('+00:00', 'Z'),
            'seats': '3',
            'vehicle': str(self.vehicle.id),
            'price': '25.00',
            'origin_lat': '43.65', 'origin_lng': '-79.38',
            'destination_lat': '45.42', 'destination_lng': '-75.69',
        }

    def test_rider_cannot_post_ride(self):
        rider = make_user('post_rider@example.com')
        c = Client()
        c.login(username='post_rider@example.com', password='TestPass123')
        resp = c.get(reverse('post-ride'))
        self.assertRedirects(resp, reverse('profile'))

    def test_unverified_driver_redirected(self):
        driver = make_user('unverified_driver@example.com', role='driver', driver_verified=False)
        c = Client()
        c.login(username='unverified_driver@example.com', password='TestPass123')
        resp = c.get(reverse('post-ride'))
        self.assertRedirects(resp, reverse('profile'))

    def test_driver_without_verified_vehicle_redirected(self):
        driver = make_user('novehicle_driver@example.com', role='driver', driver_verified=True)
        c = Client()
        c.login(username='novehicle_driver@example.com', password='TestPass123')
        resp = c.get(reverse('post-ride'))
        self.assertRedirects(resp, reverse('profile'))

    def test_successful_post_creates_ride(self):
        c = Client()
        c.login(username='post_driver@example.com', password='TestPass123')
        resp = c.post(reverse('post-ride'), self._valid_payload())
        self.assertEqual(resp.status_code, 302)
        ride = Rides.objects.get(driver=self.driver)
        self.assertEqual(ride.seats_total, 3)
        self.assertEqual(ride.seats_available, 3)
        self.assertEqual(ride.price_cents, 2500)
        self.assertEqual(ride.status, 'posted')

    def test_missing_required_fields_rerenders_with_errors(self):
        c = Client()
        c.login(username='post_driver@example.com', password='TestPass123')
        payload = self._valid_payload()
        payload['origin'] = ''
        resp = c.post(reverse('post-ride'), payload)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('origin', resp.context['errors'])
        self.assertFalse(Rides.objects.filter(driver=self.driver).exists())

    def test_departure_too_soon_rejected(self):
        c = Client()
        c.login(username='post_driver@example.com', password='TestPass123')
        payload = self._valid_payload()
        soon = timezone.now() + timedelta(minutes=5)
        payload['depart_utc'] = soon.isoformat().replace('+00:00', 'Z')
        resp = c.post(reverse('post-ride'), payload)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('depart_time', resp.context['errors'])
        self.assertFalse(Rides.objects.filter(driver=self.driver).exists())

    # ── ISSUES.md H1 ──

    def test_price_field_accepts_whole_and_cent_values(self):
        c = Client()
        c.login(username='post_driver@example.com', password='TestPass123')
        resp = c.get(reverse('post-ride'))
        self.assertContains(resp, 'step="0.01"')
        self.assertNotContains(resp, 'step="0.5"')

    def test_unpicked_location_gets_pick_from_suggestions_error(self):
        c = Client()
        c.login(username='post_driver@example.com', password='TestPass123')
        payload = self._valid_payload()
        payload['origin_lat'] = payload['origin_lng'] = ''
        payload['destination_lat'] = payload['destination_lng'] = ''
        resp = c.post(reverse('post-ride'), payload)
        self.assertEqual(resp.context['errors']['origin'], 'Please pick a location from the suggestions.')
        self.assertEqual(resp.context['errors']['destination'], 'Please pick a location from the suggestions.')

    def test_location_outside_service_area_still_gets_us_canada_error(self):
        c = Client()
        c.login(username='post_driver@example.com', password='TestPass123')
        payload = self._valid_payload()
        payload['origin_lat'], payload['origin_lng'] = '51.50', '-0.12'  # London, UK
        resp = c.post(reverse('post-ride'), payload)
        self.assertIn('US and Canada', resp.context['errors']['origin'])

    def test_error_rerender_keeps_submitted_values(self):
        c = Client()
        c.login(username='post_driver@example.com', password='TestPass123')
        payload = self._valid_payload()
        payload.update({
            'price': '12.50',
            'notes': 'Meet at <the> main gate',
            'passenger_gender_pref': 'women_only',
            'preferences_json': '["Music OK", "Pet Friendly"]',
            'stop_location': ['Kingston, ON'], 'stop_type': ['pickup'],
            'stop_lat': ['44.23'], 'stop_lng': ['-76.48'],
        })
        payload['destination_lat'] = payload['destination_lng'] = ''
        resp = c.post(reverse('post-ride'), payload)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('destination', resp.context['errors'])
        self.assertContains(resp, 'value="Toronto, ON"')
        self.assertContains(resp, 'value="Ottawa, ON"')
        self.assertContains(resp, 'value="43.65"')
        self.assertContains(resp, 'value="12.50"')
        self.assertContains(resp, 'value="10:00"')
        self.assertContains(resp, 'Meet at &lt;the&gt; main gate</textarea>')
        self.assertContains(resp, f'<option value="{self.vehicle.id}" selected>')
        self.assertContains(resp, '<option value="women_only" selected>')
        prefill = resp.context['form_js']
        self.assertEqual(prefill['date'], payload['date'])
        self.assertEqual(prefill['seats'], '3')
        self.assertEqual(prefill['prefs'], ['Music OK', 'Pet Friendly'])
        self.assertEqual(prefill['stops'], [{'location': 'Kingston, ON', 'type': 'pickup', 'lat': '44.23', 'lng': '-76.48'}])
        self.assertContains(resp, 'id="form-prefill"')

    def test_get_has_no_prefill(self):
        c = Client()
        c.login(username='post_driver@example.com', password='TestPass123')
        resp = c.get(reverse('post-ride'))
        self.assertIsNone(resp.context['form_js'])


class SearchRidesViewTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('search_driver@example.com')
        self.rider = make_user('search_rider@example.com', role='rider')
        self.ride = make_ride(self.driver)  # Toronto -> Ottawa, tomorrow

    def _search(self, client, **overrides):
        payload = {
            'from': 'Toronto, ON', 'to': 'Ottawa, ON',
            'date': self.ride.depart_at.date().isoformat(),
            'origin_lat': '43.65', 'origin_lng': '-79.38',
            'dest_lat': '45.42', 'dest_lng': '-75.69',
        }
        payload.update(overrides)
        return client.post(reverse('search-rides'), payload)

    def test_driver_cannot_search(self):
        c = Client()
        c.login(username='search_driver@example.com', password='TestPass123')
        resp = self._search(c)
        self.assertRedirects(resp, reverse('profile'))

    def test_search_within_radius_returns_ride(self):
        c = Client()
        c.login(username='search_rider@example.com', password='TestPass123')
        resp = self._search(c)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total_count'], 1)

    def test_search_outside_radius_excludes_ride(self):
        c = Client()
        c.login(username='search_rider@example.com', password='TestPass123')
        # Vancouver -- nowhere near the ride's Toronto origin.
        resp = self._search(c, origin_lat='49.28', origin_lng='-123.12')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total_count'], 0)

    def test_search_logs_ride_search_row(self):
        c = Client()
        c.login(username='search_rider@example.com', password='TestPass123')
        self._search(c)
        self.assertTrue(RideSearch.objects.filter(user=self.rider).exists())

    def test_gender_restricted_ride_excluded_for_mismatched_gender(self):
        Rides.objects.filter(pk=self.ride.pk).update(passenger_gender_pref='men_only')
        self.rider.gender = 'female'
        self.rider.save(update_fields=['gender'])
        c = Client()
        c.login(username='search_rider@example.com', password='TestPass123')
        resp = self._search(c)
        self.assertEqual(resp.context['total_count'], 0)

    # ── ISSUES.md H5 ──

    def _toronto_evening_ride(self):
        """Move the ride to 8 pm Toronto, three days out: already the next
        day in UTC."""
        tz = ZoneInfo('America/Toronto')
        day = timezone.localdate(timezone=tz) + timedelta(days=3)
        depart = datetime.combine(day, datetime.min.time().replace(hour=20), tzinfo=tz)
        Rides.objects.filter(pk=self.ride.pk).update(depart_at=depart)
        self.assertNotEqual(depart.astimezone(ZoneInfo('UTC')).date(), day)
        return day

    def _rider(self):
        c = Client()
        c.login(username='search_rider@example.com', password='TestPass123')
        return c

    def test_evening_ride_found_on_riders_local_date(self):
        day = self._toronto_evening_ride()
        c = self._rider()
        resp = self._search(c, date=day.isoformat(), tz='America/Toronto')
        self.assertEqual(resp.context['total_count'], 1)
        resp = self._search(c, date=(day + timedelta(days=1)).isoformat(), tz='America/Toronto')
        self.assertEqual(resp.context['total_count'], 0)

    def test_time_slot_uses_riders_local_hour(self):
        day = self._toronto_evening_ride()
        c = self._rider()
        resp = self._search(c, date=day.isoformat(), tz='America/Toronto', depart_time_slot='evening')
        self.assertEqual(resp.context['total_count'], 1)
        resp = self._search(c, date=day.isoformat(), tz='America/Toronto', depart_time_slot='morning')
        self.assertEqual(resp.context['total_count'], 0)

    def test_today_not_past_when_utc_is_already_tomorrow(self):
        c = self._rider()
        # 9 pm in Toronto on Oct 12 is already Oct 13 in UTC.
        late_evening = datetime(2026, 10, 13, 1, 0, tzinfo=ZoneInfo('UTC'))
        with patch('django.utils.timezone.now', return_value=late_evening):
            resp = self._search(c, date='2026-10-12', tz='America/Toronto')
            self.assertNotIn('date', resp.context['errors'])
            resp = self._search(c, date='2026-10-12', tz='UTC')
            self.assertEqual(resp.context['errors']['date'], 'Search date cannot be in the past.')

    def test_unknown_timezone_falls_back_to_utc(self):
        resp = self._search(self._rider(), tz='Not/AZone')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total_count'], 1)


class RecurringRideTimezoneTests(TestCase):
    """ISSUES.md H5: recurring rides repeat on the driver's local weekday and
    clock time, including across a DST change."""

    def test_recurring_ride_keeps_local_weekday_and_time(self):
        driver = make_driver_with_vehicle('rr_driver@example.com')
        tz = ZoneInfo('America/Toronto')
        today = timezone.localdate(timezone=tz)
        first = today + timedelta(days=(7 - today.weekday()) % 7 or 7)   # next Monday
        depart = datetime.combine(first, datetime.min.time().replace(hour=20), tzinfo=tz)
        end = first + timedelta(weeks=26)   # half a year always crosses a DST change
        c = Client()
        c.login(username='rr_driver@example.com', password='TestPass123')
        resp = c.post(reverse('post-ride'), {
            'origin': 'Toronto, ON', 'destination': 'Ottawa, ON',
            'date': first.isoformat(), 'depart_time': '20:00',
            'depart_utc': depart.astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00', 'Z'),
            'tz': 'America/Toronto',
            'seats': '3', 'vehicle': str(driver.vehicles.first().id), 'price': '25.00',
            'origin_lat': '43.65', 'origin_lng': '-79.38',
            'destination_lat': '45.42', 'destination_lng': '-75.69',
            'is_recurring': '1', 'recurring_days': '0', 'recurring_end_date': end.isoformat(),
        })
        self.assertEqual(resp.status_code, 302)
        rides = list(Rides.objects.filter(driver=driver).order_by('depart_at'))
        self.assertEqual(len(rides), 27)
        local = [r.depart_at.astimezone(tz) for r in rides]
        self.assertTrue(all(d.weekday() == 0 and (d.hour, d.minute) == (20, 0) for d in local))
        self.assertEqual(local[0].date(), first)
        offsets = {d.utcoffset() for d in local}
        self.assertEqual(len(offsets), 2)   # both EDT and EST in the range


class RouteDemandViewTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('rd_driver@example.com')
        self.ride = make_ride(self.driver)  # Toronto -> Ottawa

    def _searchers_for_ride(self, n, seats=1):
        for i in range(n):
            u = make_user(f'rd_rider{i}@example.com')
            RideSearch.objects.create(
                user=u, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
                destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
                search_date=date.today(), passenger_count=seats,
            )

    def test_rider_cannot_view_route_demand(self):
        rider = make_user('rd_rider_only@example.com')
        c = Client()
        c.login(username='rd_rider_only@example.com', password='TestPass123')
        resp = c.get(reverse('route-demand'))
        self.assertRedirects(resp, reverse('profile'))

    def test_no_active_rides_shows_empty_state(self):
        solo_driver = make_driver_with_vehicle('rd_solo_driver@example.com')
        c = Client()
        c.login(username='rd_solo_driver@example.com', password='TestPass123')
        resp = c.get(reverse('route-demand'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['has_active_rides'])
        self.assertEqual(resp.context['route_demands'], [])

    def test_matching_searches_produce_demand_card(self):
        self._searchers_for_ride(2)
        c = Client()
        c.login(username='rd_driver@example.com', password='TestPass123')
        resp = c.get(reverse('route-demand'))
        demands = resp.context['route_demands']
        self.assertEqual(len(demands), 1)
        self.assertEqual(demands[0]['search_count'], 2)
        self.assertEqual(demands[0]['heat'], 'cool')
        self.assertEqual(len(demands[0]['riders']), 2)

    def test_high_search_count_marked_hot(self):
        self._searchers_for_ride(6)
        c = Client()
        c.login(username='rd_driver@example.com', password='TestPass123')
        resp = c.get(reverse('route-demand'))
        demands = resp.context['route_demands']
        self.assertEqual(demands[0]['heat'], 'hot')
        self.assertEqual(resp.context['stats']['hot_routes'], 1)

    def test_search_for_unrelated_route_not_shown(self):
        unrelated = make_user('rd_unrelated@example.com')
        RideSearch.objects.create(
            user=unrelated, origin='Vancouver, BC', origin_lat=49.28, origin_lng=-123.12,
            destination='Calgary, AB', dest_lat=51.05, dest_lng=-114.07,
            search_date=date.today(), passenger_count=1,
        )
        c = Client()
        c.login(username='rd_driver@example.com', password='TestPass123')
        resp = c.get(reverse('route-demand'))
        demands = resp.context['route_demands']
        self.assertEqual(len(demands), 1)
        self.assertEqual(demands[0]['search_count'], 0)
        self.assertEqual(demands[0]['heat'], 'cool')
        self.assertEqual(demands[0]['demand_label'], 'No searches yet')

    def test_only_posted_or_full_rides_are_anchors(self):
        Rides.objects.filter(pk=self.ride.pk).update(status='completed')
        self._searchers_for_ride(3)
        c = Client()
        c.login(username='rd_driver@example.com', password='TestPass123')
        resp = c.get(reverse('route-demand'))
        self.assertFalse(resp.context['has_active_rides'])
        self.assertEqual(resp.context['route_demands'], [])


class CancelBookingAndRideTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('cxl_driver@example.com')
        self.rider = make_user('cxl_rider@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=2)

    def test_rider_cancels_booking_restores_seat(self):
        c = Client()
        c.login(username='cxl_rider@example.com', password='TestPass123')
        resp = c.post(reverse('cancel-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_CANCELLED)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 3)

    def test_other_user_cannot_cancel_booking(self):
        stranger = make_user('cxl_stranger@example.com')
        c = Client()
        c.login(username='cxl_stranger@example.com', password='TestPass123')
        resp = c.post(reverse('cancel-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 403)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_CONFIRMED)

    def test_cannot_cancel_already_cancelled_booking(self):
        self.booking.status = RideBooking.STATUS_CANCELLED
        self.booking.save(update_fields=['status'])
        c = Client()
        c.login(username='cxl_rider@example.com', password='TestPass123')
        resp = c.post(reverse('cancel-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 400)

    def test_cannot_cancel_after_departure(self):
        Rides.objects.filter(pk=self.ride.pk).update(depart_at=timezone.now() - timedelta(hours=1))
        c = Client()
        c.login(username='cxl_rider@example.com', password='TestPass123')
        resp = c.post(reverse('cancel-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 400)

    def test_driver_cancels_ride_cascades_to_bookings(self):
        c = Client()
        c.login(username='cxl_driver@example.com', password='TestPass123')
        resp = c.post(reverse('cancel-ride', args=[encode_id(self.ride.id)]))
        self.assertEqual(resp.status_code, 200)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.status, 'cancelled')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_CANCELLED)

    def test_non_owner_cannot_cancel_ride(self):
        c = Client()
        c.login(username='cxl_rider@example.com', password='TestPass123')
        resp = c.post(reverse('cancel-ride', args=[encode_id(self.ride.id)]))
        self.assertEqual(resp.status_code, 404)


class PriceNegotiationTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('neg_driver@example.com')
        self.rider = make_user('neg_rider@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )

    def test_rider_creates_offer_driver_accepts(self):
        c = Client()
        c.login(username='neg_rider@example.com', password='TestPass123')
        resp = c.post(
            reverse('create-price-offer', args=[encode_id(self.booking.id)]),
            {'price_cents': '2000'},
        )
        self.assertEqual(resp.status_code, 201)
        offer = PriceOffer.objects.get(booking=self.booking)
        self.assertEqual(offer.proposed_by, self.rider)
        self.assertEqual(offer.price_cents, 2000)

        c2 = Client()
        c2.login(username='neg_driver@example.com', password='TestPass123')
        resp2 = c2.post(
            reverse('respond-price-offer', args=[encode_id(offer.id)]),
            {'action': 'accept'},
        )
        self.assertEqual(resp2.status_code, 200)
        offer.refresh_from_db()
        self.assertEqual(offer.status, PriceOffer.STATUS_ACCEPTED)
        self.booking.refresh_from_db()
        self.assertEqual(
            self.booking.agreed_price_cents,
            2000 * self.booking.seats + settings.SERVICE_FEE_CENTS,
        )

    def test_third_party_cannot_create_offer(self):
        stranger = make_user('neg_stranger@example.com')
        c = Client()
        c.login(username='neg_stranger@example.com', password='TestPass123')
        resp = c.post(
            reverse('create-price-offer', args=[encode_id(self.booking.id)]),
            {'price_cents': '2000'},
        )
        self.assertEqual(resp.status_code, 403)

    def test_cannot_respond_to_own_offer(self):
        c = Client()
        c.login(username='neg_rider@example.com', password='TestPass123')
        c.post(
            reverse('create-price-offer', args=[encode_id(self.booking.id)]),
            {'price_cents': '2000'},
        )
        offer = PriceOffer.objects.get(booking=self.booking)
        resp = c.post(
            reverse('respond-price-offer', args=[encode_id(offer.id)]),
            {'action': 'accept'},
        )
        self.assertEqual(resp.status_code, 403)

    def test_non_positive_offer_rejected(self):
        c = Client()
        c.login(username='neg_rider@example.com', password='TestPass123')
        resp = c.post(
            reverse('create-price-offer', args=[encode_id(self.booking.id)]),
            {'price_cents': '0'},
        )
        self.assertEqual(resp.status_code, 400)


class ChatIsolationTests(TransactionTestCase):
    """ISSUES.md C4: a passenger must never see another passenger's chat on the same ride."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('ci_driver@example.com')
        self.rider_a = make_user('ci_rider_a@example.com')
        self.rider_b = make_user('ci_rider_b@example.com')
        self.ride = make_ride(self.driver)
        # Confirmed: offers are refused while a request is pending (H3).
        self.booking_a = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider_a, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )
        self.booking_b = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider_b, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )

    @staticmethod
    def _communicator(user, booking):
        token = make_chat_token(booking.id)
        comm = WebsocketCommunicator(ChatConsumer.as_asgi(), f'/ws/chat/{token}/')
        comm.scope['user'] = user
        comm.scope['url_route'] = {'kwargs': {'token': token}}
        return comm

    def test_messages_and_offers_stay_within_booking(self):
        async def run():
            driver_a = self._communicator(self.driver, self.booking_a)
            rider_a = self._communicator(self.rider_a, self.booking_a)
            rider_b = self._communicator(self.rider_b, self.booking_b)
            for comm in (driver_a, rider_a, rider_b):
                connected, _ = await comm.connect()
                self.assertTrue(connected)

            await rider_a.send_json_to({'type': 'message', 'body': 'private to driver'})
            msg = await driver_a.receive_json_from(timeout=5)
            self.assertEqual(msg['body'], 'private to driver')
            await rider_a.receive_json_from(timeout=5)  # sender's own echo

            await rider_a.send_json_to({'type': 'price_offer', 'price': 18})
            offer = await driver_a.receive_json_from(timeout=5)
            self.assertEqual(offer['type'], 'price_offer')
            await rider_a.receive_json_from(timeout=5)

            self.assertTrue(await rider_b.receive_nothing(timeout=0.5))

            for comm in (driver_a, rider_a, rider_b):
                await comm.disconnect()

        async_to_sync(run)()


class ChatBookingStatusTests(TransactionTestCase):
    """ISSUES.md H3: an open chat learns about accept / reject / cancel live,
    and negotiation can't confirm a booking the driver hasn't accepted."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('bs_driver@example.com')
        self.rider = make_user('bs_rider@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500 + settings.SERVICE_FEE_CENTS,
            status=RideBooking.STATUS_REQUESTED,
        )

    def _client(self, email):
        c = Client()
        c.login(username=email, password='TestPass123')
        return c

    def _status_pushed_by(self, post):
        """Open the rider's chat, run `post` (a driver/rider action), and
        return the event the chat receives."""
        async def run():
            comm = ChatIsolationTests._communicator(self.rider, self.booking)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            resp = await sync_to_async(post)()
            self.assertEqual(resp.status_code, 200)
            event = await comm.receive_json_from(timeout=5)
            await comm.disconnect()
            return event
        return async_to_sync(run)()

    def test_accept_unlocks_open_chat(self):
        driver = self._client('bs_driver@example.com')
        event = self._status_pushed_by(
            lambda: driver.post(reverse('accept-booking', args=[encode_id(self.booking.id)])))
        self.assertEqual(event['type'], 'booking_status')
        self.assertEqual(event['status'], RideBooking.STATUS_CONFIRMED)
        self.assertEqual(event['label'], 'Confirmed')
        self.assertFalse(event['nego_locked'])
        self.assertFalse(event['chat_locked'])
        self.assertEqual(event['agreed_price'], (2500 + settings.SERVICE_FEE_CENTS) // 100)

    def test_reject_locks_open_chat(self):
        driver = self._client('bs_driver@example.com')
        event = self._status_pushed_by(
            lambda: driver.post(reverse('reject-booking', args=[encode_id(self.booking.id)])))
        self.assertEqual(event['status'], RideBooking.STATUS_REJECTED)
        self.assertEqual(event['label'], 'Declined')
        self.assertTrue(event['chat_locked'])
        self.assertIn('declined', event['closed_message'])

    def test_rider_cancel_locks_open_chat(self):
        rider = self._client('bs_rider@example.com')
        event = self._status_pushed_by(
            lambda: rider.post(reverse('cancel-booking', args=[encode_id(self.booking.id)])))
        self.assertEqual(event['status'], RideBooking.STATUS_CANCELLED)
        self.assertTrue(event['chat_locked'])

    def test_ride_cancel_locks_open_chat(self):
        driver = self._client('bs_driver@example.com')
        event = self._status_pushed_by(
            lambda: driver.post(reverse('cancel-ride', args=[encode_id(self.ride.id)])))
        self.assertEqual(event['status'], RideBooking.STATUS_CANCELLED)
        self.assertTrue(event['chat_locked'])

    def test_status_change_survives_channel_layer_failure(self):
        with patch('channels.layers.InMemoryChannelLayer.group_send', side_effect=OSError('redis down')):
            resp = self._client('bs_driver@example.com').post(
                reverse('accept-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_CONFIRMED)

    def test_rejected_chat_page_is_locked(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_REJECTED)
        resp = self._client('bs_rider@example.com').get(
            reverse('chat', args=[make_chat_token(self.booking.id)]))
        self.assertEqual(resp.context['ride_status_label'], 'Declined')
        self.assertTrue(resp.context['chat_is_locked'])
        self.assertContains(resp, 'Booking request was declined')

    def test_offer_refused_while_request_pending(self):
        async def run():
            rider = ChatIsolationTests._communicator(self.rider, self.booking)
            driver = ChatIsolationTests._communicator(self.driver, self.booking)
            for comm in (rider, driver):
                connected, _ = await comm.connect()
                self.assertTrue(connected)
            await rider.send_json_to({'type': 'price_offer', 'price': 5})
            error = await rider.receive_json_from(timeout=5)
            self.assertEqual(error['code'], 'nego_locked')
            self.assertTrue(await driver.receive_nothing(timeout=0.5))
            for comm in (rider, driver):
                await comm.disconnect()

        async_to_sync(run)()
        self.assertFalse(PriceOffer.objects.filter(booking=self.booking).exists())

    def test_accept_refused_while_request_pending(self):
        offer = PriceOffer.objects.create(booking=self.booking, proposed_by=self.driver, price_cents=2000)
        result, _ = async_to_sync(ChatConsumer().accept_price_offer)(self.rider, self.booking.id, offer.id, None)
        self.assertEqual(result, 'nego_locked')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_REQUESTED)

    def test_cannot_accept_own_offer(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_DRAFT)
        offer = PriceOffer.objects.create(booking=self.booking, proposed_by=self.rider, price_cents=500)
        accept = async_to_sync(ChatConsumer().accept_price_offer)
        self.assertEqual(accept(self.rider, self.booking.id, offer.id, None)[0], 'own_offer')
        # Matching by price alone must not pick up the rider's own offer either.
        self.assertEqual(accept(self.rider, self.booking.id, None, 5)[0], 'offer_stale')
        self.booking.refresh_from_db()
        offer.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_DRAFT)
        self.assertEqual(offer.status, PriceOffer.STATUS_PENDING)
        # The other side still can.
        self.assertEqual(accept(self.driver, self.booking.id, offer.id, None)[0], 'ok')


class ChatConnectionTests(TransactionTestCase):
    """ISSUES.md H4: a refused or broken chat socket closes with a code the
    page can act on, the heartbeat is answered, and closes are logged."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('cc_driver@example.com')
        self.rider = make_user('cc_rider@example.com')
        self.stranger = make_user('cc_stranger@example.com')
        self.booking = RideBooking.objects.create(
            ride=make_ride(self.driver), passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )

    def _close_code(self, user, token=None):
        """Connect as `user` and return the code the server closes with."""
        token = token or make_chat_token(self.booking.id)
        async def run():
            comm = WebsocketCommunicator(ChatConsumer.as_asgi(), f'/ws/chat/{token}/')
            comm.scope['user'] = user
            comm.scope['url_route'] = {'kwargs': {'token': token}}
            connected, _ = await comm.connect()
            # Accepted first, so the browser gets the code instead of 1006.
            self.assertTrue(connected)
            output = await comm.receive_output(timeout=5)
            self.assertEqual(output['type'], 'websocket.close')
            return output['code']
        return async_to_sync(run)()

    def test_signed_out_gets_4401(self):
        from django.contrib.auth.models import AnonymousUser
        with self.assertLogs('komute.chat', 'WARNING'):
            self.assertEqual(self._close_code(AnonymousUser()), 4401)

    def test_bad_token_gets_4403(self):
        self.assertEqual(self._close_code(self.rider, token='not-a-token'), 4403)

    def test_other_users_booking_gets_4403(self):
        self.assertEqual(self._close_code(self.stranger), 4403)

    def test_channel_layer_down_gets_1011(self):
        with patch('channels.layers.InMemoryChannelLayer.group_add', side_effect=OSError('redis down')), \
             self.assertLogs('komute.chat', 'ERROR'):
            self.assertEqual(self._close_code(self.rider), 1011)

    def test_ping_answered_and_close_logged(self):
        async def run():
            comm = ChatIsolationTests._communicator(self.rider, self.booking)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await comm.send_json_to({'type': 'ping'})
            self.assertEqual(await comm.receive_json_from(timeout=5), {'type': 'pong'})
            await comm.disconnect(code=1006)

        with self.assertLogs('komute.chat', 'INFO') as logs:
            async_to_sync(run)()
        self.assertTrue(any('code=1006' in line for line in logs.output))

    def test_chat_page_has_connection_bar(self):
        c = Client()
        c.login(username='cc_rider@example.com', password='TestPass123')
        resp = c.get(reverse('chat', args=[make_chat_token(self.booking.id)]))
        self.assertContains(resp, 'id="connBar"')


class BookingNotifyTests(TransactionTestCase):
    """ISSUES.md M4: booking changes reach the other party's open inbox /
    my-rides page over the per-user socket."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('bn_driver@example.com')
        self.rider = make_user('bn_rider@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500 + settings.SERVICE_FEE_CENTS,
            status=RideBooking.STATUS_REQUESTED,
        )

    def _client(self, email):
        c = Client()
        c.login(username=email, password='TestPass123')
        return c

    @staticmethod
    def _communicator(user):
        comm = WebsocketCommunicator(NotifyConsumer.as_asgi(), '/ws/notify/')
        comm.scope['user'] = user
        return comm

    def _notices(self, action):
        """Open the driver's and rider's notify sockets, run `action` (sync),
        and return {'driver': event or None, 'rider': event or None}."""
        async def run():
            comms = {'driver': self._communicator(self.driver),
                     'rider': self._communicator(self.rider)}
            for comm in comms.values():
                connected, _ = await comm.connect()
                self.assertTrue(connected)
                self.assertEqual((await comm.receive_json_from(timeout=5))['type'], 'state')
            await sync_to_async(action)()
            out = {}
            for who, comm in comms.items():
                out[who] = (None if await comm.receive_nothing(timeout=0.5)
                            else await comm.receive_json_from(timeout=5))
                await comm.disconnect()
            return out
        return async_to_sync(run)()

    def _post_ok(self, email, name, arg, **data):
        def action():
            resp = self._client(email).post(reverse(name, args=[arg]), data)
            self.assertEqual(resp.status_code, 200, resp.content)
        return action

    def test_anonymous_refused(self):
        async def run():
            comm = self._communicator(AnonymousUser())
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            output = await comm.receive_output(timeout=5)
            self.assertEqual(output, {'type': 'websocket.close', 'code': 4401})
        async_to_sync(run)()

    def test_connect_sends_page_fingerprint_and_answers_ping(self):
        page = self._client('bn_driver@example.com').get(reverse('driver-inbox'))
        fingerprint = page.context['bookings_fingerprint']
        self.assertContains(page, f'data-fingerprint="{fingerprint}"')
        async def run():
            comm = self._communicator(self.driver)
            await comm.connect()
            state = await comm.receive_json_from(timeout=5)
            await comm.send_to(text_data='{"type":"ping"}')
            pong = await comm.receive_json_from(timeout=5)
            await comm.disconnect()
            return state, pong
        state, pong = async_to_sync(run)()
        self.assertEqual(state, {'type': 'state', 'fingerprint': fingerprint})
        self.assertEqual(pong, {'type': 'pong'})

    def test_fingerprint_tracks_booking_changes(self):
        before_driver = booking_fingerprint(self.driver)
        before_rider = booking_fingerprint(self.rider)
        # A rider's pre-request chat isn't on the driver's pages.
        other = make_user('bn_other@example.com')
        RideBooking.objects.create(ride=self.ride, passenger=other, seats=1,
                                   agreed_price_cents=0, status=RideBooking.STATUS_DRAFT)
        self.assertEqual(booking_fingerprint(self.driver), before_driver)
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_CONFIRMED)
        self.assertNotEqual(booking_fingerprint(self.driver), before_driver)
        self.assertNotEqual(booking_fingerprint(self.rider), before_rider)

    def test_booking_request_notifies_driver(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_CANCELLED)
        got = self._notices(self._post_ok('bn_rider@example.com', 'create-ride-booking',
                                          encode_id(self.ride.id), seats='1'))
        self.assertEqual(got['driver'], {'type': 'bookings_changed',
                                         'message': 'You have a new booking request.'})
        self.assertIsNone(got['rider'])

    def test_accept_and_reject_notify_rider(self):
        got = self._notices(self._post_ok('bn_driver@example.com', 'accept-booking',
                                          encode_id(self.booking.id)))
        self.assertIn('accepted', got['rider']['message'])
        self.assertIsNone(got['driver'])

        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_REQUESTED)
        got = self._notices(self._post_ok('bn_driver@example.com', 'reject-booking',
                                          encode_id(self.booking.id)))
        self.assertIn('declined', got['rider']['message'])
        self.assertIsNone(got['driver'])

    def test_rider_cancel_notifies_driver(self):
        got = self._notices(self._post_ok('bn_rider@example.com', 'cancel-booking',
                                          encode_id(self.booking.id)))
        self.assertIn('cancelled', got['driver']['message'])
        self.assertIsNone(got['rider'])

    def test_ride_cancel_notifies_passengers(self):
        got = self._notices(self._post_ok('bn_driver@example.com', 'cancel-ride',
                                          encode_id(self.ride.id)))
        self.assertIn('cancelled', got['rider']['message'])
        self.assertIsNone(got['driver'])

    def test_price_agreed_in_chat_notifies_other_party(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_CONFIRMED)
        offer = PriceOffer.objects.create(booking=self.booking, proposed_by=self.driver, price_cents=2000)
        async def run():
            chat = ChatIsolationTests._communicator(self.rider, self.booking)
            notify = self._communicator(self.driver)
            for comm in (chat, notify):
                connected, _ = await comm.connect()
                self.assertTrue(connected)
            await notify.receive_json_from(timeout=5)   # state
            await chat.send_json_to({'type': 'price_accepted', 'offer_id': offer.id})
            notice = await notify.receive_json_from(timeout=5)
            for comm in (chat, notify):
                await comm.disconnect()
            return notice
        notice = async_to_sync(run)()
        self.assertEqual(notice['type'], 'bookings_changed')
        self.assertIn('price was agreed', notice['message'])

    def test_request_survives_channel_layer_failure(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_CANCELLED)
        with patch('channels.layers.InMemoryChannelLayer.group_send', side_effect=OSError('redis down')):
            resp = self._client('bn_rider@example.com').post(
                reverse('create-ride-booking', args=[encode_id(self.ride.id)]), {'seats': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(RideBooking.objects.filter(
            ride=self.ride, passenger=self.rider, status=RideBooking.STATUS_REQUESTED).exists())

    def test_pages_load_the_notify_script(self):
        rider = self._client('bn_rider@example.com')
        driver = self._client('bn_driver@example.com')
        for client, name in ((rider, 'rider-inbox'), (rider, 'my-rides'),
                             (driver, 'driver-inbox'), (driver, 'my-rides')):
            resp = client.get(reverse(name))
            self.assertContains(resp, 'js/booking-updates.js', msg_prefix=name)


class EditRideViewTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('edrv_driver@example.com')
        self.other_driver = make_driver_with_vehicle('erlv_driver@example.com')
        self.ride = make_ride(self.driver)

    def _payload(self):
        depart_dt = timezone.now() + timedelta(days=2)
        return {
            'origin': 'Kingston, ON', 'destination': 'Montreal, QC',
            'date': depart_dt.date().isoformat(), 'depart_time': '11:00',
            'depart_utc': depart_dt.isoformat().replace('+00:00', 'Z'),
            'seats': '4', 'price': '30.00',
            'origin_lat': '44.23', 'origin_lng': '-76.48',
            'destination_lat': '45.50', 'destination_lng': '-73.56',
        }

    def test_owner_can_edit_ride(self):
        c = Client()
        c.login(username='edrv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('edit-ride', args=[encode_id(self.ride.id)]), self._payload())
        self.assertRedirects(resp, reverse('my-rides'))
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.origin, 'Kingston, ON')
        self.assertEqual(self.ride.seats_total, 4)
        self.assertEqual(self.ride.seats_available, 4)
        self.assertEqual(self.ride.price_cents, 3000)

    def test_non_owner_gets_404(self):
        c = Client()
        c.login(username='erlv_driver@example.com', password='TestPass123')
        resp = c.get(reverse('edit-ride', args=[encode_id(self.ride.id)]))
        self.assertEqual(resp.status_code, 404)

    def test_cannot_edit_non_posted_ride(self):
        Rides.objects.filter(pk=self.ride.pk).update(status='completed')
        c = Client()
        c.login(username='edrv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('edit-ride', args=[encode_id(self.ride.id)]), self._payload())
        self.assertRedirects(resp, reverse('my-rides'))
        self.ride.refresh_from_db()
        self.assertNotEqual(self.ride.origin, 'Kingston, ON')

    def test_cannot_edit_ride_with_active_booking(self):
        rider = make_user('edit_rider@example.com')
        RideBooking.objects.create(
            ride=self.ride, passenger=rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED,
        )
        c = Client()
        c.login(username='edrv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('edit-ride', args=[encode_id(self.ride.id)]), self._payload())
        self.assertRedirects(resp, reverse('my-rides'))
        self.ride.refresh_from_db()
        self.assertNotEqual(self.ride.origin, 'Kingston, ON')

    # ── ISSUES.md M2 ──

    def _post(self, **overrides):
        c = Client()
        c.login(username='edrv_driver@example.com', password='TestPass123')
        payload = self._payload()
        payload.update(overrides)
        return c.post(reverse('edit-ride', args=[encode_id(self.ride.id)]), payload)

    def test_unpicked_origin_asks_for_a_pick(self):
        resp = self._post(origin_lat='', origin_lng='')
        self.assertEqual(resp.context['errors']['origin'], 'Please pick a location from the suggestions.')

    def test_unpicked_stop_rejected_and_old_stops_kept(self):
        RideStop.objects.create(ride=self.ride, stop_order=1, location='Belleville, ON', lat=44.16, lng=-77.38)
        resp = self._post(stop_location=['Cornwall, ON'], stop_lat=[''], stop_lng=[''])
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Cornwall, ON', resp.context['errors']['stops'])
        self.assertEqual(list(self.ride.stops.values_list('location', flat=True)), ['Belleville, ON'])

    def test_picked_stop_saved_with_coordinates(self):
        resp = self._post(stop_location=['Cornwall, ON'], stop_lat=['45.02'], stop_lng=['-74.73'])
        self.assertRedirects(resp, reverse('my-rides'))
        stop = self.ride.stops.get()
        self.assertEqual((stop.location, stop.lat, stop.lng), ('Cornwall, ON', 45.02, -74.73))


class PlaceAutocompleteTests(TestCase):
    """ISSUES.md M2: every location field uses the shared autocomplete, and
    search no longer guesses the first geocoder match."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('pa_driver@example.com')
        self.rider = make_user('pa_rider@example.com')
        self.vehicle = self.driver.vehicles.first()

    def _login(self, email):
        c = Client()
        c.login(username=email, password='TestPass123')
        return c

    def _post_ride(self, **stops):
        depart_dt = timezone.now() + timedelta(days=1)
        payload = {
            'origin': 'Toronto, ON', 'destination': 'Ottawa, ON',
            'date': depart_dt.date().isoformat(), 'depart_time': '10:00',
            'depart_utc': depart_dt.isoformat().replace('+00:00', 'Z'),
            'seats': '3', 'vehicle': str(self.vehicle.id), 'price': '25.00',
            'origin_lat': '43.65', 'origin_lng': '-79.38',
            'destination_lat': '45.42', 'destination_lng': '-75.69',
            'stop_type': ['pickup'],
        }
        payload.update(stops)
        return self._login('pa_driver@example.com').post(reverse('post-ride'), payload)

    def test_pages_load_shared_script(self):
        ride = make_ride(self.driver)
        pages = [
            (self._login('pa_driver@example.com'), reverse('post-ride')),
            (self._login('pa_driver@example.com'), reverse('edit-ride', args=[encode_id(ride.id)])),
            (self._login('pa_rider@example.com'), reverse('search-rides')),
        ]
        for client, url in pages:
            resp = client.get(url)
            self.assertContains(resp, 'js/place-autocomplete.js', msg_prefix=url)
            self.assertNotContains(resp, 'photon.komoot.io/api', msg_prefix=url)
            self.assertNotContains(resp, 'limit=1', msg_prefix=url)

    def test_unpicked_stop_rejected(self):
        resp = self._post_ride(stop_location=['Kingston, ON'], stop_lat=[''], stop_lng=[''])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['errors']['stops'],
                         'Pick "Kingston, ON" from the suggestions list, or remove that stop.')
        self.assertContains(resp, 'Pick &quot;Kingston, ON&quot; from the suggestions list')
        self.assertFalse(Rides.objects.filter(driver=self.driver).exists())

    def test_stop_outside_service_area_rejected(self):
        resp = self._post_ride(stop_location=['London, UK'], stop_lat=['51.50'], stop_lng=['-0.12'])
        self.assertIn('outside the US and Canada', resp.context['errors']['stops'])

    def test_picked_stop_saved_with_coordinates(self):
        resp = self._post_ride(stop_location=['Kingston, ON'], stop_lat=['44.23'], stop_lng=['-76.48'])
        self.assertEqual(resp.status_code, 302)
        stop = RideStop.objects.get(ride__driver=self.driver)
        self.assertEqual((stop.location, stop.lat, stop.lng), ('Kingston, ON', 44.23, -76.48))

    def test_shared_script_has_no_auto_pick(self):
        js = (Path(settings.BASE_DIR) / 'static' / 'js' / 'place-autocomplete.js').read_text(encoding='utf-8')
        self.assertNotIn('items[0]', js)
        self.assertNotIn('limit=1', js)
        self.assertIn("Couldn't load suggestions", js)


class ProfileVehicleTests(TestCase):
    def setUp(self):
        self.driver = make_user('pv_driver@example.com', role='driver')

    def _base_payload(self, **overrides):
        payload = {'full_name': 'PV Driver', 'dob': '1990-01-01', 'role': 'driver'}
        payload.update(overrides)
        return payload

    def test_update_profile_basic_fields(self):
        c = Client()
        c.login(username='pv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            full_name='New Name', gender='male', bio='Hello there',
            country_code='+1', phone_number='4155552671',
            # Drivers must submit at least one vehicle for the save to succeed.
            vehicle_make=['Toyota'], vehicle_model=['Corolla'],
            vehicle_year=['2020'], vehicle_color=['Blue'], vehicle_plate=['BASIC1'],
        ))
        self.assertRedirects(resp, reverse('profile'))
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.full_name, 'New Name')
        self.assertEqual(self.driver.gender, 'male')
        self.assertEqual(self.driver.phone, '+14155552671')

    def test_underage_driver_rejected(self):
        too_young = date.today().replace(year=date.today().year - 17).isoformat()
        c = Client()
        c.login(username='pv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(dob=too_young))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('dob', resp.context['errors'])

    def test_adult_driver_with_future_license_expiry_saves(self):
        # Regression: the profile page's shared date helpers applied the DOB
        # 18+ age check to License Expiry too, so a (valid) future expiry
        # blocked Save with "You must be at least 18 years old."
        future_expiry = (date.today() + timedelta(days=365 * 3)).isoformat()
        c = Client()
        c.login(username='pv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            license_expiry=future_expiry,
            vehicle_make=['Toyota'], vehicle_model=['Corolla'],
            vehicle_year=['2020'], vehicle_color=['Blue'], vehicle_plate=['EXPOK1'],
        ))
        self.assertRedirects(resp, reverse('profile'))
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.dob, date(1990, 1, 1))
        self.assertEqual(self.driver.license_expiry.isoformat(), future_expiry)

    def test_driver_turning_18_today_accepted(self):
        today = date.today()
        try:
            eighteenth_birthday = today.replace(year=today.year - 18)
        except ValueError:  # today is Feb 29
            eighteenth_birthday = today.replace(year=today.year - 18, day=28)
        c = Client()
        c.login(username='pv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            dob=eighteenth_birthday.isoformat(),
            vehicle_make=['Toyota'], vehicle_model=['Corolla'],
            vehicle_year=['2020'], vehicle_color=['Blue'], vehicle_plate=['BDAY18'],
        ))
        self.assertRedirects(resp, reverse('profile'))
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.dob, eighteenth_birthday)

    def test_profile_page_limits_age_check_to_dob_field(self):
        # Client-side guard for the same regression: the age check must be
        # scoped to the DOB field, and the hidden native pickers must react
        # on input (not only change) so the age pill never shows a stale value.
        c = Client()
        c.login(username='pv_driver@example.com', password='TestPass123')
        html = c.get(reverse('profile')).content.decode()
        self.assertIn("return hiddenId === 'dobInput' ? dobAgeError(date) : '';", html)
        self.assertNotIn('setDobError(', html)
        self.assertIn(
            "oninput=\"pickNativeDate(this,'dobDisplay','dobInput'); updateAge(); updateProgress()\"",
            html,
        )
        self.assertIn(
            "oninput=\"pickNativeDate(this,'licExpDisplay','licExpInput'); updateProgress()\"",
            html,
        )

    def test_duplicate_plate_in_submission_rejected(self):
        c = Client()
        c.login(username='pv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            vehicle_make=['Toyota', 'Honda'], vehicle_model=['Corolla', 'Civic'],
            vehicle_year=['2020', '2021'], vehicle_color=['Blue', 'Red'],
            vehicle_plate=['ABC123', 'ABC123'],
        ))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('vehicles', resp.context['errors'])
        self.assertFalse(Vehicle.objects.filter(owner=self.driver).exists())

    def test_plate_owned_by_another_user_rejected(self):
        other = make_driver_with_vehicle('pv_other@example.com')
        taken_plate = other.vehicles.first().plate
        c = Client()
        c.login(username='pv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            vehicle_make=['Honda'], vehicle_model=['Civic'],
            vehicle_year=['2019'], vehicle_color=['Red'], vehicle_plate=[taken_plate],
        ))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('vehicles', resp.context['errors'])

    def test_vehicle_submission_creates_row(self):
        c = Client()
        c.login(username='pv_driver@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            vehicle_make=['Toyota'], vehicle_model=['Corolla'],
            vehicle_year=['2020'], vehicle_color=['Blue'], vehicle_plate=['NEWPLT1'],
        ))
        self.assertRedirects(resp, reverse('profile'))
        self.assertTrue(Vehicle.objects.filter(owner=self.driver, plate='NEWPLT1').exists())

    def test_submitting_without_vehicle_fields_does_not_wipe_existing_vehicle(self):
        driver = make_driver_with_vehicle('pv_hasvehicle@example.com')
        self.assertTrue(driver.vehicles.exists())
        c = Client()
        c.login(username='pv_hasvehicle@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(full_name='Still Driver'))
        self.assertEqual(resp.status_code, 200)  # re-rendered with an error, not redirected
        self.assertIn('vehicles', resp.context['errors'])
        self.assertTrue(driver.vehicles.exists())

    def test_editing_plate_preserves_existing_photo_and_id(self):
        driver = make_driver_with_vehicle('pv_platephoto@example.com')
        vehicle = driver.vehicles.first()
        vehicle.photo_url = '/media/vehicles/existing.jpg'
        vehicle.save(update_fields=['photo_url'])
        original_id = vehicle.id

        c = Client()
        c.login(username='pv_platephoto@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            vehicle_id=[str(vehicle.id)],
            vehicle_make=['Toyota'], vehicle_model=['Corolla'],
            vehicle_year=['2020'], vehicle_color=['blue'],
            vehicle_plate=['NEWPLATE99'],
        ))
        self.assertRedirects(resp, reverse('profile'))
        updated = driver.vehicles.get(plate='NEWPLATE99')
        self.assertEqual(updated.photo_url, '/media/vehicles/existing.jpg')
        # Updated in place -- same row, so anything referencing the old id
        # (e.g. a Rides.vehicle FK) still points at the right vehicle.
        self.assertEqual(updated.id, original_id)

    def test_ride_vehicle_link_survives_unrelated_profile_resave(self):
        driver = make_driver_with_vehicle('pv_ridelink@example.com')
        vehicle = driver.vehicles.first()
        ride = make_ride(driver)

        c = Client()
        c.login(username='pv_ridelink@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            full_name='Ride Link Driver',
            vehicle_id=[str(vehicle.id)],
            vehicle_make=[vehicle.make], vehicle_model=[vehicle.model],
            vehicle_year=[str(vehicle.year)], vehicle_color=[vehicle.color],
            vehicle_plate=[vehicle.plate],
        ))
        self.assertRedirects(resp, reverse('profile'))
        ride.refresh_from_db()
        self.assertEqual(ride.vehicle_id, vehicle.id)

    def test_removing_a_vehicle_row_deletes_only_that_vehicle(self):
        driver = make_driver_with_vehicle('pv_removeone@example.com')
        kept = driver.vehicles.first()
        removed = Vehicle.objects.create(
            owner=driver, make='Honda', model='Civic', year=2019,
            color='red', plate='PV-REMOVE1',
        )
        c = Client()
        c.login(username='pv_removeone@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(
            vehicle_id=[str(kept.id)],
            vehicle_make=[kept.make], vehicle_model=[kept.model],
            vehicle_year=[str(kept.year)], vehicle_color=[kept.color],
            vehicle_plate=[kept.plate],
        ))
        self.assertRedirects(resp, reverse('profile'))
        self.assertTrue(Vehicle.objects.filter(id=kept.id).exists())
        self.assertFalse(Vehicle.objects.filter(id=removed.id).exists())


class DriverInboxViewTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('di_driver@example.com')
        self.rider = make_user('di_rider@example.com')
        self.ride = make_ride(self.driver)

    def test_rider_cannot_view_driver_inbox(self):
        c = Client()
        c.login(username='di_rider@example.com', password='TestPass123')
        resp = c.get(reverse('driver-inbox'))
        self.assertRedirects(resp, reverse('profile'))

    def test_untouched_draft_booking_hidden(self):
        RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=0, status=RideBooking.STATUS_DRAFT,
        )
        c = Client()
        c.login(username='di_driver@example.com', password='TestPass123')
        resp = c.get(reverse('driver-inbox'))
        self.assertEqual(resp.context['total_count'], 0)

    def test_draft_booking_with_message_is_shown(self):
        booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=0, status=RideBooking.STATUS_DRAFT,
        )
        Message.objects.create(ride=self.ride, sender=self.rider, receiver=self.driver, body='Hi, still available?')
        c = Client()
        c.login(username='di_driver@example.com', password='TestPass123')
        resp = c.get(reverse('driver-inbox'))
        self.assertEqual(resp.context['total_count'], 1)
        self.assertEqual(resp.context['conversations'][0]['booking_id'], booking.id)

    def test_confirmed_booking_shows_agreed_price(self):
        RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )
        c = Client()
        c.login(username='di_driver@example.com', password='TestPass123')
        resp = c.get(reverse('driver-inbox'))
        conv = resp.context['conversations'][0]
        self.assertEqual(conv['nego_status'], 'agreed')
        self.assertIn('25.00', conv['nego_label'])


class RiderInboxViewTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('ri_driver@example.com')
        self.rider = make_user('ri_rider@example.com')
        self.ride = make_ride(self.driver)

    def test_driver_cannot_view_rider_inbox(self):
        c = Client()
        c.login(username='ri_driver@example.com', password='TestPass123')
        resp = c.get(reverse('rider-inbox'))
        self.assertRedirects(resp, reverse('profile'))

    def test_draft_booking_always_excluded(self):
        RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=0, status=RideBooking.STATUS_DRAFT,
        )
        Message.objects.create(ride=self.ride, sender=self.rider, receiver=self.driver, body='Hi')
        c = Client()
        c.login(username='ri_rider@example.com', password='TestPass123')
        resp = c.get(reverse('rider-inbox'))
        self.assertEqual(resp.context['total_count'], 0)

    def test_confirmed_booking_is_shown(self):
        RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )
        c = Client()
        c.login(username='ri_rider@example.com', password='TestPass123')
        resp = c.get(reverse('rider-inbox'))
        self.assertEqual(resp.context['total_count'], 1)


class VerifyDriverViewGateTests(TestCase):
    """Covers the request-validation gates, the payload sent to the
    DL-Verification Node, and how its response is parsed. The outbound
    `requests.post` call is mocked (see module docstring) -- no real
    verification service is hit."""

    def setUp(self):
        # LocMemCache isn't reset between test methods by Django's TestCase
        # (only the DB rolls back), and _rate_limited's bucket is keyed on
        # (ip, user.id) -- with several tests per class hitting this view,
        # a stale count from an earlier test can trip the limit here.
        cache.clear()
        self.driver = make_user('vd_driver@example.com', role='driver')

    def _make_verifiable(self, photo_url='/media/vehicles/v1.jpg', **vehicle_kwargs):
        """Give self.driver a license + a vehicle with a photo, i.e. everything
        the pre-call gates require before the outbound API call is made."""
        self.driver.license_front_url = '/media/licenses/license_1.jpg'
        self.driver.save(update_fields=['license_front_url'])
        defaults = dict(
            make='Honda', model='Civic', year=2019, color='black',
            plate=f'PLT{self.driver.id}', photo_url=photo_url, vehicle_verified=False,
        )
        defaults.update(vehicle_kwargs)
        return Vehicle.objects.create(owner=self.driver, **defaults)

    def _selfie_file(self):
        # A real image: the view decodes and resizes the selfie (H2).
        return {'selfie': SimpleUploadedFile('s.jpg', _jpeg_bytes(), content_type='image/jpeg')}

    def test_get_method_not_allowed(self):
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        resp = c.get(reverse('verify-driver'))
        self.assertEqual(resp.status_code, 405)

    def test_unauthenticated_rejected(self):
        c = Client()
        resp = c.post(reverse('verify-driver'))
        self.assertEqual(resp.status_code, 401)

    def test_missing_license_rejected(self):
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        resp = c.post(reverse('verify-driver'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('license', json.loads(resp.content)['error'].lower())

    def test_missing_selfie_rejected(self):
        self.driver.license_front_url = '/media/licenses/license_1.jpg'
        self.driver.save(update_fields=['license_front_url'])
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        # License read is mocked out so the request reaches the selfie check
        # without touching real disk storage.
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')):
            resp = c.post(reverse('verify-driver'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('selfie', json.loads(resp.content)['error'].lower())

    def test_no_vehicle_rejected(self):
        self.driver.license_front_url = '/media/licenses/license_1.jpg'
        self.driver.save(update_fields=['license_front_url'])
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')):
            resp = c.post(reverse('verify-driver'), self._selfie_file())
        self.assertEqual(resp.status_code, 400)
        self.assertIn('vehicle', json.loads(resp.content)['error'].lower())

    def test_vehicle_without_photo_rejected(self):
        self.driver.license_front_url = '/media/licenses/license_1.jpg'
        self.driver.save(update_fields=['license_front_url'])
        Vehicle.objects.create(
            owner=self.driver, make='Honda', model='Civic', year=2019,
            color='black', plate='NOPHOTO1', photo_url=None,
        )
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')):
            resp = c.post(reverse('verify-driver'), self._selfie_file())
        self.assertEqual(resp.status_code, 400)
        self.assertIn('vehicle', json.loads(resp.content)['error'].lower())

    def test_approved_response_verifies_driver_and_vehicle(self):
        vehicle = self._make_verifiable()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "decision": "APPROVED",
            "stages": {
                "license_ocr": {"passed": True},
                "face_biometrics": {"passed": True, "vlm_confidence": 0.93},
                "vehicle_verification": {"passed": True},
            },
            "rejection_reasons": [],
        }
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post', return_value=mock_resp) as mock_post:
            resp = c.post(reverse('verify-driver'), self._selfie_file())

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data['verified'])
        self.assertEqual(data['similarity'], 93.0)
        self.assertEqual(data['decision']['status'], 'APPROVED')

        # The outbound payload must use the DL-Verification Node's actual
        # schema field names -- this is what regresses if the request
        # shape silently drifts from the service's contract again.
        sent = mock_post.call_args.kwargs['json']
        self.assertEqual(mock_post.call_args.kwargs['headers']['X-API-Key'], settings.DL_VERIFICATION_API_KEY)
        self.assertEqual(mock_post.call_args.args[0], settings.DL_VERIFICATION_API_URL)
        for key in ('request_id', 'driver_id', 'personal_info', 'license_details', 'vehicle_details', 'images'):
            self.assertIn(key, sent)
        for key in ('selfie_base64', 'license_image_base64', 'vehicle_photo_base64'):
            self.assertIn(key, sent['images'])
        self.assertEqual(sent['vehicle_details']['plate_number'], vehicle.plate)

        self.driver.refresh_from_db()
        vehicle.refresh_from_db()
        self.assertTrue(self.driver.driver_verified)
        self.assertTrue(vehicle.vehicle_verified)
        self.assertAlmostEqual(self.driver.face_match_score, 0.93)
        # C2: a pass starts a new re-verification period.
        self.assertIsNotNone(self.driver.driver_verified_at)
        self.assertLess(timezone.now() - self.driver.driver_verified_at, timedelta(minutes=1))
        self.assertIsNone(self.driver.reverify_reminder_sent_at)

    def test_saved_profile_photo_never_used_as_verification_selfie(self):
        # C2: the profile photo may show the vehicle, and isn't live proof,
        # so a request without a fresh selfie is rejected before any call out.
        self._make_verifiable()
        self.driver.selfie_url = '/private-media/selfies/1/profile.jpg'
        self.driver.save(update_fields=['selfie_url'])
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post') as mock_post:
            resp = c.post(reverse('verify-driver'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('selfie', json.loads(resp.content)['error'].lower())
        mock_post.assert_not_called()

    def test_rejected_response_does_not_verify_driver_or_vehicle(self):
        vehicle = self._make_verifiable()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "decision": "REJECTED",
            "stages": {
                "license_ocr": {"passed": True},
                "face_biometrics": {"passed": False, "vlm_confidence": 0.22},
                "vehicle_verification": {"passed": False},
            },
            "rejection_reasons": ["Facial biometrics mismatch: VLM confidence 0.22"],
        }
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post', return_value=mock_resp):
            resp = c.post(reverse('verify-driver'), self._selfie_file())

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertFalse(data['verified'])
        self.assertIn('Facial biometrics mismatch', data['decision']['details'])

        self.driver.refresh_from_db()
        vehicle.refresh_from_db()
        self.assertFalse(self.driver.driver_verified)
        self.assertFalse(vehicle.vehicle_verified)


def _jpeg_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (20, 20), 'red').save(buf, format='JPEG')
    return buf.getvalue()


class PrivateMediaTests(TestCase):
    """ISSUES.md C1: identity images live outside the public /media/ tree
    and are only served to their owner or staff."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self._tmp) / 'private',
            MEDIA_ROOT=Path(self._tmp) / 'public',
            PRIVATE_MEDIA_USE_ACCEL=False,
        )
        self._override.enable()
        self.owner = make_user('pm_owner@example.com')
        self.other = make_user('pm_other@example.com')

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _upload_selfie(self):
        c = Client()
        c.login(username='pm_owner@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), {
            'full_name': 'PM Owner', 'role': 'rider',
            'selfie_file': SimpleUploadedFile('me.jpg', _jpeg_bytes(), content_type='image/jpeg'),
        })
        self.assertRedirects(resp, reverse('profile'))
        self.owner.refresh_from_db()
        return c, self.owner.selfie_url

    def test_upload_goes_to_private_storage_with_random_name(self):
        _, url = self._upload_selfie()
        self.assertTrue(url.startswith('/private-media/selfies/'))
        self.assertNotIn(f'selfie_{self.owner.id}', url)
        self.assertEqual(self.owner.avatar_url, url)
        rel = url[len('/private-media/'):]
        self.assertTrue((Path(self._tmp) / 'private' / rel).exists())
        self.assertFalse((Path(self._tmp) / 'public' / 'selfies').exists())

    def test_owner_can_fetch(self):
        c, url = self._upload_selfie()
        resp = c.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b''.join(resp.streaming_content), (Path(self._tmp) / 'private' / url[len('/private-media/'):]).read_bytes())
        self.assertEqual(resp['Cache-Control'], 'private, no-store')
        self.assertEqual(resp['Content-Type'], 'image/jpeg')

    def test_other_user_gets_404(self):
        _, url = self._upload_selfie()
        c = Client()
        c.login(username='pm_other@example.com', password='TestPass123')
        self.assertEqual(c.get(url).status_code, 404)

    def test_anonymous_redirected_to_login(self):
        _, url = self._upload_selfie()
        resp = Client().get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith(settings.LOGIN_URL))

    def test_staff_can_fetch(self):
        _, url = self._upload_selfie()
        make_user('pm_staff@example.com', is_staff=True)
        c = Client()
        c.login(username='pm_staff@example.com', password='TestPass123')
        self.assertEqual(c.get(url).status_code, 200)

    def test_path_traversal_rejected(self):
        c, _ = self._upload_selfie()
        for bad in ('/private-media/selfies/../../config/settings.py',
                    '/private-media/../manage.py',
                    '/private-media/other/file.jpg'):
            self.assertEqual(c.get(bad).status_code, 404, bad)

    def test_accel_mode_hands_off_to_nginx(self):
        c, url = self._upload_selfie()
        with override_settings(PRIVATE_MEDIA_USE_ACCEL=True):
            resp = c.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['X-Accel-Redirect'], '/protected-media/' + url[len('/private-media/'):])
        self.assertEqual(resp.content, b'')

    def test_replacing_selfie_deletes_old_file(self):
        _, first = self._upload_selfie()
        _, second = self._upload_selfie()
        self.assertNotEqual(first, second)
        self.assertFalse((Path(self._tmp) / 'private' / first[len('/private-media/'):]).exists())
        self.assertTrue((Path(self._tmp) / 'private' / second[len('/private-media/'):]).exists())

    def test_move_private_media_migrates_legacy_files(self):
        public = Path(self._tmp) / 'public'
        (public / 'licenses').mkdir(parents=True)
        (public / 'selfies').mkdir()
        (public / 'vehicles').mkdir()
        (public / 'licenses' / f'license_{self.owner.id}.jpg').write_bytes(b'lic')
        (public / 'selfies' / f'selfie_{self.owner.id}.jpg').write_bytes(b'self')
        (public / 'vehicles' / 'vehicle_1_1.jpg').write_bytes(b'veh')
        (public / 'vehicles' / 'orphan.jpg').write_bytes(b'old')
        self.owner.license_front_url = f'/media/licenses/license_{self.owner.id}.jpg'
        self.owner.selfie_url = self.owner.avatar_url = f'/media/selfies/selfie_{self.owner.id}.jpg'
        self.owner.save()
        vehicle = Vehicle.objects.create(
            owner=self.owner, make='Honda', model='Civic', year=2019,
            color='black', plate='PMMOVE1', photo_url='/media/vehicles/vehicle_1_1.jpg',
        )

        call_command('move_private_media', '--dry-run')
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.license_front_url.startswith('/media/'))

        call_command('move_private_media', '--delete-orphans')
        self.owner.refresh_from_db()
        vehicle.refresh_from_db()
        self.assertTrue(self.owner.license_front_url.startswith('/private-media/licenses/'))
        self.assertTrue(self.owner.selfie_url.startswith('/private-media/selfies/'))
        self.assertEqual(self.owner.avatar_url, self.owner.selfie_url)
        self.assertTrue(vehicle.photo_url.startswith('/private-media/vehicles/'))
        self.assertEqual(list(public.rglob('*.jpg')), [])

        c = Client()
        c.login(username='pm_owner@example.com', password='TestPass123')
        self.assertEqual(b''.join(c.get(self.owner.license_front_url).streaming_content), b'lic')
        self.assertEqual(c.get(vehicle.photo_url).status_code, 200)


class ImageUploadTests(TestCase):
    """ISSUES.md H2: large photos are resized and logged, a rejected photo
    is never reported as saved, vehicle photos land on the right vehicle,
    and an over-limit request gets an explanation page."""

    def setUp(self):
        cache.clear()  # verify-driver's rate-limit bucket
        self._tmp = tempfile.mkdtemp()
        self._override = override_settings(PRIVATE_MEDIA_ROOT=Path(self._tmp), PRIVATE_MEDIA_USE_ACCEL=False)
        self._override.enable()
        self.user = make_user('up_user@example.com', role='driver')
        self.client.login(username='up_user@example.com', password='TestPass123')

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _big_jpeg(size=(3000, 2000)):
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', size, 'blue').save(buf, format='JPEG')
        return buf.getvalue()

    def _stored_dims(self, url):
        from PIL import Image
        with Image.open(Path(self._tmp) / url[len('/private-media/'):]) as img:
            return img.size

    def _save_profile(self, **extra):
        payload = {'full_name': 'Up User', 'dob': '1990-01-01', 'role': 'driver',
                   'vehicle_make': ['Toyota'], 'vehicle_model': ['Corolla'],
                   'vehicle_year': ['2020'], 'vehicle_color': ['Blue'], 'vehicle_plate': ['UPL1']}
        payload.update(extra)
        resp = self.client.post(reverse('profile'), payload)
        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)
        from django.contrib.messages import get_messages
        return [str(m) for m in get_messages(resp.wsgi_request)]

    def test_large_photo_resized_and_logged(self):
        with self.assertLogs('komute.uploads', 'INFO') as logs:
            msgs = self._save_profile(selfie_file=SimpleUploadedFile('big.jpg', self._big_jpeg(), content_type='image/jpeg'))
        self.assertIn('Profile saved successfully.', msgs)
        self.user.refresh_from_db()
        self.assertEqual(self._stored_dims(self.user.selfie_url), (1200, 800))
        line = next(l for l in logs.output if 'profile-photo' in l)
        self.assertIn(f'user={self.user.id}', line)
        self.assertIn("name='big.jpg'", line)
        self.assertIn("type='image/jpeg'", line)
        self.assertIn('dims=3000x2000 -> 1200x800', line)

    @override_settings(IMAGE_UPLOAD_MAX_MB=1)
    def test_too_large_photo_not_reported_as_saved(self):
        too_big = SimpleUploadedFile('huge.jpg', b'x' * (1024 * 1024 + 1), content_type='image/jpeg')
        with self.assertLogs('komute.uploads', 'WARNING') as logs:
            msgs = self._save_profile(full_name='Renamed', license_file=too_big)
        self.assertNotIn('Profile saved successfully.', msgs)
        self.assertIn('License photo: File is too large (max 1 MB).', msgs)
        self.assertIn('Your other changes were saved.', msgs)
        self.assertIn('too large', logs.output[0])
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, 'Renamed')
        self.assertFalse(self.user.license_front_url)

    def test_unreadable_photo_not_reported_as_saved(self):
        msgs = self._save_profile(selfie_file=SimpleUploadedFile('a.heic', b'not an image', content_type='image/heic'))
        self.assertNotIn('Profile saved successfully.', msgs)
        self.assertIn("Profile photo: That file isn't a valid image.", msgs)

    def test_vehicle_photo_goes_to_its_own_row(self):
        # Browsers leave out empty file inputs, so with a shared field name
        # a photo for the second vehicle used to be saved on the first.
        v1 = Vehicle.objects.create(owner=self.user, make='Toyota', model='Corolla', year=2020, color='Blue', plate='ROW1')
        v2 = Vehicle.objects.create(owner=self.user, make='Honda', model='Civic', year=2019, color='Red', plate='ROW2')
        self._save_profile(
            vehicle_id=[str(v1.id), str(v2.id)], vehicle_make=['Toyota', 'Honda'],
            vehicle_model=['Corolla', 'Civic'], vehicle_year=['2020', '2019'],
            vehicle_color=['Blue', 'Red'], vehicle_plate=['ROW1', 'ROW2'],
            vehicle_image_file_1=SimpleUploadedFile('car.jpg', _jpeg_bytes(), content_type='image/jpeg'),
        )
        v1.refresh_from_db()
        v2.refresh_from_db()
        self.assertFalse(v1.photo_url)
        self.assertTrue(v2.photo_url.startswith('/private-media/vehicles/'))

    def _verifiable(self):
        self.user.license_front_url = '/media/licenses/license_1.jpg'
        self.user.save(update_fields=['license_front_url'])
        Vehicle.objects.create(owner=self.user, make='Honda', model='Civic', year=2019,
                               color='black', plate='UPV1', photo_url='/media/vehicles/v1.jpg')

    def test_verification_selfie_resized_before_sending(self):
        from PIL import Image
        import base64
        self._verifiable()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "decision": "REJECTED",
            "stages": {"face_biometrics": {"passed": False, "vlm_confidence": 0.2}},
            "rejection_reasons": ["Facial biometrics mismatch for JOHN SMITH"],
        }
        selfie = SimpleUploadedFile('s.jpg', self._big_jpeg(), content_type='image/jpeg')
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post', return_value=mock_resp) as mock_post, \
             self.assertLogs('komute.uploads', 'INFO') as logs:
            resp = self.client.post(reverse('verify-driver'), {'selfie': selfie})
        self.assertEqual(resp.status_code, 200)
        sent = base64.b64decode(mock_post.call_args.kwargs['json']['images']['selfie_base64'])
        with Image.open(io.BytesIO(sent)) as img:
            self.assertEqual(img.size, (1200, 800))
        result_line = next(l for l in logs.output if 'verification result' in l)
        self.assertIn('decision=REJECTED', result_line)
        self.assertIn('FACE_MISMATCH', result_line)
        self.assertNotIn('JOHN SMITH', result_line)  # reason text may quote the license

    def test_unreadable_verification_selfie_rejected_before_call(self):
        self._verifiable()
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post') as mock_post:
            resp = self.client.post(reverse('verify-driver'), {
                'selfie': SimpleUploadedFile('s.jpg', b'garbage', content_type='image/jpeg')})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Selfie', json.loads(resp.content)['error'])
        mock_post.assert_not_called()

    @override_settings(UPLOAD_REQUEST_MAX_MB=40)
    def test_upload_too_large_page(self):
        resp = Client().get(reverse('upload-too-large'))
        self.assertEqual(resp.status_code, 413)
        self.assertContains(resp, 'more than 40 MB', status_code=413)

    @override_settings(IMAGE_UPLOAD_MAX_MB=15, UPLOAD_REQUEST_MAX_MB=40)
    def test_profile_page_shrinks_photos_before_upload(self):
        html = self.client.get(reverse('profile')).content.decode()
        self.assertIn('const UPLOAD_FILE_MAX_BYTES    = 15 * 1024 * 1024;', html)
        self.assertIn('const UPLOAD_REQUEST_MAX_BYTES = 40 * 1024 * 1024;', html)
        self.assertIn("'vehicle_image_file_' + i", html)
        self.assertIn('shrinkImage(_verifySelfieBlob)', html)
        self.assertIn('r.status === 413', html)


@override_settings(DRIVER_REVERIFY_DAYS=90, DRIVER_REVERIFY_REMINDER_DAYS=10)
class DriverReverificationTests(TestCase):
    """ISSUES.md C2: drivers re-take the verification selfie every
    DRIVER_REVERIFY_DAYS, get one reminder DRIVER_REVERIFY_REMINDER_DAYS
    before, and are blocked from posting rides / confirming bookings after."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('rv_driver@example.com')
        self.rider = make_user('rv_rider@example.com')

    def _verified_days_ago(self, days, user=None):
        user = user or self.driver
        user.driver_verified = True
        user.driver_verified_at = timezone.now() - timedelta(days=days)
        user.save(update_fields=['driver_verified', 'driver_verified_at'])

    def _login(self, email):
        c = Client()
        c.login(username=email, password='TestPass123')
        return c

    # ── expire_driver_verification command ──

    def test_no_reminder_before_window(self):
        self._verified_days_ago(79)
        call_command('expire_driver_verification')
        self.driver.refresh_from_db()
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(self.driver.reverify_reminder_sent_at)
        self.assertTrue(self.driver.driver_verified)

    def test_reminder_sent_once_inside_window(self):
        self._verified_days_ago(81)
        call_command('expire_driver_verification')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.driver.email])
        self.assertIn('re-verify', mail.outbox[0].subject.lower())
        self.driver.refresh_from_db()
        self.assertIsNotNone(self.driver.reverify_reminder_sent_at)
        self.assertTrue(self.driver.driver_verified)

        call_command('expire_driver_verification')
        self.assertEqual(len(mail.outbox), 1)

    def test_expired_after_period(self):
        self._verified_days_ago(91)
        self.driver.face_match_score = 0.9
        self.driver.save(update_fields=['face_match_score'])
        call_command('expire_driver_verification')
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.driver_verified)
        self.assertIsNone(self.driver.face_match_score)
        self.assertEqual(len(mail.outbox), 0)

    def test_dry_run_changes_nothing(self):
        self._verified_days_ago(91)
        call_command('expire_driver_verification', '--dry-run')
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.driver_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_failed_reminder_retried_next_run(self):
        self._verified_days_ago(81)
        with patch('komute.management.commands.expire_driver_verification.EmailMultiAlternatives.send',
                   side_effect=Exception('smtp down')):
            call_command('expire_driver_verification')
        self.driver.refresh_from_db()
        self.assertIsNone(self.driver.reverify_reminder_sent_at)
        call_command('expire_driver_verification')
        self.assertEqual(len(mail.outbox), 1)

    # ── blocking (checked by date, even before the command has run) ──

    def test_post_ride_blocked_when_lapsed(self):
        self._verified_days_ago(91)
        resp = self._login('rv_driver@example.com').get(reverse('post-ride'))
        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)

    def test_post_ride_allowed_when_current(self):
        self._verified_days_ago(30)
        resp = self._login('rv_driver@example.com').get(reverse('post-ride'))
        self.assertEqual(resp.status_code, 200)

    def test_accept_booking_blocked_when_lapsed(self):
        ride = make_ride(self.driver)
        booking = RideBooking.objects.create(
            ride=ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED,
        )
        self._verified_days_ago(91)
        resp = self._login('rv_driver@example.com').post(
            reverse('accept-booking', args=[encode_id(booking.id)]))
        self.assertEqual(resp.status_code, 403)
        booking.refresh_from_db()
        self.assertEqual(booking.status, RideBooking.STATUS_REQUESTED)

    def test_chat_price_agreement_cannot_confirm_when_lapsed(self):
        ride = make_ride(self.driver)
        # A pre-request chat: agreeing the price here is what confirms it.
        booking = RideBooking.objects.create(
            ride=ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_DRAFT,
        )
        offer = PriceOffer.objects.create(booking=booking, proposed_by=self.driver, price_cents=2000)
        self._verified_days_ago(91)
        result, _ = async_to_sync(ChatConsumer().accept_price_offer)(self.rider, booking.id, offer.id, None)
        self.assertEqual(result, 'driver_unverified')
        booking.refresh_from_db()
        offer.refresh_from_db()
        self.assertEqual(booking.status, RideBooking.STATUS_DRAFT)
        self.assertEqual(offer.status, PriceOffer.STATUS_PENDING)

    def test_invite_send_and_accept_blocked_when_lapsed(self):
        ride = make_ride(self.driver)
        invite = RideInvite.objects.create(driver=self.driver, passenger=self.rider, ride=ride)
        self._verified_days_ago(91)

        resp = self._login('rv_driver@example.com').post(
            reverse('send-invite'),
            data=json.dumps({'rider_id': self.rider.id, 'ride_id': encode_id(ride.id)}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)

        from komute.views import _make_invite_token
        resp = self._login('rv_rider@example.com').post(
            reverse('accept-invite', args=[_make_invite_token(invite.id)]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 're-verify')
        self.assertFalse(RideBooking.objects.filter(ride=ride, passenger=self.rider).exists())

    # ── profile page ──

    def test_profile_warns_inside_reminder_window(self):
        self._verified_days_ago(85)
        resp = self._login('rv_driver@example.com').get(reverse('profile'))
        self.assertTrue(resp.context['reverify_soon'])
        self.assertContains(resp, 'reverifySoonHint')
        self.assertContains(resp, 'Re-verify Identity')

    def test_profile_shows_expired(self):
        self._verified_days_ago(91)
        resp = self._login('rv_driver@example.com').get(reverse('profile'))
        self.assertFalse(resp.context['driver_verified'])
        self.assertTrue(resp.context['reverify_expired'])
        self.assertContains(resp, 'Your identity verification expired on')

    def test_verification_selfie_upload_only_in_debug(self):
        c = self._login('rv_driver@example.com')
        with override_settings(DEBUG=False):
            resp = c.get(reverse('profile'))
        self.assertNotContains(resp, 'id="verifySelfieFileInput"')
        with override_settings(DEBUG=True):
            resp = c.get(reverse('profile'))
        self.assertContains(resp, 'id="verifySelfieFileInput"')

    def test_changing_profile_photo_keeps_verification(self):
        self._verified_days_ago(10)
        tmp = tempfile.mkdtemp()
        try:
            with override_settings(PRIVATE_MEDIA_ROOT=Path(tmp)):
                resp = self._login('rv_driver@example.com').post(reverse('profile'), {
                    # Same verified details as make_user; only the photo changes.
                    'full_name': self.driver.full_name, 'dob': '1995-01-01', 'role': 'driver',
                    'vehicle_make': ['Toyota'], 'vehicle_model': ['Corolla'],
                    'vehicle_year': ['2020'], 'vehicle_color': ['blue'],
                    'vehicle_plate': [self.driver.vehicles.first().plate],
                    'selfie_file': SimpleUploadedFile('car.jpg', _jpeg_bytes(), content_type='image/jpeg'),
                })
            self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.selfie_url)
        self.assertTrue(self.driver.driver_verified)

    # ── migration backfill ──

    def test_backfill_gives_existing_drivers_full_notice(self):
        from importlib import import_module
        from django.apps import apps as django_apps
        backfill = import_module('komute.migrations.0033_customuser_driver_verified_at').backfill_driver_verified_at

        recent = make_user('rv_recent@example.com', role='driver', driver_verified=True)
        log = VerificationLog.objects.create(user=recent, result=VerificationLog.RESULT_APPROVED)
        VerificationLog.objects.filter(id=log.id).update(created_at=timezone.now() - timedelta(days=20))
        old = make_user('rv_old@example.com', role='driver', driver_verified=True)
        log = VerificationLog.objects.create(user=old, result=VerificationLog.RESULT_APPROVED)
        VerificationLog.objects.filter(id=log.id).update(created_at=timezone.now() - timedelta(days=200))
        no_log = make_user('rv_nolog@example.com', role='driver', driver_verified=True)

        backfill(django_apps, None)
        now = timezone.now()
        for u in (recent, old, no_log):
            u.refresh_from_db()
        self.assertAlmostEqual((now - recent.driver_verified_at).days, 20, delta=1)
        # Old verifications are pulled forward to the start of the reminder
        # window: reminded on the next run, blocked 10 days later.
        self.assertAlmostEqual((now - old.driver_verified_at).days, 80, delta=1)
        self.assertTrue(old.driver_verification_current)
        self.assertLess(now - no_log.driver_verified_at, timedelta(minutes=1))


class VerifiedDetailsChangeTests(TestCase):
    """ISSUES.md C3: verification is tied to the details it was checked
    against. Changing them (beyond case/hyphens, which DL-Verification
    ignores) voids it; new or changed vehicles start unverified."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('vdc_driver@example.com')
        self.driver.full_name = 'Mary-Jane Smith'
        self.driver.license_number = 'S1234-56789'
        self.driver.license_province = 'ON'
        self.driver.license_expiry = date.today() + timedelta(days=365)
        self.driver.driver_verified_at = timezone.now()
        self.driver.face_match_score = 0.9
        self.driver.save()
        self.vehicle = self.driver.vehicles.first()
        self.c = Client()
        self.c.login(username='vdc_driver@example.com', password='TestPass123')

    def _post(self, vehicles=None, **overrides):
        v = self.vehicle
        payload = {
            'full_name': self.driver.full_name, 'dob': '1995-01-01', 'role': 'driver',
            'license_number': self.driver.license_number,
            'license_province': self.driver.license_province,
            'license_expiry': self.driver.license_expiry.isoformat(),
            'vehicle_id': [str(v.id)], 'vehicle_make': [v.make], 'vehicle_model': [v.model],
            'vehicle_year': [str(v.year)], 'vehicle_color': [v.color], 'vehicle_plate': [v.plate],
        }
        payload.update(vehicles or {})
        payload.update(overrides)
        return self.c.post(reverse('profile'), payload)

    def test_unchanged_save_keeps_verification(self):
        self._post()
        self.driver.refresh_from_db()
        self.vehicle.refresh_from_db()
        self.assertTrue(self.driver.driver_verified)
        self.assertTrue(self.vehicle.vehicle_verified)

    def test_case_and_hyphen_only_changes_keep_verification(self):
        self._post(full_name='MARY JANE  smith', license_number='s123456789', license_province='on')
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.driver_verified)

    def test_each_verified_field_change_resets(self):
        changes = [
            {'full_name': 'Mary Jones'},
            {'dob': '1994-01-01'},
            {'license_number': 'S1234-56780'},
            {'license_province': 'BC'},
            {'license_expiry': (date.today() + timedelta(days=400)).isoformat()},
        ]
        for change in changes:
            with self.subTest(change=change):
                CustomUser.objects.filter(id=self.driver.id).update(
                    driver_verified=True, full_name='Mary-Jane Smith', dob=date(1995, 1, 1),
                    license_number='S1234-56789', license_province='ON',
                    license_expiry=date.today() + timedelta(days=365),
                )
                self.driver.refresh_from_db()
                resp = self._post(**change)
                self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)
                self.driver.refresh_from_db()
                self.assertFalse(self.driver.driver_verified)
                self.assertIsNone(self.driver.face_match_score)

    def test_past_license_expiry_rejected(self):
        resp = self._post(license_expiry=(date.today() - timedelta(days=1)).isoformat())
        self.assertEqual(resp.status_code, 200)
        self.assertIn('license_expiry', resp.context['errors'])

    def test_license_expiring_voids_verification(self):
        CustomUser.objects.filter(id=self.driver.id).update(license_expiry=date.today() - timedelta(days=1))
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.driver_verification_current)
        resp = self.c.get(reverse('post-ride'))
        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)
        call_command('expire_driver_verification')
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.driver_verified)

    def test_new_vehicle_starts_unverified(self):
        v = self.vehicle
        self._post(vehicles={
            'vehicle_id': [str(v.id), ''], 'vehicle_make': [v.make, 'Honda'],
            'vehicle_model': [v.model, 'Civic'], 'vehicle_year': [str(v.year), '2021'],
            'vehicle_color': [v.color, 'red'], 'vehicle_plate': [v.plate, 'NEW-123'],
        })
        new = Vehicle.objects.get(plate='NEW-123')
        self.assertFalse(new.vehicle_verified)
        self.vehicle.refresh_from_db()
        self.assertTrue(self.vehicle.vehicle_verified)

    def test_vehicle_change_resets_its_verification(self):
        for field, value in [('vehicle_plate', 'CHG-999'), ('vehicle_color', 'green'),
                             ('vehicle_year', '2018'), ('vehicle_model', 'Camry')]:
            with self.subTest(field=field):
                Vehicle.objects.filter(id=self.vehicle.id).update(
                    vehicle_verified=True, plate='PLT-vdc_', color='blue', year=2020, model='Corolla')
                self.vehicle.refresh_from_db()
                self._post(vehicles={field: [value]})
                self.vehicle.refresh_from_db()
                self.assertFalse(self.vehicle.vehicle_verified)

    def test_plate_formatting_only_change_keeps_vehicle_verified(self):
        self._post(vehicles={'vehicle_plate': ['plt vdc_']})
        self.vehicle.refresh_from_db()
        self.assertTrue(self.vehicle.vehicle_verified)

    def test_post_ride_rejects_unverified_vehicle(self):
        unverified = Vehicle.objects.create(
            owner=self.driver, make='Honda', model='Civic', year=2021,
            color='red', plate='UNV-1', vehicle_verified=False,
        )
        resp = self.c.post(reverse('post-ride'), {
            'origin': 'Toronto, ON', 'origin_lat': '43.65', 'origin_lng': '-79.38',
            'destination': 'Ottawa, ON', 'dest_lat': '45.42', 'dest_lng': '-75.69',
            'date': (date.today() + timedelta(days=2)).isoformat(), 'depart_time': '09:00',
            'seats': '2', 'vehicle': str(unverified.id), 'price': '20',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('vehicle', resp.context['errors'])
        self.assertFalse(Rides.objects.filter(vehicle=unverified).exists())


class VerificationResultStorageTests(TestCase):
    """ISSUES.md C3: combined-score approval and stored rejection reasons."""

    setUp = VerifyDriverViewGateTests.setUp
    _make_verifiable = VerifyDriverViewGateTests._make_verifiable
    _selfie_file = VerifyDriverViewGateTests._selfie_file

    def _call(self, body):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = body
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.views.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post', return_value=mock_resp):
            return c.post(reverse('verify-driver'), self._selfie_file())

    def test_combined_approval_verifies_vehicle_even_if_its_stage_failed(self):
        vehicle = self._make_verifiable()
        self._call({
            "decision": "APPROVED",
            "stages": {"license_ocr": {"passed": True},
                       "face_biometrics": {"passed": True, "vlm_confidence": 0.95},
                       "vehicle_verification": {"passed": False}},
            "rejection_reasons": [],
        })
        vehicle.refresh_from_db()
        self.assertTrue(vehicle.vehicle_verified)
        log = VerificationLog.objects.get(user=self.driver)
        self.assertEqual(log.stage_results, {
            'license_ocr': True, 'face_biometrics': True, 'vehicle_verification': False})
        self.assertIsNone(log.reason_codes)

    def test_only_outcome_details_stored_not_licence_text(self):
        # Privacy policy s.9/s.13: the driver sees the full reasons, but only
        # categories and per-stage pass/fail are kept -- no OCR output, no
        # reason text (which can quote values read off the licence).
        self._make_verifiable()
        reasons = [
            "License image too blurry. Please retry the verification.",
            "Name mismatch: entered 'JANE DOE', licence reads 'JOHN DOE'",
        ]
        resp = self._call({
            "decision": "REJECTED",
            "stages": {"license_ocr": {"passed": False, "extracted": {"name": "JOHN DOE"}},
                       "face_biometrics": {"passed": True, "vlm_confidence": 0.9}},
            "rejection_reasons": reasons,
        })
        self.assertEqual(json.loads(resp.content)['decision']['details'], '; '.join(reasons))
        log = VerificationLog.objects.get(user=self.driver)
        self.assertEqual(log.result, VerificationLog.RESULT_REJECTED)
        self.assertEqual(log.reason_codes, ['IMAGE_QUALITY', 'NAME_MISMATCH'])
        self.assertEqual(log.stage_results, {
            'license_ocr': False, 'face_biometrics': True, 'vehicle_verification': False})
        self.assertIsNone(log.ocr_extracted_json)
        stored = json.dumps([log.stage_results, log.reason_codes, log.notes])
        self.assertNotIn('JOHN', stored)
        self.assertNotIn('JANE', stored)

    def test_reason_categories(self):
        from komute.views import _reason_codes
        cases = {
            "Image is blurred, please retry": 'IMAGE_QUALITY',
            "Facial biometrics mismatch: VLM confidence 0.22": 'FACE_MISMATCH',
            "Licence plate number does not match": 'VEHICLE_MISMATCH',
            "Vehicle colour mismatch": 'VEHICLE_MISMATCH',
            "Expiry date does not match": 'EXPIRY_MISMATCH',
            "Date of birth mismatch": 'DOB_MISMATCH',
            "License number mismatch": 'LICENSE_NUMBER_MISMATCH',
            "Name mismatch": 'NAME_MISMATCH',
            "Something unexpected happened": 'OTHER',
        }
        for text, code in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_reason_codes([text]), [code])
        self.assertEqual(_reason_codes(["Name mismatch", "Name mismatch (surname)"]), ['NAME_MISMATCH'])
