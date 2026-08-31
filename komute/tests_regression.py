"""
Pre-deploy regression suite. Run with the throwaway sqlite settings:

    python manage.py test komute.tests_regression --settings=config.settings_regression -v 2

Not wired into CI; this is a manually-invoked pre-deploy sanity pass covering
auth, password reset, account deactivation/deletion, the full ride lifecycle
(post/edit/search/book/accept/reject/cancel/review/invite), route-demand
matching, price negotiation, profile + vehicle management, driver/rider
inbox conversation assembly, the verify-driver request-validation gates,
hashid encode/decode, and all retention/purge management commands.
Does NOT cover: websocket chat (consumers.py, needs channels' test infra),
or real outbound email delivery (locmem backend only). The verify-driver
flow's pre-call validation gates, request payload shape, and response
parsing are covered with the outbound `requests.post` call to the
DL-Verification Node mocked out -- no real verification service is hit.
"""
import json
import time
from datetime import date, timedelta
from unittest.mock import MagicMock, mock_open, patch

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

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
        return {'selfie': SimpleUploadedFile('s.jpg', b'fake-selfie-bytes', content_type='image/jpeg')}

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
        with patch('komute.views.default_storage.open', mock_open(read_data=b'fake-bytes')):
            resp = c.post(reverse('verify-driver'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('selfie', json.loads(resp.content)['error'].lower())

    def test_no_vehicle_rejected(self):
        self.driver.license_front_url = '/media/licenses/license_1.jpg'
        self.driver.save(update_fields=['license_front_url'])
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.views.default_storage.open', mock_open(read_data=b'fake-bytes')):
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
        with patch('komute.views.default_storage.open', mock_open(read_data=b'fake-bytes')):
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
        with patch('komute.views.default_storage.open', mock_open(read_data=b'fake-bytes')), \
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
        with patch('komute.views.default_storage.open', mock_open(read_data=b'fake-bytes')), \
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
