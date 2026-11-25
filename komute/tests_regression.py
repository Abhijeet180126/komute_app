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
from django.db.models import F
from django.utils import timezone

from komute.chat_tokens import make_chat_token
from komute.consumers import ChatConsumer, NotifyConsumer
from komute.notifications import booking_fingerprint
from komute.hashid_utils import decode_id, encode_id
from komute.models import (
    CustomUser, Vehicle, Rides, RideStop, RideBooking, Message,
    RideSearch, VerificationLog, Review, RideInvite, PriceOffer,
    UserBlock, UserReport, BiometricConsent, LegalAcceptance, EmailLog,
)


def make_user(email, role='rider', accepted_terms=True, **kwargs):
    defaults = dict(full_name=f'Test {email}', dob=date(1995, 1, 1), phone=None, email_verified=True)
    defaults.update(kwargs)
    user = CustomUser.objects.create_user(
        username=email, email=email, password='TestPass123', role=role, **defaults
    )
    if accepted_terms:
        # As signup does; otherwise LegalAcceptanceMiddleware sends every
        # request to the accept-terms page.
        LegalAcceptance.objects.create(
            user=user, terms_version=settings.TERMS_VERSION,
            privacy_version=settings.PRIVACY_VERSION, source=LegalAcceptance.SOURCE_SIGNUP)
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
            'confirm_password': 'GoodPassword1!', 'dob': '1995-01-01', 'agreed_to_terms': '1',
        })
        self.assertRedirects(resp, reverse('login'))
        self.assertTrue(CustomUser.objects.filter(email='jane@example.com').exists())

        resp = c.post(reverse('login'), {'email': 'jane@example.com', 'password': 'GoodPassword1!'})
        self.assertEqual(resp.status_code, 302)  # first login -> role-select

        resp = c.get(reverse('logout'))
        self.assertRedirects(resp, reverse('login'))

    def test_signup_records_terms_and_privacy_acceptance(self):
        Client(HTTP_USER_AGENT='TestBrowser/1.0').post(reverse('signup'), {
            'full_name': 'Jane Doe', 'email': 'jane@example.com',
            'country_code': '+1', 'phone_number': '4155552671', 'password': 'GoodPassword1!',
            'confirm_password': 'GoodPassword1!', 'dob': '1995-01-01', 'agreed_to_terms': '1',
        }, REMOTE_ADDR='203.0.113.9')
        acceptance = LegalAcceptance.objects.get(user__email='jane@example.com')
        self.assertEqual(acceptance.terms_version, settings.TERMS_VERSION)
        self.assertEqual(acceptance.privacy_version, settings.PRIVACY_VERSION)
        self.assertEqual(acceptance.source, LegalAcceptance.SOURCE_SIGNUP)
        self.assertEqual(acceptance.ip_address, '203.0.113.9')
        self.assertEqual(acceptance.user_agent, 'TestBrowser/1.0')
        self.assertLess(timezone.now() - acceptance.accepted_at, timedelta(minutes=1))

    def test_signup_without_acceptance_creates_nothing(self):
        for value in ('', 'on'):
            resp = Client().post(reverse('signup'), {
                'full_name': 'Jane Doe', 'email': 'jane@example.com',
                'country_code': '+1', 'phone_number': '4155552671', 'password': 'GoodPassword1!',
                'confirm_password': 'GoodPassword1!', 'dob': '1995-01-01', 'agreed_to_terms': value,
            })
            self.assertEqual(resp.status_code, 200)
            self.assertIn('Privacy Policy', resp.context['errors']['agreed_to_terms'])
        self.assertFalse(CustomUser.objects.filter(email='jane@example.com').exists())
        self.assertFalse(LegalAcceptance.objects.exists())

    def _signup(self, **overrides):
        payload = {
            'full_name': 'Jane Doe', 'email': 'jane@example.com',
            'country_code': '+1', 'phone_number': '4155552671', 'password': 'GoodPassword1!',
            'confirm_password': 'GoodPassword1!', 'dob': '1995-01-01', 'agreed_to_terms': '1',
        }
        payload.update(overrides)
        return Client().post(reverse('signup'), payload)

    def test_signup_saves_dob(self):
        self._signup()
        self.assertEqual(CustomUser.objects.get(email='jane@example.com').dob, date(1995, 1, 1))

    def test_signup_requires_adult_dob(self):
        # L-5: 18+ is enforced at signup, not first at booking.
        today = date.today()
        try:
            seventeen = today.replace(year=today.year - 18) + timedelta(days=1)
        except ValueError:  # Feb 29
            seventeen = date(today.year - 18, 3, 1)
        for dob, error in (('', 'required'), ('not-a-date', 'Invalid'),
                           (seventeen.isoformat(), 'at least 18'), ('1900-01-01', '100 or under')):
            resp = self._signup(dob=dob)
            self.assertEqual(resp.status_code, 200, dob)
            self.assertIn(error, resp.context['errors']['dob'], dob)
        self.assertFalse(CustomUser.objects.filter(email='jane@example.com').exists())

    def test_signup_accepts_eighteenth_birthday(self):
        from komute.views import _latest_adult_dob
        resp = self._signup(dob=_latest_adult_dob().isoformat())
        self.assertRedirects(resp, reverse('login'))
        self.assertContains(Client().get(reverse('signup')),
                            f'max="{_latest_adult_dob().isoformat()}"')

    def test_signup_checkbox_names_the_privacy_policy(self):
        resp = Client().get(reverse('signup'))
        self.assertContains(resp, f'href="{reverse("privacy-policy")}" target="_blank" rel="noopener">Privacy Policy</a>')

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


class DeleteAccountFlowTests(TestCase):
    """Account deletion (ISSUES.md A2): requested from the profile or the
    public /delete-account/ page, confirmed through the emailed link."""

    def setUp(self):
        cache.clear()   # rate-limit buckets
        self.user = make_user('deact@example.com')
        self.client.login(username='deact@example.com', password='TestPass123')

    @staticmethod
    def _token_from(email):
        confirm_line = [l for l in email.body.splitlines() if l.startswith('http') and '/confirm/' in l][0]
        return confirm_line.rstrip('/').split('/')[-1]

    def test_request_then_confirm_deletion(self):
        resp = self.client.post(reverse('request-delete-account'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('delete your account', json.loads(resp.content)['message'])
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn(self.user.email, email.to)
        self.assertEqual(email.subject, '[Komüte] Confirm account deletion')
        self.assertIn(f'permanently deleted after {settings.ACCOUNT_DELETION_GRACE_DAYS} days', email.body)
        self.assertNotIn('deactivat', email.body.lower())
        self.assertNotIn('deactivat', email.alternatives[0][0].lower())

        token = self._token_from(email)
        self.assertTrue(CustomUser.objects.get(pk=self.user.pk).is_active)  # still active pre-confirm

        resp = self.client.get(reverse('confirm-delete-account', args=[token]), follow=True)
        self.assertRedirects(resp, reverse('login'))
        self.assertContains(resp, 'Your account is closed now and permanently deleted after')

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertIsNotNone(self.user.deactivated_at)

        # Session should be logged out
        resp = self.client.get(reverse('profile'))
        self.assertNotEqual(resp.status_code, 200)  # redirected to login (302)

    def test_old_deactivate_link_path_still_confirms(self):
        from komute.views import _make_deactivate_token
        resp = self.client.get(f'/deactivate/{_make_deactivate_token(self.user)}/')
        self.assertRedirects(resp, reverse('login'))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_deactivated_user_cannot_login(self):
        self.user.is_active = False
        self.user.deactivated_at = timezone.now()
        self.user.save()
        c = Client()
        resp = c.post(reverse('login'), {'email': 'deact@example.com', 'password': 'TestPass123'})
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_confirm_with_bad_token_errors_gracefully(self):
        resp = self.client.get(reverse('confirm-delete-account', args=['not-a-real-token']))
        self.assertRedirects(resp, reverse('delete-account'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)  # untouched

    def test_confirm_twice_is_idempotent(self):
        self.user.is_active = False
        self.user.deactivated_at = timezone.now()
        self.user.save()
        from komute.views import _make_deactivate_token
        token = _make_deactivate_token(self.user)
        resp = self.client.get(reverse('confirm-delete-account', args=[token]))
        self.assertRedirects(resp, reverse('login'))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_profile_says_delete_not_deactivate(self):
        resp = self.client.get(reverse('profile'))
        self.assertContains(resp, 'Delete Account')
        self.assertContains(resp, 'id="delete-account"')
        self.assertNotContains(resp, 'Deactivate')

    # ── Public page ──

    def test_public_page_needs_no_login(self):
        resp = Client().get(reverse('delete-account'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Delete your account')

    def test_public_page_emails_the_same_confirmation_link(self):
        c = Client()
        resp = c.post(reverse('delete-account'), {'email': 'DEACT@example.com'})
        self.assertRedirects(resp, reverse('delete-account'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        resp = c.get(reverse('confirm-delete-account', args=[self._token_from(mail.outbox[0])]))
        self.assertRedirects(resp, reverse('login'))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_public_page_same_reply_for_unknown_or_closed_account(self):
        closed = make_user('closed@example.com', is_active=False, deactivated_at=timezone.now())
        replies = []
        for email in ('deact@example.com', 'nobody@example.com', closed.email):
            resp = Client().post(reverse('delete-account'), {'email': email}, follow=True)
            replies.append([str(m) for m in resp.context['messages']])
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0], replies[2])
        self.assertEqual(len(mail.outbox), 1)   # only the open account got one

    def test_public_page_rate_limited(self):
        for _ in range(4):
            Client().post(reverse('delete-account'), {'email': 'deact@example.com'})
        self.assertEqual(len(mail.outbox), settings.RATE_LIMITS['delete-account-email'][0])

    def test_signed_in_user_sees_their_email_filled_in(self):
        resp = self.client.get(reverse('delete-account'))
        self.assertContains(resp, 'value="deact@example.com"')


class AccountClosureTripsTests(TestCase):
    """Closing an account cancels its upcoming rides and bookings, so no one
    is left waiting on it, and its rides can't be found or booked."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('close_driver@example.com')
        self.rider = make_user('close_rider@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=2,
            agreed_price_cents=5000, status=RideBooking.STATUS_CONFIRMED,
        )
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=1)

    def _close(self, user):
        from komute.views import _make_deactivate_token
        resp = Client().get(reverse('confirm-delete-account', args=[_make_deactivate_token(user)]), follow=True)
        self.assertContains(resp, 'upcoming rides and bookings have been cancelled')

    def test_closing_driver_cancels_upcoming_rides_and_tells_riders(self):
        departed = make_ride(self.driver, depart_at=timezone.now() - timedelta(hours=1))
        self._close(self.driver)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.status, 'cancelled')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_CANCELLED)
        self.assertEqual([m.to for m in mail.outbox], [[self.rider.mail_to]])
        departed.refresh_from_db()
        self.assertEqual(departed.status, 'posted')  # under way: left to finish

    def test_closing_rider_cancels_bookings_and_declines_invites(self):
        other_ride = make_ride(self.driver)
        draft = RideBooking.objects.create(ride=other_ride, passenger=self.rider, seats=1,
                                           agreed_price_cents=2500, status=RideBooking.STATUS_DRAFT)
        invite = RideInvite.objects.create(driver=self.driver, passenger=self.rider, ride=make_ride(self.driver))
        self._close(self.rider)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_CANCELLED)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 3)
        draft.refresh_from_db()
        self.assertEqual(draft.status, RideBooking.STATUS_CANCELLED)
        other_ride.refresh_from_db()
        self.assertEqual(other_ride.seats_available, 3)  # the draft held none
        invite.refresh_from_db()
        self.assertEqual(invite.status, RideInvite.STATUS_DECLINED)
        # Only the confirmed booking's driver email; none for the draft.
        self.assertEqual([m.to for m in mail.outbox], [[self.driver.mail_to]])

    def test_closed_drivers_leftover_ride_cannot_be_found_or_booked(self):
        from komute.services import bookings as booking_rules
        searcher = make_user('close_searcher@example.com')
        c = Client()
        c.login(username='close_searcher@example.com', password='TestPass123')
        search = lambda: c.post(reverse('search-rides'), {
            'from': 'Toronto, ON', 'to': 'Ottawa, ON', 'date': self.ride.depart_at.date().isoformat(),
            'origin_lat': '43.65', 'origin_lng': '-79.38', 'dest_lat': '45.42', 'dest_lng': '-75.69',
        }).context['total_count']
        self.assertEqual(search(), 1)
        CustomUser.objects.filter(pk=self.driver.pk).update(is_active=False)  # e.g. a cancel that failed
        self.assertEqual(search(), 0)
        ride = Rides.objects.select_related('driver').get(pk=self.ride.pk)
        with self.assertRaises(booking_rules.BookingError) as ctx:
            booking_rules.book_ride(ride, searcher, intent='book', seats_raw=1)
        self.assertEqual(ctx.exception.code, 'ride_unavailable')
        self.assertFalse(booking_rules.reserve_seats(ride.id, 1))


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
        future = time.time() + settings.PASSWORD_RESET_LINK_MINUTES * 60 + 100  # just past the expiry window
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

    def test_purge_keeps_consent_proof_but_drops_network_details(self):
        BiometricConsent.objects.create(user=self.leaver, notice_version='2026-10-18',
                                        ip_address='203.0.113.7', user_agent='TestBrowser/1.0')
        call_command('purge_deactivated_accounts')
        consent = BiometricConsent.objects.get(user=self.leaver)
        self.assertEqual(consent.notice_version, '2026-10-18')
        self.assertIsNone(consent.ip_address)
        self.assertEqual(consent.user_agent, '')

    def test_purge_keeps_legal_acceptance_but_drops_network_details(self):
        LegalAcceptance.objects.filter(user=self.leaver).update(ip_address='203.0.113.7', user_agent='TestBrowser/1.0')
        call_command('purge_deactivated_accounts')
        acceptance = LegalAcceptance.objects.get(user=self.leaver)
        self.assertEqual(acceptance.terms_version, settings.TERMS_VERSION)
        self.assertIsNone(acceptance.ip_address)
        self.assertEqual(acceptance.user_agent, '')

    def test_purge_deletes_axes_sign_in_records_for_the_leaver_only(self):
        from axes.models import AccessAttempt, AccessLog
        now = timezone.now()
        AccessLog.objects.create(username='Leaver@Example.com', ip_address='203.0.113.7',
                                 user_agent='TestBrowser/1.0', attempt_time=now)
        AccessAttempt.objects.create(username='leaver@example.com', ip_address='203.0.113.7',
                                     user_agent='TestBrowser/1.0', attempt_time=now, failures_since_start=1)
        AccessLog.objects.create(username='other_rider@example.com', ip_address='198.51.100.2',
                                 user_agent='TestBrowser/1.0', attempt_time=now)
        call_command('purge_deactivated_accounts')
        self.assertFalse(AccessLog.objects.filter(username__iexact='leaver@example.com').exists())
        self.assertFalse(AccessAttempt.objects.filter(username__iexact='leaver@example.com').exists())
        self.assertTrue(AccessLog.objects.filter(username='other_rider@example.com').exists())

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
                    'purge_ride_search_log', 'scrub_verification_data', 'purge_deactivated_accounts',
                    'purge_user_reports']:
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


class BookingAndReviewRetentionTests(TestCase):
    """Audit P-17: cancelled and rejected bookings go after 6 months even when
    the ride went ahead. Audit P-18: reviews go 2 years after the ride."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('bk_driver@example.com')
        self.rider = make_user('bk_rider@example.com')
        self.long_ago = timezone.now() - timedelta(days=settings.CANCELLED_RIDE_RETENTION_DAYS + 10)
        self.recently = timezone.now() - timedelta(days=10)
        # A ride that went ahead, still inside its 2-year window.
        self.ride = make_ride(self.driver, status='completed', depart_at=self.long_ago, arrive_at=self.long_ago)

    def _booking(self, status, **kwargs):
        return RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                          agreed_price_cents=1000, status=status, **kwargs)

    def test_old_cancelled_and_rejected_bookings_are_deleted(self):
        old_cancel = self._booking(RideBooking.STATUS_CANCELLED, cancelled_at=self.long_ago)
        old_reject = self._booking(RideBooking.STATUS_REJECTED, rejected_at=self.long_ago)
        new_cancel = self._booking(RideBooking.STATUS_CANCELLED, cancelled_at=self.recently)
        new_reject = self._booking(RideBooking.STATUS_REJECTED, rejected_at=self.recently)
        completed = self._booking(RideBooking.STATUS_COMPLETED)
        # Rejected before rejected_at existed: falls back to booked_at.
        legacy = self._booking(RideBooking.STATUS_REJECTED)
        RideBooking.objects.filter(pk=legacy.pk).update(booked_at=self.long_ago)

        call_command('purge_cancelled_rides')

        left = set(RideBooking.objects.values_list('pk', flat=True))
        self.assertEqual(left, {new_cancel.pk, new_reject.pk, completed.pk})
        self.assertNotIn(old_cancel.pk, left)
        self.assertNotIn(old_reject.pk, left)
        self.assertTrue(Rides.objects.filter(pk=self.ride.pk).exists())

    def test_rejecting_records_the_time(self):
        ride = make_ride(self.driver)
        booking = RideBooking.objects.create(ride=ride, passenger=self.rider, seats=1,
                                             agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED)
        c = Client()
        c.force_login(self.driver)
        resp = c.post(reverse('reject-booking', args=[encode_id(booking.id)]))
        self.assertEqual(resp.status_code, 200, resp.content)
        booking.refresh_from_db()
        self.assertEqual(booking.status, RideBooking.STATUS_REJECTED)
        self.assertIsNotNone(booking.rejected_at)

    def test_reviews_go_with_the_ride_after_two_years(self):
        cutoff_days = settings.COMPLETED_RIDE_RETENTION_DAYS
        old = timezone.now() - timedelta(days=cutoff_days + 10)
        old_ride = make_ride(self.driver, status='completed', depart_at=old, arrive_at=old)
        old_booking = RideBooking.objects.create(ride=old_ride, passenger=self.rider, seats=1,
                                                 agreed_price_cents=1000, status=RideBooking.STATUS_COMPLETED)
        recent_booking = self._booking(RideBooking.STATUS_COMPLETED)
        gone = Review.objects.create(booking=old_booking, reviewer=self.rider, reviewee=self.driver, rating=5)
        kept = Review.objects.create(booking=recent_booking, reviewer=self.rider, reviewee=self.driver, rating=4)
        # Already detached from its booking: dated by when it was written.
        orphan_old = Review.objects.create(booking=None, reviewer=self.rider, reviewee=self.driver, rating=3)
        Review.objects.filter(pk=orphan_old.pk).update(created_at=old)
        orphan_new = Review.objects.create(booking=None, reviewer=self.rider, reviewee=self.driver, rating=3)

        call_command('purge_completed_rides')

        self.assertEqual(set(Review.objects.values_list('pk', flat=True)), {kept.pk, orphan_new.pk})
        self.assertFalse(Review.objects.filter(pk=gone.pk).exists())
        self.assertFalse(Rides.objects.filter(pk=old_ride.pk).exists())

    def test_policy_states_both_periods(self):
        html = Client().get(reverse('privacy-policy')).content.decode()
        self.assertIn('bookings that were cancelled or that the driver declined:</strong> up to 6 months', html)
        self.assertIn('<strong>Reviews:</strong> up to 2 years after the ride, then deleted.', html)


class HashidUtilsTests(TestCase):
    def test_roundtrip(self):
        self.assertEqual(decode_id(encode_id(42)), 42)

    def test_decode_garbage_returns_none(self):
        self.assertIsNone(decode_id('not-a-real-hashid'))

    def test_decode_empty_string_returns_none(self):
        self.assertIsNone(decode_id(''))

    def test_uses_hashid_salt(self):
        # Audit S-20: ids come from HASHID_SALT, not SECRET_KEY directly.
        from hashids import Hashids
        expected = Hashids(salt=settings.HASHID_SALT, min_length=8).encode(42)
        self.assertEqual(encode_id(42), expected)


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

    # ── audit F-2: SQL bounding-box prefilter ──

    def test_ride_found_through_stop_near_search_origin(self):
        # Searching from Kingston: far from Toronto, but the ride stops there.
        RideStop.objects.create(ride=self.ride, stop_order=1, location='Kingston, ON',
                                lat=44.23, lng=-76.48)
        resp = self._search(self._rider(), origin_lat='44.23', origin_lng='-76.48')
        self.assertEqual(resp.context['total_count'], 1)
        RideStop.objects.filter(ride=self.ride).delete()
        resp = self._search(self._rider(), origin_lat='44.23', origin_lng='-76.48')
        self.assertEqual(resp.context['total_count'], 0)

    def test_box_keeps_rides_just_inside_radius(self):
        # Due east of the ride's origin at 99% of the radius, where the box's
        # longitude edge is tightest.
        from komute.views import _haversine_km
        r = settings.SEARCH_RADIUS_KM
        lat, lng = 43.65, -79.38
        east = lng
        while _haversine_km(lat, lng, lat, east) < 0.99 * r:
            east += 0.001
        resp = self._search(self._rider(), origin_lat=str(lat), origin_lng=f'{east:.4f}')
        self.assertEqual(resp.context['total_count'], 1)

    def test_corner_of_box_outside_radius_excluded(self):
        # Inside the lat/lng box on both axes but outside the circle.
        from komute.views import _geo_box_q
        r = settings.SEARCH_RADIUS_KM
        lat_off = r / 111.2 * 0.9
        lng_off = r / (111.2 * 0.72) * 0.9
        self.assertTrue(Rides.objects.filter(
            _geo_box_q(43.65 + lat_off, -79.38 + lng_off, r, 'origin_lat', 'origin_lng'),
            pk=self.ride.pk).exists())
        resp = self._search(self._rider(), origin_lat=f'{43.65 + lat_off:.4f}',
                            origin_lng=f'{-79.38 + lng_off:.4f}')
        self.assertEqual(resp.context['total_count'], 0)

    def test_results_paginated_with_correct_total(self):
        for _ in range(11):
            make_ride(self.driver, depart_at=self.ride.depart_at)
        c = self._rider()
        resp = self._search(c)
        self.assertEqual(resp.context['total_count'], 12)
        self.assertEqual(len(resp.context['rides'].object_list), 10)
        resp = self._search(c, page='2')
        self.assertEqual(len(resp.context['rides'].object_list), 2)

    def test_text_only_search_paginates_in_sql(self):
        for _ in range(11):
            make_ride(self.driver, depart_at=self.ride.depart_at)
        resp = self._search(self._rider(), origin_lat='', origin_lng='', dest_lat='', dest_lng='')
        self.assertEqual(resp.context['total_count'], 12)
        self.assertEqual(len(resp.context['rides'].object_list), 10)


class RecurringRideTimezoneTests(TestCase):
    """ISSUES.md H5: recurring rides repeat on the driver's local weekday and
    clock time, including across a DST change."""

    def test_recurring_ride_keeps_local_weekday_and_time(self):
        driver = make_driver_with_vehicle('rr_driver@example.com')
        tz = ZoneInfo('America/Toronto')
        today = timezone.localdate(timezone=tz)
        first = today + timedelta(days=(7 - today.weekday()) % 7 or 7)   # next Monday
        depart = datetime.combine(first, datetime.min.time().replace(hour=20), tzinfo=tz)
        # As far as the 4-month limit allows (audit D-8), on whole weeks.
        from komute.views import _max_ride_date
        end = first + timedelta(weeks=(_max_ride_date() - first).days // 7)
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
        self.assertEqual(len(rides), (end - first).days // 7 + 1)
        local = [r.depart_at.astimezone(tz) for r in rides]
        # 20:00 local on every date, on either side of a DST change.
        self.assertTrue(all(d.weekday() == 0 and (d.hour, d.minute) == (20, 0) for d in local))
        self.assertEqual(local[0].date(), first)
        # Four months doesn't always cross a DST change, so compare with
        # what the calendar says rather than expecting both offsets.
        expected = {datetime.combine(d.date(), datetime.min.time().replace(hour=20), tzinfo=tz).utcoffset()
                    for d in local}
        self.assertEqual({d.utcoffset() for d in local}, expected)


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

    # ── audit F-3: SQL prefilter, one match pass per route ──

    def _driver_client(self):
        c = Client()
        c.login(username='rd_driver@example.com', password='TestPass123')
        return c

    def test_two_routes_each_get_their_own_searches(self):
        make_ride(self.driver, origin='Vancouver, BC', origin_lat=49.28, origin_lng=-123.12,
                  destination='Calgary, AB', dest_lat=51.05, dest_lng=-114.07)
        make_ride(self.driver)  # second ride on the Toronto route
        self._searchers_for_ride(2)
        van = make_user('rd_van@example.com')
        RideSearch.objects.create(
            user=van, origin='Vancouver, BC', origin_lat=49.30, origin_lng=-123.10,
            destination='Calgary, AB', dest_lat=51.04, dest_lng=-114.05,
            search_date=date.today(), passenger_count=1,
        )
        demands = self._driver_client().get(reverse('route-demand')).context['route_demands']
        counts = sorted(d['search_count'] for d in demands)
        self.assertEqual(counts, [1, 2, 2])

    def test_origin_match_alone_is_not_demand(self):
        # Right origin, wrong destination: the box pair must match together.
        u = make_user('rd_half@example.com')
        RideSearch.objects.create(
            user=u, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
            destination='Calgary, AB', dest_lat=51.05, dest_lng=-114.07,
            search_date=date.today(), passenger_count=1,
        )
        demands = self._driver_client().get(reverse('route-demand')).context['route_demands']
        self.assertEqual(demands[0]['search_count'], 0)

    def test_rider_row_uses_latest_search(self):
        self._searchers_for_ride(1, seats=1)
        u = CustomUser.objects.get(email='rd_rider0@example.com')
        RideSearch.objects.create(
            user=u, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
            destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
            search_date=date.today(), passenger_count=3,
        )
        demand = self._driver_client().get(reverse('route-demand')).context['route_demands'][0]
        self.assertEqual(demand['search_count'], 2)
        self.assertEqual(len(demand['riders']), 1)
        self.assertEqual(demand['riders'][0]['seats_needed'], 3)


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

    def test_cancelling_held_booking_emails_driver(self):
        c = Client()
        c.login(username='cxl_rider@example.com', password='TestPass123')
        c.post(reverse('cancel-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual([m.to for m in mail.outbox], [[self.driver.mail_to]])

    def test_cancelling_draft_releases_no_seats(self):
        # A "Chat with driver" draft never held a seat: cancelling it on a
        # full ride must leave the ride full (it let a 3-seat car take 6),
        # and the driver, who never saw a request, gets no email.
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=0, status='full')
        draft_rider = make_user('cxl_draft@example.com')
        draft = RideBooking.objects.create(
            ride=self.ride, passenger=draft_rider, seats=3,
            agreed_price_cents=7500, status=RideBooking.STATUS_DRAFT,
        )
        c = Client()
        c.login(username='cxl_draft@example.com', password='TestPass123')
        resp = c.post(reverse('cancel-booking', args=[encode_id(draft.id)]))
        self.assertEqual(resp.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.status, RideBooking.STATUS_CANCELLED)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 0)
        self.assertEqual(self.ride.status, 'full')
        self.assertEqual(len(mail.outbox), 0)

    def test_cancel_uses_stored_status_not_callers_copy(self):
        # The caller loaded a draft, but it was promoted to a 2-seat request
        # before the cancel ran: the 2 held seats go back, once.
        from komute.services.bookings import cancel_booking
        stale = RideBooking.objects.create(
            ride=self.ride, passenger=make_user('cxl_stale@example.com'), seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_DRAFT,
        )
        RideBooking.objects.filter(pk=stale.pk).update(status=RideBooking.STATUS_REQUESTED, seats=2)
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=0, status='full')
        cancel_booking(stale)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, 2)
        self.assertEqual(self.ride.status, 'posted')

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


class SeatRaceTests(TestCase):
    """Two actions on one ride or booking at the same moment. SQLite runs
    them one after the other, so each test makes the ride lock step itself
    apply the other action, as Postgres would let it land just before."""

    def setUp(self):
        from komute.services import bookings as booking_rules
        self.rules = booking_rules
        self.real_lock = booking_rules.lock_ride
        self.driver = make_driver_with_vehicle('race_driver@example.com')
        self.rider = make_user('race_rider@example.com')
        self.ride = make_ride(self.driver)
        self.draft = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=2,
            agreed_price_cents=5000, status=RideBooking.STATUS_DRAFT,
        )

    def _lock_then(self, change):
        def lock(ride_id):
            change()
            return self.real_lock(ride_id)
        return lock

    def _seats(self):
        self.ride.refresh_from_db()
        return self.ride.seats_available

    def _request_draft(self):
        # What "Request to book" does to the draft: takes its seats.
        RideBooking.objects.filter(pk=self.draft.pk).update(status=RideBooking.STATUS_REQUESTED)
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=F('seats_available') - 2)

    def test_price_accept_on_draft_requested_meanwhile_takes_no_more_seats(self):
        offer = PriceOffer.objects.create(booking=self.draft, proposed_by=self.driver, price_cents=2000)
        with patch.object(self.rules, 'lock_ride', self._lock_then(self._request_draft)):
            result = self.rules.chat_accept(self.rider, self.draft.id, offer.id, None)
        self.assertEqual(result, ('nego_locked', None))
        self.assertEqual(self._seats(), 1)  # 3 - the request's 2, not - 4
        offer.refresh_from_db()
        self.assertEqual(offer.status, PriceOffer.STATUS_PENDING)

    def test_price_accept_on_draft_cancelled_meanwhile_does_not_revive_it(self):
        offer = PriceOffer.objects.create(booking=self.draft, proposed_by=self.driver, price_cents=2000)
        cancel = lambda: RideBooking.objects.filter(pk=self.draft.pk).update(status=RideBooking.STATUS_CANCELLED)
        with patch.object(self.rules, 'lock_ride', self._lock_then(cancel)):
            result = self.rules.chat_accept(self.rider, self.draft.id, offer.id, None)
        self.assertEqual(result, ('booking_inactive', None))
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, RideBooking.STATUS_CANCELLED)
        self.assertEqual(self._seats(), 3)

    def test_price_accept_with_no_seats_left_keeps_offer_pending(self):
        offer = PriceOffer.objects.create(booking=self.draft, proposed_by=self.driver, price_cents=2000)
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=1)
        self.assertEqual(self.rules.chat_accept(self.rider, self.draft.id, offer.id, None), ('no_seats', None))
        offer.refresh_from_db()
        self.assertEqual(offer.status, PriceOffer.STATUS_PENDING)

    def test_book_on_ride_cancelled_meanwhile_is_refused(self):
        other = make_user('race_other@example.com')
        cancel = lambda: Rides.objects.filter(pk=self.ride.pk).update(status='cancelled')
        with patch.object(self.rules, 'lock_ride', self._lock_then(cancel)):
            with self.assertRaises(self.rules.BookingError) as ctx:
                self.rules.book_ride(self.ride, other, intent='book', seats_raw=1)
        self.assertEqual(ctx.exception.code, 'ride_unavailable')
        self.assertFalse(RideBooking.objects.filter(passenger=other).exists())
        self.assertEqual(self._seats(), 3)

    def test_second_book_click_does_not_make_a_second_request(self):
        other = make_user('race_other@example.com')
        self.rules.book_ride(self.ride, other, intent='book', seats_raw=1)
        with self.assertRaises(self.rules.BookingError):
            self.rules.book_ride(self.ride, other, intent='book', seats_raw=1)
        self.assertEqual(RideBooking.objects.filter(passenger=other).count(), 1)
        self.assertEqual(self._seats(), 2)

    def test_invite_accept_after_draft_requested_meanwhile_is_refused(self):
        from komute.services import invites as invite_rules
        invite = RideInvite.objects.create(driver=self.driver, passenger=self.rider, ride=self.ride)
        with patch.object(invite_rules, 'lock_ride', self._lock_then(self._request_draft)):
            with self.assertRaises(invite_rules.InviteError) as ctx:
                invite_rules.accept_invite(invite, self.rider)
        self.assertEqual(ctx.exception.code, 'booking_changed')
        invite.refresh_from_db()
        self.assertEqual(invite.status, RideInvite.STATUS_SENT)
        # The refusal rolls back the whole transaction, which in this test
        # includes the simulated request; the accept itself took nothing.
        self.assertEqual(self._seats(), 3)

    def test_invite_accept_when_full_leaves_invite_open(self):
        from komute.services import invites as invite_rules
        invite = RideInvite.objects.create(driver=self.driver, passenger=self.rider, ride=self.ride)
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=1)
        with self.assertRaises(invite_rules.InviteError):
            invite_rules.accept_invite(invite, self.rider)
        invite.refresh_from_db()
        self.assertEqual(invite.status, RideInvite.STATUS_SENT)
        self.assertEqual(self._seats(), 1)

    def test_edit_after_a_request_landed_is_refused_and_keeps_its_seats(self):
        from komute.services import rides as ride_rules
        self._request_draft()  # arrived after the edit page's own check
        with self.assertRaises(ride_rules.EditBlocked):
            ride_rules.update_ride(
                self.ride, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
                destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
                depart_at=self.ride.depart_at, tz_name='', duration_seconds=0, seats=3,
                price_cents=2500, notes='', preferences_json=None, passenger_gender_pref='any', stops=[])
        self.assertEqual(self._seats(), 1)

    def test_reject_gives_seats_back_once(self):
        self._request_draft()
        self.draft.refresh_from_db()
        self.rules.reject_booking(self.draft)
        with self.assertRaises(self.rules.BookingError):
            self.rules.reject_booking(self.draft)
        self.assertEqual(self._seats(), 3)


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

    def test_fingerprint_skips_closed_rides(self):
        # F-4: history on completed/cancelled rides isn't hashed, but a ride
        # closing still changes the hash.
        open_fp = booking_fingerprint(self.rider)
        Rides.objects.filter(pk=self.ride.pk).update(status='completed')
        closed_fp = booking_fingerprint(self.rider)
        self.assertNotEqual(closed_fp, open_fp)
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_COMPLETED)
        self.assertEqual(booking_fingerprint(self.rider), closed_fp)
        old = make_ride(self.driver, status='cancelled')
        driver_fp = booking_fingerprint(self.driver)
        Rides.objects.filter(pk=old.pk).update(seats_available=0)
        self.assertEqual(booking_fingerprint(self.driver), driver_fp)

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


class WebSocketOriginTests(TransactionTestCase):
    """ISSUES.md L1: only pages on our own hosts may open a WebSocket.

    The validator copies ALLOWED_HOSTS when config.asgi is first imported,
    so these tests use the list it actually holds rather than override it."""

    def setUp(self):
        from channels.security.websocket import OriginValidator
        from config.asgi import application
        self.app = application
        validator = application.application_mapping['websocket']
        self.assertIsInstance(validator, OriginValidator)
        self.assertNotIn('*', validator.allowed_origins)
        self.host = next(h for h in validator.allowed_origins if not h.startswith('.'))

    def test_validator_uses_allowed_hosts(self):
        self.assertIn(self.host, settings.ALLOWED_HOSTS)

    def _connects(self, origin):
        application = self.app
        headers = [(b'host', self.host.encode())]
        if origin is not None:
            headers.append((b'origin', origin))
        async def run():
            comm = WebsocketCommunicator(application, '/ws/notify/', headers=headers)
            connected, _ = await comm.connect()
            await comm.disconnect()
            return connected
        return async_to_sync(run)()

    def test_own_origin_allowed(self):
        # Anonymous, so NotifyConsumer accepts and then closes with 4401.
        self.assertTrue(self._connects(f'https://{self.host}'.encode()))

    def test_other_site_refused(self):
        self.assertFalse(self._connects(b'https://evil.example'))
        self.assertFalse(self._connects(f'https://{self.host}.evil.example'.encode()))

    def test_missing_origin_refused(self):
        self.assertFalse(self._connects(None))


class ReportAndBlockTests(TestCase):
    """ISSUES.md A1: users can report and block each other; a block hides
    rides, refuses bookings, invites and negotiation, and closes the chat."""

    def setUp(self):
        cache.clear()   # rate-limit buckets
        self.driver = make_driver_with_vehicle('rb_driver@example.com')
        self.rider = make_user('rb_rider@example.com')
        self.stranger = make_user('rb_stranger@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2500 + settings.SERVICE_FEE_CENTS,
            status=RideBooking.STATUS_CONFIRMED,
        )

    def _client(self, email):
        c = Client()
        c.login(username=email, password='TestPass123')
        return c

    def _report(self, email, **data):
        return self._client(email).post(reverse('report-user'), data)

    def _search(self):
        return self._client('rb_rider@example.com').post(reverse('search-rides'), {
            'from': 'Toronto, ON', 'to': 'Ottawa, ON',
            'date': self.ride.depart_at.date().isoformat(),
            'origin_lat': '43.65', 'origin_lng': '-79.38',
            'dest_lat': '45.42', 'dest_lng': '-75.69',
        })

    def _block(self, blocker, blocked):
        return UserBlock.objects.create(blocker=blocker, blocked=blocked)

    # ── Report ──

    def test_report_from_booking_saved_and_emailed_to_admin(self):
        resp = self._report('rb_rider@example.com', booking=encode_id(self.booking.id),
                            reason='harassment', details='Rude messages')
        self.assertEqual(resp.status_code, 200)
        report = UserReport.objects.get()
        self.assertEqual((report.reporter, report.reported, report.booking), (self.rider, self.driver, self.booking))
        self.assertEqual((report.reason, report.details, report.status), ('harassment', 'Rude messages', 'open'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [settings.ADMIN_EMAIL])
        self.assertIn('Harassment or threats', mail.outbox[0].subject)
        self.assertIn('Rude messages', mail.outbox[0].body)
        self.assertIn(reverse('admin:komute_userreport_change', args=[report.id]), mail.outbox[0].body)

    def test_driver_reports_passenger(self):
        self._report('rb_driver@example.com', booking=encode_id(self.booking.id), reason='no_show')
        self.assertEqual(UserReport.objects.get().reported, self.rider)

    def test_report_from_ride_page_names_its_driver(self):
        resp = self._report('rb_stranger@example.com', ride=encode_id(self.ride.id), reason='fraud')
        self.assertEqual(resp.status_code, 200)
        report = UserReport.objects.get()
        self.assertEqual((report.reported, report.booking), (self.driver, None))

    def test_report_saved_even_if_admin_email_fails(self):
        with patch('komute.views.EmailMultiAlternatives.send', side_effect=OSError('SES down')), \
             self.assertLogs('komute.tasks', 'ERROR'):
            resp = self._report('rb_rider@example.com', booking=encode_id(self.booking.id), reason='no_show')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(UserReport.objects.count(), 1)

    def test_report_validation(self):
        key = encode_id(self.booking.id)
        for data in ({'reason': 'bogus'}, {'reason': ''}, {'reason': 'other'},
                     {'reason': 'other', 'details': '   '}, {'reason': 'harassment', 'details': 'x' * 2001}):
            resp = self._report('rb_rider@example.com', booking=key, **data)
            self.assertEqual(resp.status_code, 400, data)
        self.assertFalse(UserReport.objects.exists())

    def test_only_a_party_can_report_through_a_booking(self):
        self.assertEqual(self._report('rb_stranger@example.com', booking=encode_id(self.booking.id),
                                      reason='fraud').status_code, 404)
        self.assertEqual(self._report('rb_driver@example.com', ride=encode_id(self.ride.id),
                                      reason='fraud').status_code, 404)   # own ride
        self.assertEqual(self._report('rb_rider@example.com', booking='bad', reason='fraud').status_code, 404)
        self.assertEqual(self._report('rb_rider@example.com', reason='fraud').status_code, 404)
        self.assertFalse(UserReport.objects.exists())

    def test_report_needs_login_and_post(self):
        self.assertEqual(Client().post(reverse('report-user'), {'reason': 'fraud'}).status_code, 302)
        self.assertEqual(self._client('rb_rider@example.com').get(reverse('report-user')).status_code, 405)

    def test_reports_rate_limited(self):
        limits = {**settings.RATE_LIMITS, 'report-user': (2, 3600)}
        with override_settings(RATE_LIMITS=limits):
            codes = [self._report('rb_rider@example.com', booking=encode_id(self.booking.id),
                                  reason='no_show').status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
        self.assertEqual(UserReport.objects.count(), 2)

    # ── Block ──

    def test_block_from_booking_and_ride(self):
        resp = self._client('rb_rider@example.com').post(reverse('block-user'), {'booking': encode_id(self.booking.id)})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(UserBlock.objects.filter(blocker=self.rider, blocked=self.driver).exists())
        # Blocking again is harmless.
        self._client('rb_rider@example.com').post(reverse('block-user'), {'booking': encode_id(self.booking.id)})
        self.assertEqual(UserBlock.objects.count(), 1)

        resp = self._client('rb_stranger@example.com').post(reverse('block-user'), {'ride': encode_id(self.ride.id)})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(UserBlock.objects.filter(blocker=self.stranger, blocked=self.driver).exists())

    def test_cannot_block_through_someone_elses_booking(self):
        resp = self._client('rb_stranger@example.com').post(reverse('block-user'), {'booking': encode_id(self.booking.id)})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(UserBlock.objects.exists())

    def test_block_hides_rides_from_search_both_ways(self):
        self.assertEqual(self._search().context['total_count'], 1)
        block = self._block(self.rider, self.driver)
        self.assertEqual(self._search().context['total_count'], 0)
        block.delete()
        self._block(self.driver, self.rider)
        self.assertEqual(self._search().context['total_count'], 0)

    def test_block_refuses_booking_and_hides_ride_page(self):
        other_ride = make_ride(self.driver, depart_at=timezone.now() + timedelta(days=2))
        self._block(self.driver, self.rider)
        rider = self._client('rb_rider@example.com')
        url = reverse('create-ride-booking', args=[encode_id(other_ride.id)])
        self.assertEqual(rider.get(url).status_code, 404)
        for intent in ('book', 'chat'):
            resp = rider.post(url, {'seats': '1', 'intent': intent})
            self.assertEqual(resp.status_code, 403)
        self.assertFalse(RideBooking.objects.filter(ride=other_ride).exists())
        other_ride.refresh_from_db()
        self.assertEqual(other_ride.seats_available, 3)

    def test_pending_request_cannot_be_accepted_after_block_but_can_be_rejected(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_REQUESTED)
        self._block(self.rider, self.driver)
        driver = self._client('rb_driver@example.com')
        resp = driver.post(reverse('accept-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 403)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_REQUESTED)
        resp = driver.post(reverse('reject-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 200)

    def test_confirmed_booking_left_alone_and_can_be_cancelled(self):
        self._block(self.driver, self.rider)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_CONFIRMED)
        resp = self._client('rb_rider@example.com').post(reverse('cancel-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 200)

    def test_block_refuses_invites_and_hides_rider_from_route_demand(self):
        RideSearch.objects.create(
            user=self.stranger, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
            destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
            search_date=date.today(), passenger_count=1,
        )
        driver = self._client('rb_driver@example.com')
        riders = driver.get(reverse('route-demand')).context['route_demands'][0]['riders']
        self.assertEqual([r['id'] for r in riders], [self.stranger.id])

        self._block(self.stranger, self.driver)
        riders = driver.get(reverse('route-demand')).context['route_demands'][0]['riders']
        self.assertEqual(riders, [])
        payload = lambda rider_id: json.dumps({'rider_id': rider_id, 'ride_id': encode_id(self.ride.id)})
        resp = driver.post(reverse('send-invite'), data=payload(self.stranger.id), content_type='application/json')
        # Same answer as an ineligible rider, so the driver isn't told about the block.
        unknown = driver.post(reverse('send-invite'), data=payload(999999999), content_type='application/json')
        self.assertEqual((resp.status_code, resp.json()), (unknown.status_code, unknown.json()))
        self.assertFalse(RideInvite.objects.exists())

    def test_invite_sent_before_block_cannot_be_accepted(self):
        from komute.views import _make_invite_token
        invite = RideInvite.objects.create(driver=self.driver, passenger=self.stranger, ride=self.ride)
        self._block(self.stranger, self.driver)
        resp = self._client('rb_stranger@example.com').post(
            reverse('accept-invite', args=[_make_invite_token(invite.id)]))
        self.assertContains(resp, 'no longer available')
        self.assertFalse(RideBooking.objects.filter(passenger=self.stranger).exists())
        invite.refresh_from_db()
        self.assertEqual(invite.status, RideInvite.STATUS_SENT)

    def test_http_negotiation_refused_after_block(self):
        offer = PriceOffer.objects.create(booking=self.booking, proposed_by=self.driver, price_cents=2000)
        self._block(self.driver, self.rider)
        rider = self._client('rb_rider@example.com')
        resp = rider.post(reverse('create-price-offer', args=[encode_id(self.booking.id)]), {'price_cents': '1500'})
        self.assertEqual(resp.status_code, 403)
        resp = rider.post(reverse('respond-price-offer', args=[encode_id(offer.id)]), {'action': 'accept'})
        self.assertEqual(resp.status_code, 403)
        offer.refresh_from_db()
        self.assertEqual(offer.status, PriceOffer.STATUS_PENDING)

    def test_chat_page_closed_for_both_sides(self):
        from komute.views import BLOCKED_CHAT_MESSAGE
        self._block(self.driver, self.rider)
        for email, blocked_by_me in (('rb_rider@example.com', False), ('rb_driver@example.com', True)):
            resp = self._client(email).get(reverse('chat', args=[make_chat_token(self.booking.id)]))
            self.assertTrue(resp.context['chat_is_locked'], email)
            self.assertEqual(resp.context['chat_closed_message'], BLOCKED_CHAT_MESSAGE)
            self.assertEqual(resp.context['ride_status_label'], 'Confirmed')
            self.assertEqual(resp.context['blocked_by_me'], blocked_by_me)

    def test_a_cancelled_chat_keeps_its_own_closed_message(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_CANCELLED)
        self._block(self.driver, self.rider)
        resp = self._client('rb_rider@example.com').get(reverse('chat', args=[make_chat_token(self.booking.id)]))
        self.assertIn('cancelled', resp.context['chat_closed_message'])

    def test_unblock_from_profile(self):
        self._block(self.rider, self.driver)
        rider = self._client('rb_rider@example.com')
        page = rider.get(reverse('profile'))
        self.assertEqual([b['name'] for b in page.context['blocked_users']], [self.driver.full_name.title()])
        url = reverse('unblock-user', args=[encode_id(self.driver.id)])
        self.assertContains(page, url)
        # The driver can't lift a block the rider made.
        self._client('rb_driver@example.com').post(reverse('unblock-user', args=[encode_id(self.rider.id)]))
        self.assertTrue(UserBlock.objects.exists())
        self.assertRedirects(rider.post(url), reverse('profile'))
        self.assertFalse(UserBlock.objects.exists())
        self.assertEqual(self._search().context['total_count'], 1)

    def test_inboxes_show_blocked_state(self):
        self._block(self.rider, self.driver)
        convo = self._client('rb_rider@example.com').get(reverse('rider-inbox')).context['conversations'][0]
        self.assertTrue(convo['blocked_by_me'])
        convo = self._client('rb_driver@example.com').get(reverse('driver-inbox')).context['conversations'][0]
        self.assertFalse(convo['blocked_by_me'])

    def test_pages_show_report_and_block_controls(self):
        rider = self._client('rb_rider@example.com')
        driver = self._client('rb_driver@example.com')
        other_ride = make_ride(self.driver, depart_at=timezone.now() + timedelta(days=2))
        pages = (
            (rider, reverse('chat', args=[make_chat_token(self.booking.id)])),
            (rider, reverse('create-ride-booking', args=[encode_id(other_ride.id)])),
            (rider, reverse('rider-inbox')),
            (driver, reverse('driver-inbox')),
        )
        for client, url in pages:
            resp = client.get(url)
            self.assertContains(resp, 'data-safety="report"', msg_prefix=url)
            self.assertContains(resp, 'data-safety="block"', msg_prefix=url)
            self.assertContains(resp, 'js/report-block.js', msg_prefix=url)
            for value, label in UserReport.REASON_CHOICES:
                self.assertContains(resp, f'value="{value}"', msg_prefix=url)
        self.assertContains(rider.get(reverse('profile')), 'Blocked users')

    # ── Staff actions and retention ──

    def _admin_action(self, action, report):
        staff = CustomUser.objects.create_superuser(email='rb_admin@example.com', password='TestPass123',
                                                    full_name='Admin', dob=date(1990, 1, 1))
        c = self._client(staff.email)
        resp = c.post(reverse('admin:komute_userreport_changelist'),
                      {'action': action, '_selected_action': [report.id]})
        self.assertEqual(resp.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.handled_by, staff)
        return report

    def _open_report(self):
        return UserReport.objects.create(reporter=self.rider, reported=self.driver, booking=self.booking, reason='harassment')

    def test_admin_warn_by_email(self):
        report = self._admin_action('warn_by_email', self._open_report())
        self.assertEqual(report.status, UserReport.STATUS_ACTIONED)
        self.assertIn('warned by email', report.staff_note)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.driver.email])
        self.assertIn(reverse('community-guidelines'), mail.outbox[0].body)
        self.assertNotIn(self.rider.email, mail.outbox[0].body)

    def test_admin_deactivate_reported_account(self):
        report = self._admin_action('deactivate_reported_account', self._open_report())
        self.assertEqual(report.status, UserReport.STATUS_ACTIONED)
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.is_active)
        # Not queued for deletion: purge_deactivated_accounts needs deactivated_at.
        self.assertIsNone(self.driver.deactivated_at)

    def test_admin_dismiss(self):
        report = self._admin_action('dismiss', self._open_report())
        self.assertEqual(report.status, UserReport.STATUS_DISMISSED)
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.is_active)
        self.assertEqual(len(mail.outbox), 0)

    def test_purge_user_reports_deletes_only_old_closed_reports(self):
        old = timezone.now() - timedelta(days=settings.REPORT_RETENTION_DAYS + 1)
        old_closed, old_open, new_closed = (self._open_report() for _ in range(3))
        UserReport.objects.filter(pk=old_closed.pk).update(status=UserReport.STATUS_DISMISSED, updated_at=old)
        UserReport.objects.filter(pk=old_open.pk).update(updated_at=old)
        UserReport.objects.filter(pk=new_closed.pk).update(status=UserReport.STATUS_ACTIONED)
        call_command('purge_user_reports')
        self.assertEqual(set(UserReport.objects.values_list('pk', flat=True)), {old_open.pk, new_closed.pk})

    def test_purged_account_loses_its_blocks_but_reports_stay(self):
        self._block(self.rider, self.driver)
        self._block(self.stranger, self.rider)
        self._open_report()
        CustomUser.objects.filter(pk=self.rider.pk).update(
            is_active=False, deactivated_at=timezone.now() - timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS + 1))
        call_command('purge_deactivated_accounts')
        self.assertFalse(UserBlock.objects.exists())
        self.assertEqual(UserReport.objects.count(), 1)


class BlockChatSocketTests(TransactionTestCase):
    """ISSUES.md A1: a block closes open chats live and the socket refuses
    blocked users."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('bk_driver@example.com')
        self.rider = make_user('bk_rider@example.com')
        self.booking = RideBooking.objects.create(
            ride=make_ride(self.driver), passenger=self.rider, seats=1,
            agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED,
        )

    def test_block_locks_open_chat(self):
        driver = Client()
        driver.login(username='bk_driver@example.com', password='TestPass123')
        async def run():
            comm = ChatIsolationTests._communicator(self.rider, self.booking)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            resp = await sync_to_async(driver.post)(reverse('block-user'), {'booking': encode_id(self.booking.id)})
            self.assertEqual(resp.status_code, 200)
            event = await comm.receive_json_from(timeout=5)
            await comm.disconnect()
            return event
        event = async_to_sync(run)()
        self.assertEqual(event['type'], 'booking_status')
        self.assertTrue(event['chat_locked'])
        self.assertEqual(event['closed_message'], "You can't message this user.")

    def test_socket_refused_once_blocked(self):
        UserBlock.objects.create(blocker=self.rider, blocked=self.driver)
        async def run():
            comm = ChatIsolationTests._communicator(self.driver, self.booking)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            return await comm.receive_output(timeout=5)
        with self.assertLogs('komute.chat', 'WARNING'):
            output = async_to_sync(run)()
        self.assertEqual(output, {'type': 'websocket.close', 'code': 4410})

    def test_socket_opened_before_block_refuses_messages_and_offers(self):
        async def run():
            comm = ChatIsolationTests._communicator(self.rider, self.booking)
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            await sync_to_async(UserBlock.objects.create)(blocker=self.driver, blocked=self.rider)
            errors = []
            for frame in ({'type': 'message', 'body': 'hello'}, {'type': 'price_offer', 'price': 10},
                          {'type': 'price_accepted', 'offer_id': 1}):
                await comm.send_json_to(frame)
                errors.append(await comm.receive_json_from(timeout=5))
            await comm.disconnect()
            return errors
        errors = async_to_sync(run)()
        self.assertEqual([e['code'] for e in errors], ['blocked'] * 3)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(PriceOffer.objects.exists())


class EditRideViewTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('edrv_driver@example.com')
        self.other_driver = make_driver_with_vehicle('erlv_driver@example.com')
        self.ride = make_ride(self.driver)

    def _payload(self):
        depart_dt = timezone.now() + timedelta(days=2)
        return {
            'origin': 'Kingston, ON', 'destination': 'Ottawa, ON',
            'date': depart_dt.date().isoformat(), 'depart_time': '11:00',
            'depart_utc': depart_dt.isoformat().replace('+00:00', 'Z'),
            'seats': '4', 'price': '30.00',
            'origin_lat': '44.23', 'origin_lng': '-76.48',
            'destination_lat': '45.42', 'destination_lng': '-75.69',
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

    def test_edit_page_offers_post_ride_preference_tags(self):
        from komute.views import RIDE_PREFERENCE_OPTIONS
        Rides.objects.filter(pk=self.ride.pk).update(preferences_json=['Pet Friendly', 'Pets OK'])
        c = Client()
        c.login(username='edrv_driver@example.com', password='TestPass123')
        resp = c.get(reverse('edit-ride', args=[encode_id(self.ride.id)]))
        # The post-ride tags first; an old-name tag stays visible so it can be removed.
        self.assertEqual(resp.context['pref_options'], RIDE_PREFERENCE_OPTIONS + ['Pets OK'])
        self.assertContains(resp, 'data-pref="Pet Friendly"')

    def test_preference_list_matches_post_ride_and_search(self):
        import re
        from pathlib import Path
        from komute.views import RIDE_PREFERENCE_OPTIONS
        templates = Path(settings.BASE_DIR) / 'templates'
        post = (templates / 'post-ride.html').read_text(encoding='utf-8')
        post_tags = re.findall(r'togglePrefTag\(this\)">\s*<svg.*?</svg>\s*([^<]+?)\s*</button>', post, re.S)
        self.assertEqual(post_tags, RIDE_PREFERENCE_OPTIONS)
        search = (templates / 'search-rides.html').read_text(encoding='utf-8')
        filters = re.findall(r'<option value="([^"]+)"\s+\{% if filter_preference', search)
        self.assertTrue(filters)
        self.assertTrue(set(filters) <= set(RIDE_PREFERENCE_OPTIONS), filters)

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

    def test_quebec_destination_rejected(self):
        # Audit P-11: Quebec is outside the service area at launch.
        resp = self._post(destination='Montreal, QC', destination_lat='45.50', destination_lng='-73.56')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Quebec', resp.context['errors']['destination'])
        self.ride.refresh_from_db()
        self.assertNotEqual(self.ride.destination, 'Montreal, QC')


class QuebecServiceAreaTests(TestCase):
    """Audit P-11 / D-5: no pickups, stops or drop-offs in Quebec at launch
    (no French Terms yet). Rides that only drive through Quebec are fine."""

    def test_border_points(self):
        from komute.service_area import in_quebec
        points = [
            ('Parliament Hill, Ottawa', 45.4236, -75.7009, False),
            ('Museum of History, Gatineau', 45.4302, -75.7093, True),
            ('Aylmer, Gatineau', 45.3945, -75.8440, True),
            ('Orleans, Ottawa', 45.4700, -75.5150, False),
            ('Hawkesbury, ON', 45.6080, -74.6050, False),
            ('Grenville, QC', 45.6280, -74.6000, True),
            ('Montreal', 45.5019, -73.5674, True),
            ('Cornwall, ON', 45.0180, -74.7280, False),
            ('Saint-Regis, QC', 45.0010, -74.6490, True),
            ('Stanstead, QC', 45.0200, -72.0970, True),
            ('Derby Line, VT', 44.9980, -72.0990, False),
            ('Campbellton, NB', 48.0050, -66.6730, False),
            ('Pointe-a-la-Croix, QC', 48.0180, -66.6920, True),
            ('Edmundston, NB', 47.3730, -68.3250, False),
            ('Quebec City', 46.8139, -71.2080, True),
            ('Labrador City, NL', 52.9440, -66.9140, False),
            ('Fermont, QC', 52.7920, -67.0860, True),
            ('Toronto', 43.6532, -79.3832, False),
            ('Iles-de-la-Madeleine', 47.3800, -61.8600, True),
            ('Boston', 42.3601, -71.0589, False),
        ]
        for name, lat, lng, expected in points:
            self.assertEqual(in_quebec(lat, lng), expected, name)

    def _post_ride(self, **overrides):
        driver = make_driver_with_vehicle('qc_driver@example.com')
        depart_dt = timezone.now() + timedelta(days=1)
        payload = {
            'origin': 'Ottawa, ON', 'destination': 'Moncton, NB',
            'date': depart_dt.date().isoformat(), 'depart_time': '10:00',
            'depart_utc': depart_dt.isoformat().replace('+00:00', 'Z'),
            'seats': '3', 'vehicle': str(driver.vehicles.first().id), 'price': '25.00',
            'origin_lat': '45.42', 'origin_lng': '-75.69',
            'destination_lat': '46.09', 'destination_lng': '-64.78',
            'stop_type': ['pickup'],
        }
        payload.update(overrides)
        c = Client()
        c.login(username='qc_driver@example.com', password='TestPass123')
        return c.post(reverse('post-ride'), payload)

    def test_ride_through_quebec_allowed(self):
        resp = self._post_ride()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Rides.objects.filter(destination='Moncton, NB').exists())

    def test_quebec_pickup_rejected(self):
        resp = self._post_ride(origin='Gatineau, QC', origin_lat='45.43', origin_lng='-75.71')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['errors']['origin'],
                         "Komüte isn't available in Quebec yet. Please choose a pickup location outside Quebec.")
        self.assertFalse(Rides.objects.exists())

    def test_quebec_drop_off_rejected(self):
        resp = self._post_ride(destination='Quebec City, QC', destination_lat='46.81', destination_lng='-71.21')
        self.assertIn('drop-off location outside Quebec', resp.context['errors']['destination'])
        self.assertFalse(Rides.objects.exists())

    def test_quebec_stop_rejected(self):
        resp = self._post_ride(stop_location=['Montreal, QC'], stop_lat=['45.50'], stop_lng=['-73.57'])
        self.assertIn('"Montreal, QC" is in Quebec', resp.context['errors']['stops'])
        self.assertFalse(Rides.objects.exists())

    def test_search_in_quebec_rejected(self):
        make_user('qc_rider@example.com')
        c = Client()
        c.login(username='qc_rider@example.com', password='TestPass123')
        resp = c.post(reverse('search-rides'), {
            'from': 'Montreal, QC', 'origin_lat': '45.50', 'origin_lng': '-73.57',
            'to': 'Ottawa, ON', 'dest_lat': '45.42', 'dest_lng': '-75.69',
            'date': (timezone.localdate() + timedelta(days=1)).isoformat(),
        })
        self.assertIn('Quebec', resp.context['errors']['from'])
        self.assertNotIn('to', resp.context['errors'])
        self.assertFalse(RideSearch.objects.exists())

    def test_terms_say_quebec_is_not_served(self):
        for url in (reverse('terms-condition'), reverse('signup')):
            self.assertContains(Client().get(url), 'not yet available in the province of Quebec', msg_prefix=url)


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

    def test_rider_cannot_save_without_dob(self):
        # L-5: riders need it too, or clearing it would undo the booking check.
        make_user('pv_rider@example.com')
        c = Client()
        c.login(username='pv_rider@example.com', password='TestPass123')
        resp = c.post(reverse('profile'), self._base_payload(role='rider', dob=''))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('required', resp.context['errors']['dob'])
        self.assertEqual(CustomUser.objects.get(email='pv_rider@example.com').dob, date(1995, 1, 1))

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
        """Give self.driver a vehicle with a photo, i.e. everything the
        pre-call gates require (besides the request's own photos) before the
        outbound API call is made."""
        defaults = dict(
            make='Honda', model='Civic', year=2019, color='black',
            plate=f'PLT{self.driver.id}', photo_url=photo_url, vehicle_verified=False,
        )
        defaults.update(vehicle_kwargs)
        return Vehicle.objects.create(owner=self.driver, **defaults)

    def _selfie_file(self):
        # A real image: the view decodes and resizes the selfie (H2).
        return {'selfie': SimpleUploadedFile('s.jpg', _jpeg_bytes(), content_type='image/jpeg'),
                **_licence_image(), **_consent_fields()}

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
        resp = c.post(reverse('verify-driver'), _consent_fields())
        self.assertEqual(resp.status_code, 400)
        self.assertIn('license', json.loads(resp.content)['error'].lower())

    def test_missing_selfie_rejected(self):
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        resp = c.post(reverse('verify-driver'), {**_licence_image(), **_consent_fields()})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('selfie', json.loads(resp.content)['error'].lower())

    def test_no_vehicle_rejected(self):
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')):
            resp = c.post(reverse('verify-driver'), self._selfie_file())
        self.assertEqual(resp.status_code, 400)
        self.assertIn('vehicle', json.loads(resp.content)['error'].lower())

    def test_vehicle_without_photo_rejected(self):
        Vehicle.objects.create(
            owner=self.driver, make='Honda', model='Civic', year=2019,
            color='black', plate='NOPHOTO1', photo_url=None,
        )
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')):
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
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')), \
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
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post') as mock_post:
            resp = c.post(reverse('verify-driver'), {**_licence_image(), **_consent_fields()})
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
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')), \
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


def _licence_image():
    """The licence photo the verification modal sends with the selfie (it is
    never stored, audit P-5/P-13)."""
    return {'license_image': SimpleUploadedFile('l.jpg', _jpeg_bytes(), content_type='image/jpeg')}


def _consent_fields():
    """The biometric consent the verification modal sends with the selfie."""
    return {'biometric_consent': '1', 'notice_version': settings.BIOMETRIC_NOTICE_VERSION}


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
            'full_name': 'PM Owner', 'dob': '1995-01-01', 'role': 'rider',
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
        self.owner.selfie_url = self.owner.avatar_url = f'/media/selfies/selfie_{self.owner.id}.jpg'
        self.owner.save()
        vehicle = Vehicle.objects.create(
            owner=self.owner, make='Honda', model='Civic', year=2019,
            color='black', plate='PMMOVE1', photo_url='/media/vehicles/vehicle_1_1.jpg',
        )

        call_command('move_private_media', '--dry-run')
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.selfie_url.startswith('/media/'))

        call_command('move_private_media', '--delete-orphans')
        self.owner.refresh_from_db()
        vehicle.refresh_from_db()
        self.assertTrue(self.owner.selfie_url.startswith('/private-media/selfies/'))
        self.assertEqual(self.owner.avatar_url, self.owner.selfie_url)
        self.assertTrue(vehicle.photo_url.startswith('/private-media/vehicles/'))
        self.assertEqual(list(public.rglob('*.jpg')), [])

        c = Client()
        c.login(username='pm_owner@example.com', password='TestPass123')
        self.assertEqual(b''.join(c.get(self.owner.selfie_url).streaming_content), b'self')
        self.assertEqual(c.get(vehicle.photo_url).status_code, 200)


class LicenceImageMigrationTests(TransactionTestCase):
    """Migration 0040 (audit P-5/P-13): deletes stored licence photos and
    encrypts the licence number and province already in the database."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._override = override_settings(PRIVATE_MEDIA_ROOT=Path(self._tmp))
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._tmp, ignore_errors=True)
        call_command('migrate', 'komute', verbosity=0)

    def test_deletes_licence_files_and_encrypts_fields(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        before, after = [('komute', '0039_set_site_domain')], [('komute', '0040_licence_image_not_stored')]

        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old_user = executor.loader.project_state(before).apps.get_model('komute', 'CustomUser')
        licence = Path(self._tmp) / 'licenses' / '1' / 'old.jpg'
        licence.parent.mkdir(parents=True)
        licence.write_bytes(b'lic')
        selfie = Path(self._tmp) / 'selfies' / '1' / 'me.jpg'
        selfie.parent.mkdir(parents=True)
        selfie.write_bytes(b'me')
        user = old_user.objects.create(
            username='mig@example.com', email='mig@example.com',
            license_number='S1234-56789', license_province='ON',
            license_front_url='/private-media/licenses/1/old.jpg',
            selfie_url='/private-media/selfies/1/me.jpg',
        )

        executor = MigrationExecutor(connection)
        executor.migrate(after)

        self.assertFalse(licence.exists())
        self.assertTrue(selfie.exists())
        with connection.cursor() as cur:
            cur.execute('SELECT license_number, license_province FROM users WHERE id = %s', [user.id])
            raw_number, raw_province = cur.fetchone()
        self.assertNotIn('56789', raw_number)
        self.assertNotEqual(raw_province, 'ON')
        new_user = executor.loader.project_state(after).apps.get_model('komute', 'CustomUser')
        migrated = new_user.objects.get(id=user.id)
        self.assertEqual((migrated.license_number, migrated.license_province), ('S1234-56789', 'ON'))


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
            msgs = self._save_profile(full_name='Renamed', selfie_file=too_big)
        self.assertNotIn('Profile saved successfully.', msgs)
        self.assertIn('Profile photo: File is too large (max 1 MB).', msgs)
        self.assertIn('Your other changes were saved.', msgs)
        self.assertIn('too large', logs.output[0])
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, 'Renamed')
        self.assertFalse(self.user.selfie_url)

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
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post', return_value=mock_resp) as mock_post, \
             self.assertLogs('komute.uploads', 'INFO') as logs:
            resp = self.client.post(reverse('verify-driver'), {'selfie': selfie, **_licence_image(), **_consent_fields()})
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
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post') as mock_post:
            resp = self.client.post(reverse('verify-driver'), {
                'selfie': SimpleUploadedFile('s.jpg', b'garbage', content_type='image/jpeg'),
                **_licence_image(), **_consent_fields()})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Selfie', json.loads(resp.content)['error'])
        mock_post.assert_not_called()

    def _verify(self, licence):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"decision": "REJECTED", "stages": {}, "rejection_reasons": []}
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post', return_value=mock_resp) as mock_post:
            resp = self.client.post(reverse('verify-driver'), {
                'selfie': SimpleUploadedFile('s.jpg', _jpeg_bytes(), content_type='image/jpeg'),
                'license_image': licence, **_consent_fields()})
        return resp, mock_post

    def test_licence_photo_sent_resized_and_never_stored(self):
        # Audit P-5/P-13: the licence photo comes with the request, goes to
        # DL-Verification and is dropped; nothing lands in private storage.
        from PIL import Image
        import base64
        self._verifiable()
        resp, mock_post = self._verify(SimpleUploadedFile('l.jpg', self._big_jpeg(), content_type='image/jpeg'))
        self.assertEqual(resp.status_code, 200)
        sent = base64.b64decode(mock_post.call_args.kwargs['json']['images']['license_image_base64'])
        with Image.open(io.BytesIO(sent)) as img:
            self.assertEqual(img.size, (1200, 800))
        self.assertEqual([p for p in Path(self._tmp).rglob('*') if p.is_file()], [])
        self.assertFalse(hasattr(self.user, 'license_front_url'))

    @override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_large_licence_photo_kept_in_memory(self):
        # Django would spill an upload over FILE_UPLOAD_MAX_MEMORY_SIZE to a
        # temp file on disk; the verify view keeps it in memory instead.
        from django.core.files.uploadedfile import InMemoryUploadedFile
        self._verifiable()
        seen = []
        from komute.views import _resize_image as real_resize
        def spy(file_obj, *args, **kwargs):
            seen.append(type(file_obj))
            return real_resize(file_obj, *args, **kwargs)
        with patch('komute.services.verification.resize_image', side_effect=spy):
            resp, _ = self._verify(SimpleUploadedFile('l.jpg', self._big_jpeg(), content_type='image/jpeg'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen, [InMemoryUploadedFile, InMemoryUploadedFile])

    def test_verify_still_checks_csrf(self):
        self._verifiable()
        c = Client(enforce_csrf_checks=True)
        c.login(username='up_user@example.com', password='TestPass123')
        with patch('requests.post') as mock_post:
            resp = c.post(reverse('verify-driver'), {**_licence_image(), **_consent_fields()})
        self.assertEqual(resp.status_code, 403)
        mock_post.assert_not_called()

    def test_unreadable_licence_photo_rejected_before_call(self):
        self._verifiable()
        resp, mock_post = self._verify(SimpleUploadedFile('l.jpg', b'garbage', content_type='image/jpeg'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('License photo', json.loads(resp.content)['error'])
        mock_post.assert_not_called()

    def test_profile_save_ignores_a_licence_file(self):
        msgs = self._save_profile(license_file=SimpleUploadedFile('l.jpg', _jpeg_bytes(), content_type='image/jpeg'))
        self.assertIn('Profile saved successfully.', msgs)
        self.assertEqual([p for p in Path(self._tmp).rglob('*') if p.is_file()], [])

    def test_licence_number_and_province_encrypted_in_db(self):
        from django.db import connection
        self.user.license_number = 'S1234-56789'
        self.user.license_province = 'ON'
        self.user.save(update_fields=['license_number', 'license_province'])
        with connection.cursor() as cur:
            cur.execute('SELECT license_number, license_province FROM users WHERE id = %s', [self.user.id])
            raw_number, raw_province = cur.fetchone()
        self.assertNotIn('56789', raw_number)
        self.assertNotEqual(raw_province, 'ON')
        self.user.refresh_from_db()
        self.assertEqual((self.user.license_number, self.user.license_province), ('S1234-56789', 'ON'))

    def test_profile_asks_for_licence_photo_in_the_verify_modal(self):
        html = self.client.get(reverse('profile')).content.decode()
        self.assertNotIn('name="license_file"', html)
        self.assertIn('id="verifyLicenseStep"', html)
        self.assertLess(html.index('id="verifyConsentStep"'), html.index('id="verifyLicenseStep"'))
        self.assertLess(html.index('id="verifyLicenseStep"'), html.index('id="verifyCaptureStep"'))
        self.assertIn("fd.append('license_image', license, 'verify_license.jpg')", html)

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
        self.assertIn('shrinkImage(_verifyLicenseFile)', html)
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


class LegalAcceptanceGateTests(TestCase):
    """Audit P-4: signed-in users without an acceptance of the current Terms
    and Privacy Policy are sent to /accept-terms/, which records one."""

    def setUp(self):
        self.user = make_user('la_user@example.com', accepted_terms=False)
        self.client = Client(HTTP_USER_AGENT='TestBrowser/1.0')
        self.client.login(username='la_user@example.com', password='TestPass123')

    def test_page_redirects_to_accept_terms_with_next(self):
        resp = self.client.get(reverse('my-rides') + '?tab=past')
        self.assertRedirects(resp, reverse('accept-terms') + '?next=%2Fmy-rides%2F%3Ftab%3Dpast',
                             fetch_redirect_response=False)

    def test_post_is_blocked_too(self):
        resp = self.client.post(reverse('report-user'), {'reported_id': '1', 'reason': 'spam'})
        self.assertRedirects(resp, reverse('accept-terms'), fetch_redirect_response=False)
        self.assertFalse(UserReport.objects.exists())

    def test_legal_logout_and_deletion_pages_stay_open(self):
        for name in ('terms-condition', 'privacy-policy', 'community-guidelines', 'delete-account', 'accept-terms'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertRedirects(self.client.get(reverse('logout')), reverse('login'))

    def test_signed_out_visitors_are_not_affected(self):
        self.assertEqual(Client().get(reverse('home')).status_code, 200)

    def test_accepting_records_and_continues_to_next(self):
        resp = self.client.post(reverse('accept-terms'), {'agreed_to_terms': '1', 'next': '/my-rides/'},
                                REMOTE_ADDR='203.0.113.9')
        self.assertRedirects(resp, '/my-rides/', fetch_redirect_response=False)
        acceptance = LegalAcceptance.objects.get(user=self.user)
        self.assertEqual(acceptance.source, LegalAcceptance.SOURCE_REACCEPT)
        self.assertEqual(acceptance.terms_version, settings.TERMS_VERSION)
        self.assertEqual(acceptance.privacy_version, settings.PRIVACY_VERSION)
        self.assertEqual(acceptance.ip_address, '203.0.113.9')
        self.assertEqual(acceptance.user_agent, 'TestBrowser/1.0')
        self.assertEqual(self.client.get(reverse('my-rides')).status_code, 200)

    def test_accepting_needs_the_box_ticked(self):
        resp = self.client.post(reverse('accept-terms'), {'next': '/my-rides/'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Please tick the box')
        self.assertFalse(LegalAcceptance.objects.exists())

    def test_offsite_next_ignored(self):
        resp = self.client.post(reverse('accept-terms'), {'agreed_to_terms': '1', 'next': 'https://evil.example/'})
        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)

    def test_page_shows_current_versions(self):
        resp = self.client.get(reverse('accept-terms'))
        self.assertContains(resp, f'(version {settings.TERMS_VERSION})')
        self.assertContains(resp, 'Accept the terms to continue')

    def test_new_version_asks_again(self):
        self.client.post(reverse('accept-terms'), {'agreed_to_terms': '1'})
        self.assertEqual(self.client.get(reverse('my-rides')).status_code, 200)  # session now remembers it
        with override_settings(TERMS_VERSION='2099-01-01'):
            resp = self.client.get(reverse('my-rides'))
            self.assertRedirects(resp, reverse('accept-terms') + '?next=%2Fmy-rides%2F',
                                 fetch_redirect_response=False)
            self.assertContains(self.client.get(reverse('accept-terms')), "We've updated our terms")
            self.client.post(reverse('accept-terms'), {'agreed_to_terms': '1'})
            self.assertEqual(self.client.get(reverse('my-rides')).status_code, 200)
        self.assertEqual(
            list(LegalAcceptance.objects.filter(user=self.user).order_by('accepted_at', 'id')
                 .values_list('terms_version', flat=True)),
            [settings.TERMS_VERSION, '2099-01-01'])

    def test_already_accepted_user_skips_the_page(self):
        accepted = make_user('la_ok@example.com')
        c = Client()
        c.force_login(accepted)
        self.assertEqual(c.get(reverse('my-rides')).status_code, 200)
        self.assertRedirects(c.get(reverse('accept-terms') + '?next=/my-rides/'), '/my-rides/',
                             fetch_redirect_response=False)

    def test_admin_cannot_add_or_edit_acceptance_records(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        model_admin = site._registry[LegalAcceptance]
        request = RequestFactory().get('/')
        request.user = make_user('la_admin@example.com', is_staff=True, is_superuser=True)
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))


class BiometricConsentTests(TestCase):
    """Audit P-2 / D-2: the verification selfie needs the driver's consent to
    the biometric notice on every attempt, and each acceptance is recorded
    before any image is sent to the verification service."""

    setUp = VerifyDriverViewGateTests.setUp
    _make_verifiable = VerifyDriverViewGateTests._make_verifiable

    _APPROVED = {
        "decision": "APPROVED",
        "stages": {"license_ocr": {"passed": True},
                   "face_biometrics": {"passed": True, "vlm_confidence": 0.9},
                   "vehicle_verification": {"passed": True}},
        "rejection_reasons": [],
    }

    def _client(self):
        c = Client(HTTP_USER_AGENT='TestBrowser/1.0')
        c.login(username='vd_driver@example.com', password='TestPass123')
        return c

    def _selfie(self):
        return {'selfie': SimpleUploadedFile('s.jpg', _jpeg_bytes(), content_type='image/jpeg'),
                **_licence_image()}

    def _post(self, data, on_call=None):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = self._APPROVED

        def fake_post(*args, **kwargs):
            if on_call:
                on_call()
            return mock_resp

        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')) as mock_file, \
             patch('requests.post', side_effect=fake_post) as mock_post:
            resp = self._client().post(reverse('verify-driver'), data, REMOTE_ADDR='203.0.113.7')
        return resp, mock_post, mock_file

    def test_without_consent_nothing_is_read_or_sent(self):
        self._make_verifiable()
        resp, mock_post, mock_file = self._post(self._selfie())
        self.assertEqual(resp.status_code, 400)
        self.assertIn('biometric notice', json.loads(resp.content)['error'])
        mock_post.assert_not_called()
        mock_file.assert_not_called()
        self.assertFalse(BiometricConsent.objects.exists())
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.driver_verified)

    def test_consent_must_be_exactly_ticked(self):
        self._make_verifiable()
        resp, mock_post, _ = self._post({**self._selfie(), **_consent_fields(), 'biometric_consent': 'on'})
        self.assertEqual(resp.status_code, 400)
        mock_post.assert_not_called()

    def test_outdated_notice_version_rejected(self):
        self._make_verifiable()
        resp, mock_post, _ = self._post({**self._selfie(), **_consent_fields(), 'notice_version': '2000-01-01'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('reload', json.loads(resp.content)['error'])
        mock_post.assert_not_called()
        self.assertFalse(BiometricConsent.objects.exists())

    def test_consent_recorded_before_images_are_sent(self):
        self._make_verifiable()
        counts_at_call = []
        resp, mock_post, _ = self._post(
            {**self._selfie(), **_consent_fields()},
            on_call=lambda: counts_at_call.append(BiometricConsent.objects.filter(user=self.driver).count()),
        )
        self.assertEqual(resp.status_code, 200)
        mock_post.assert_called_once()
        self.assertEqual(counts_at_call, [1])
        consent = BiometricConsent.objects.get(user=self.driver)
        self.assertEqual(consent.notice_version, settings.BIOMETRIC_NOTICE_VERSION)
        self.assertEqual(consent.ip_address, '203.0.113.7')
        self.assertEqual(consent.user_agent, 'TestBrowser/1.0')
        self.assertLess(timezone.now() - consent.accepted_at, timedelta(minutes=1))

    def test_each_attempt_records_its_own_consent(self):
        # Re-verification every DRIVER_REVERIFY_DAYS asks again.
        self._make_verifiable()
        self._post({**self._selfie(), **_consent_fields()})
        self._post({**self._selfie(), **_consent_fields()})
        self.assertEqual(BiometricConsent.objects.filter(user=self.driver).count(), 2)

    def test_no_record_when_request_fails_before_the_call(self):
        # No vehicle: rejected before anything is sent, so nothing to record.
        resp, mock_post, _ = self._post({**self._selfie(), **_consent_fields()})
        self.assertEqual(resp.status_code, 400)
        mock_post.assert_not_called()
        self.assertFalse(BiometricConsent.objects.exists())

    def test_unparseable_client_address_stored_as_null(self):
        self._make_verifiable()
        with patch('komute.views._client_ip', return_value='unknown'):
            self._post({**self._selfie(), **_consent_fields()})
        self.assertIsNone(BiometricConsent.objects.get(user=self.driver).ip_address)

    def test_profile_shows_notice_before_the_camera(self):
        html = self._client().get(reverse('profile')).content.decode()
        self.assertIn('id="verifyConsentStep"', html)
        self.assertIn('id="verifyCaptureStep" style="display:none;"', html)
        self.assertIn('id="vsConsentAgreeBtn" class="btn-verify-identity" disabled', html)
        self.assertIn('Notice version ' + settings.BIOMETRIC_NOTICE_VERSION, html)
        self.assertIn("fd.append('notice_version', '" + settings.BIOMETRIC_NOTICE_VERSION + "')", html)
        self.assertIn("fd.append('biometric_consent', '1')", html)
        self.assertIn('within 30 days of the verification decision', html)
        # The consent step comes before the camera button in the modal.
        self.assertLess(html.index('id="verifyConsentStep"'), html.index('onclick="openCameraForVerify()"'))

    def test_admin_cannot_add_or_edit_consent_records(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        model_admin = site._registry[BiometricConsent]
        request = RequestFactory().get('/')
        request.user = make_user('bc_admin@example.com', is_staff=True, is_superuser=True)
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))


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
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'fake-bytes')), \
             patch('requests.post', return_value=mock_resp):
            return c.post(reverse('verify-driver'), self._selfie_file())

    def test_approval_with_any_failed_check_is_a_rejection(self):
        # The service's combined score can approve with a failed check; a
        # driver needs face, licence and vehicle to pass (audit P-10).
        failed_reason = {'license_ocr': 'OTHER', 'face_biometrics': 'FACE_MISMATCH',
                         'vehicle_verification': 'VEHICLE_MISMATCH'}
        vehicle = self._make_verifiable()
        for failed, code in failed_reason.items():
            with self.subTest(failed=failed):
                cache.clear()
                VerificationLog.objects.all().delete()
                stages = {name: {"passed": name != failed} for name in failed_reason}
                stages["face_biometrics"]["vlm_confidence"] = 0.95
                data = json.loads(self._call({"decision": "APPROVED", "stages": stages,
                                              "rejection_reasons": []}).content)
                self.assertFalse(data['verified'])
                self.assertTrue(data['can_request_review'])
                self.assertIn("check didn't pass", data['decision']['details'])
                self.driver.refresh_from_db()
                self.assertFalse(self.driver.driver_verified)
                vehicle.refresh_from_db()
                self.assertEqual(vehicle.vehicle_verified, failed != 'vehicle_verification')
                vehicle.vehicle_verified = False
                vehicle.save(update_fields=['vehicle_verified'])
                log = VerificationLog.objects.get(user=self.driver)
                self.assertEqual(log.result, VerificationLog.RESULT_REJECTED)
                self.assertEqual(log.reason_codes, [code])
                self.assertEqual(log.review_status, '')

    def test_approval_missing_a_check_is_a_rejection(self):
        self._make_verifiable()
        data = json.loads(self._call({"decision": "APPROVED",
                                      "stages": {"face_biometrics": {"passed": True}}}).content)
        self.assertFalse(data['verified'])
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.driver_verified)

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


class VerificationReviewQueueTests(TestCase):
    """Audit P-10 (Quebec Law 25 s.12.1): borderline checks and driver
    requests go to a staff review queue in the admin."""

    setUp = VerifyDriverViewGateTests.setUp
    _make_verifiable = VerifyDriverViewGateTests._make_verifiable
    _selfie_file = VerifyDriverViewGateTests._selfie_file
    _call = VerificationResultStorageTests._call

    REJECTED = {"decision": "REJECTED",
                "stages": {"license_ocr": {"passed": True},
                           "face_biometrics": {"passed": False, "vlm_confidence": 0.3},
                           "vehicle_verification": {"passed": True}},
                "rejection_reasons": ["Face mismatch"]}

    def _driver_client(self):
        c = Client()
        c.login(username='vd_driver@example.com', password='TestPass123')
        return c

    def _admin_action(self, action, log):
        staff = make_user('vr_admin@example.com', is_staff=True, is_superuser=True)
        c = Client()
        c.login(username=staff.email, password='TestPass123')
        resp = c.post(reverse('admin:komute_verificationlog_changelist'),
                      {'action': action, '_selected_action': [log.id]})
        self.assertEqual(resp.status_code, 302)
        log.refresh_from_db()
        self.driver.refresh_from_db()
        return staff

    def test_clear_rejection_is_not_queued_but_can_be_reviewed(self):
        self._make_verifiable()
        data = json.loads(self._call(self.REJECTED).content)
        self.assertFalse(data['review_pending'])
        self.assertTrue(data['can_request_review'])
        log = VerificationLog.objects.get(user=self.driver)
        self.assertEqual(log.review_status, '')
        self.assertEqual(len(mail.outbox), 0)

    def test_unclear_decision_is_queued_and_driver_stays_unverified(self):
        self._make_verifiable()
        data = json.loads(self._call({"decision": "MANUAL_REVIEW", "stages": {}}).content)
        self.assertFalse(data['verified'])
        self.assertTrue(data['review_pending'])
        self.assertFalse(data['can_request_review'])
        log = VerificationLog.objects.get(user=self.driver)
        self.assertEqual(log.result, VerificationLog.RESULT_MANUAL_REVIEW)
        self.assertEqual(log.review_status, VerificationLog.REVIEW_PENDING)
        self.assertEqual(log.review_source, VerificationLog.REVIEW_SOURCE_BORDERLINE)
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.driver_verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [settings.ADMIN_EMAIL])
        self.assertIn(f'/admin/komute/verificationlog/{log.id}/', mail.outbox[0].body)

    def test_clean_approval_is_not_queued(self):
        self._make_verifiable()
        self._call({"decision": "APPROVED",
                    "stages": {name: {"passed": True} for name in
                               ("license_ocr", "face_biometrics", "vehicle_verification")}})
        self.assertEqual(VerificationLog.objects.get(user=self.driver).review_status, '')

    def test_driver_requests_review_of_rejection(self):
        self._make_verifiable()
        self._call(self.REJECTED)
        c = self._driver_client()
        self.assertContains(c.get(reverse('profile')), 'Ask a person to review it')
        resp = c.post(reverse('verify-driver-review'), {'comment': 'I have grown a beard since.'})
        self.assertEqual(resp.status_code, 200)
        log = VerificationLog.objects.get(user=self.driver)
        self.assertEqual(log.review_status, VerificationLog.REVIEW_PENDING)
        self.assertEqual(log.review_source, VerificationLog.REVIEW_SOURCE_DRIVER)
        self.assertEqual(log.driver_comment, 'I have grown a beard since.')
        self.assertIsNotNone(log.review_requested_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('I have grown a beard since.', mail.outbox[0].body)
        self.assertContains(c.get(reverse('profile')), 'is reviewing your last verification')
        # Only once per attempt.
        self.assertEqual(c.post(reverse('verify-driver-review')).status_code, 409)

    def test_review_request_needs_a_rejected_latest_attempt(self):
        c = self._driver_client()
        self.assertEqual(c.post(reverse('verify-driver-review')).status_code, 400)
        VerificationLog.objects.create(user=self.driver, result=VerificationLog.RESULT_APPROVED)
        self.assertEqual(c.post(reverse('verify-driver-review')).status_code, 400)
        self.assertEqual(c.post(reverse('verify-driver-review'), {'comment': 'x' * 2001}).status_code, 400)
        self.assertEqual(Client().post(reverse('verify-driver-review')).status_code, 302)

    def test_admin_approve_verifies_driver_and_vehicle(self):
        vehicle = self._make_verifiable(is_primary=True)
        log = VerificationLog.objects.create(
            user=self.driver, result=VerificationLog.RESULT_REJECTED,
            review_status=VerificationLog.REVIEW_PENDING, review_source=VerificationLog.REVIEW_SOURCE_DRIVER)
        staff = self._admin_action('approve_after_review', log)
        self.assertEqual(log.result, VerificationLog.RESULT_APPROVED)
        self.assertEqual(log.review_status, VerificationLog.REVIEW_DONE)
        self.assertEqual(log.reviewer, staff)
        self.assertIsNotNone(log.reviewed_at)
        self.assertTrue(self.driver.driver_verified)
        self.assertIsNotNone(self.driver.driver_verified_at)
        vehicle.refresh_from_db()
        self.assertTrue(vehicle.vehicle_verified)
        self.assertEqual(mail.outbox[-1].to, [self.driver.email])
        self.assertIn('approved', mail.outbox[-1].body)

    def test_admin_reject_closes_unclear_result(self):
        log = VerificationLog.objects.create(
            user=self.driver, result=VerificationLog.RESULT_MANUAL_REVIEW,
            review_status=VerificationLog.REVIEW_PENDING, review_source=VerificationLog.REVIEW_SOURCE_BORDERLINE)
        self._admin_action('reject_after_review', log)
        self.assertEqual(log.result, VerificationLog.RESULT_REJECTED)
        self.assertEqual(log.review_status, VerificationLog.REVIEW_DONE)
        self.assertFalse(self.driver.driver_verified)
        self.assertEqual(mail.outbox[-1].to, [self.driver.email])

    def test_admin_skips_attempts_not_in_the_queue(self):
        log = VerificationLog.objects.create(user=self.driver, result=VerificationLog.RESULT_REJECTED)
        self._admin_action('approve_after_review', log)
        self.assertEqual(log.result, VerificationLog.RESULT_REJECTED)
        self.assertFalse(self.driver.driver_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_notice_text_on_profile_and_privacy_policy(self):
        profile = self._driver_client().get(reverse('profile')).content.decode()
        self.assertIn('It was made automatically by Komüte', profile)
        self.assertIn('Ask for a review', profile)
        policy = self.client.get(reverse('privacy-policy')).content.decode()
        self.assertIn('Automated decision and human review', policy)
        self.assertIn('s.12.1', policy)

    def test_data_export_includes_review_but_not_staff_notes(self):
        VerificationLog.objects.create(
            user=self.driver, result=VerificationLog.RESULT_REJECTED,
            review_status=VerificationLog.REVIEW_DONE, driver_comment='my comment', notes='staff only')
        resp = self._driver_client().get(reverse('download-my-data'))
        body = b''.join(resp.streaming_content) if resp.streaming else resp.content
        self.assertIn(b'my comment', body)
        self.assertNotIn(b'staff only', body)


class AuditFixTests(TestCase):
    """Fixes from the full security audit (audit/full-review branch)."""

    def setUp(self):
        cache.clear()
        self.driver = make_driver_with_vehicle('au_driver@example.com')
        self.rider = make_user('au_rider@example.com')
        self.ride = make_ride(self.driver)

    def _client(self, email):
        c = Client()
        c.login(username=email, password='TestPass123')
        return c

    def test_closed_account_session_loses_access(self):
        # Sign in through the app's own login view (it used to store a
        # session user id that outlived deactivation).
        c = Client()
        c.post(reverse('login'), {'email': 'au_driver@example.com', 'password': 'TestPass123'})
        self.assertEqual(c.get(reverse('profile')).status_code, 200)
        CustomUser.objects.filter(pk=self.driver.pk).update(is_active=False)
        self.assertRedirects(c.get(reverse('profile')), reverse('login'), fetch_redirect_response=False)
        self.assertRedirects(c.get(reverse('post-ride')), reverse('login'), fetch_redirect_response=False)
        self.assertEqual(c.post(reverse('verify-driver')).status_code, 401)

    def test_receipt_pdf_escapes_user_markup(self):
        hostile = 'Ann & <b>Bo</b> <img src="http://127.0.0.1:9/x.png"/>'
        CustomUser.objects.filter(pk=self.driver.pk).update(full_name=hostile)
        CustomUser.objects.filter(pk=self.rider.pk).update(full_name=hostile)
        RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                   agreed_price_cents=2600, status=RideBooking.STATUS_CONFIRMED)
        with patch('urllib.request.urlopen', side_effect=AssertionError('PDF fetched a URL')):
            resp = self._client('au_driver@example.com').get(
                reverse('ride-receipt-pdf', args=[encode_id(self.ride.id)]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')

    def test_no_booking_or_draft_on_finished_rides(self):
        rider = self._client('au_rider@example.com')
        past = make_ride(self.driver, depart_at=timezone.now() - timedelta(hours=1))
        cancelled = make_ride(self.driver, status='cancelled')
        for ride in (past, cancelled):
            for intent in ('book', 'chat'):
                resp = rider.post(reverse('create-ride-booking', args=[encode_id(ride.id)]),
                                  {'seats': '1', 'intent': intent})
                self.assertEqual(resp.status_code, 400, (ride.status, intent))
        self.assertFalse(RideBooking.objects.filter(passenger=self.rider).exists())

    def _review(self, booking):
        return self._client('au_rider@example.com').post(
            reverse('submit-review'),
            data=json.dumps({'booking_id': encode_id(booking.id), 'reviewee_id': self.driver.id, 'rating': 1}),
            content_type='application/json')

    def test_only_bookings_that_happened_can_be_reviewed(self):
        Rides.objects.filter(pk=self.ride.pk).update(status='completed')
        for status in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_REJECTED, RideBooking.STATUS_CANCELLED):
            booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                                 agreed_price_cents=2600, status=status)
            self.assertEqual(self._review(booking).status_code, 400, status)
            booking.delete()
        self.assertFalse(Review.objects.exists())
        booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                             agreed_price_cents=2600, status=RideBooking.STATUS_CONFIRMED)
        self.assertEqual(self._review(booking).status_code, 200)

    def test_invite_cannot_be_accepted_after_departure(self):
        from komute.views import _make_invite_token
        invite = RideInvite.objects.create(driver=self.driver, passenger=self.rider, ride=self.ride)
        Rides.objects.filter(pk=self.ride.pk).update(depart_at=timezone.now() - timedelta(minutes=5))
        resp = self._client('au_rider@example.com').post(reverse('accept-invite', args=[_make_invite_token(invite.id)]))
        self.assertContains(resp, 'no longer available')
        self.assertFalse(RideBooking.objects.filter(passenger=self.rider).exists())

    def test_unverified_email_cannot_post_book_or_accept_invite(self):
        # S-10: the app's own login admits unverified emails, so posting,
        # booking, chat drafts and invite acceptance check it themselves.
        from komute.views import _make_invite_token
        CustomUser.objects.filter(pk__in=[self.driver.pk, self.rider.pk]).update(email_verified=False)

        driver = self._client('au_driver@example.com')
        resp = driver.get(reverse('post-ride'))
        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)
        rides_before = Rides.objects.count()
        resp = driver.post(reverse('post-ride'), {'origin': 'A', 'destination': 'B', 'seats': '2'})
        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)
        self.assertEqual(Rides.objects.count(), rides_before)

        rider = self._client('au_rider@example.com')
        for intent in ('book', 'chat'):
            resp = rider.post(reverse('create-ride-booking', args=[encode_id(self.ride.id)]),
                              {'seats': '1', 'intent': intent})
            self.assertEqual(resp.status_code, 403, intent)
            self.assertIn('verify your email', resp.json()['error'])

        invite = RideInvite.objects.create(driver=self.driver, passenger=self.rider, ride=self.ride)
        resp = rider.post(reverse('accept-invite', args=[_make_invite_token(invite.id)]))
        self.assertContains(resp, 'verify your email')
        invite.refresh_from_db()
        self.assertNotEqual(invite.status, RideInvite.STATUS_ACCEPTED)
        self.assertFalse(RideBooking.objects.filter(passenger=self.rider).exists())
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.seats_available, self.ride.seats_total)

    def test_missing_dob_cannot_book_or_accept_invite(self):
        # L-5: accounts from before signup asked for a DOB.
        from komute.views import _make_invite_token
        CustomUser.objects.filter(pk=self.rider.pk).update(dob=None)
        rider = self._client('au_rider@example.com')
        for intent in ('book', 'chat'):
            resp = rider.post(reverse('create-ride-booking', args=[encode_id(self.ride.id)]),
                              {'seats': '1', 'intent': intent})
            self.assertEqual(resp.status_code, 403, intent)
            self.assertIn('date of birth', resp.json()['error'])

        invite = RideInvite.objects.create(driver=self.driver, passenger=self.rider, ride=self.ride)
        resp = rider.post(reverse('accept-invite', args=[_make_invite_token(invite.id)]))
        self.assertContains(resp, 'date of birth')
        invite.refresh_from_db()
        self.assertNotEqual(invite.status, RideInvite.STATUS_ACCEPTED)
        self.assertFalse(RideBooking.objects.filter(passenger=self.rider).exists())

    def test_verified_email_can_book(self):
        resp = self._client('au_rider@example.com').post(
            reverse('create-ride-booking', args=[encode_id(self.ride.id)]), {'seats': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(RideBooking.objects.filter(passenger=self.rider,
                                                   status=RideBooking.STATUS_REQUESTED).exists())

    def _accept_invite(self):
        from komute.views import _make_invite_token
        invite = RideInvite.objects.create(driver=self.driver, passenger=self.rider, ride=self.ride)
        resp = self._client('au_rider@example.com').post(
            reverse('accept-invite', args=[_make_invite_token(invite.id)]))
        invite.refresh_from_db()
        self.ride.refresh_from_db()
        return resp, invite

    def test_gender_preference_enforced_at_booking_and_invites(self):
        # L-8: search hid the ride, but a direct link could still book it.
        Rides.objects.filter(pk=self.ride.pk).update(passenger_gender_pref='women_only')
        CustomUser.objects.filter(pk=self.rider.pk).update(gender='male')
        rider = self._client('au_rider@example.com')
        for intent in ('book', 'chat'):
            resp = rider.post(reverse('create-ride-booking', args=[encode_id(self.ride.id)]),
                              {'seats': '1', 'intent': intent})
            self.assertEqual(resp.status_code, 403, intent)
        self.assertFalse(RideBooking.objects.filter(passenger=self.rider).exists())

        RideSearch.objects.create(
            user=self.rider, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
            destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
            search_date=date.today(), passenger_count=1)
        driver = self._client('au_driver@example.com')
        resp = driver.post(reverse('send-invite'), content_type='application/json',
                           data=json.dumps({'rider_id': self.rider.id, 'ride_id': encode_id(self.ride.id)}))
        self.assertEqual(resp.status_code, 400)
        rider_name = 'Test au_rider@example.com'.title()
        self.assertNotContains(driver.get(reverse('route-demand')), rider_name)

        resp, invite = self._accept_invite()
        self.assertContains(resp, 'different gender')
        self.assertNotEqual(invite.status, RideInvite.STATUS_ACCEPTED)
        self.assertFalse(RideBooking.objects.filter(passenger=self.rider).exists())

        CustomUser.objects.filter(pk=self.rider.pk).update(gender='female')
        self.assertContains(driver.get(reverse('route-demand')), rider_name)
        resp = rider.post(reverse('create-ride-booking', args=[encode_id(self.ride.id)]), {'seats': '1'})
        self.assertEqual(resp.status_code, 200)

    def test_invite_accept_charges_service_fee(self):
        # L-7: the invite booking used to leave the service fee out.
        resp, invite = self._accept_invite()
        self.assertEqual(resp.status_code, 302)
        booking = RideBooking.objects.get(passenger=self.rider)
        self.assertEqual(booking.status, RideBooking.STATUS_CONFIRMED)
        self.assertEqual(booking.agreed_price_cents, self.ride.price_cents + settings.SERVICE_FEE_CENTS)
        self.assertEqual(self.ride.seats_available, self.ride.seats_total - 1)

    def test_invite_accept_refused_with_an_active_booking(self):
        # L-7: a rider with a pending request got a second, confirmed booking.
        RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                   agreed_price_cents=2600, status=RideBooking.STATUS_REQUESTED)
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=2)
        resp, invite = self._accept_invite()
        self.assertContains(resp, 'already have a booking')
        self.assertNotEqual(invite.status, RideInvite.STATUS_ACCEPTED)
        self.assertEqual(RideBooking.objects.filter(passenger=self.rider).count(), 1)
        self.assertEqual(self.ride.seats_available, 2)

    def test_invite_accept_confirms_the_riders_draft(self):
        # L-7: the draft's seats and negotiated price carry over, and no
        # second booking is created.
        draft = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=2,
                                           agreed_price_cents=4100, status=RideBooking.STATUS_DRAFT)
        resp, invite = self._accept_invite()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(invite.status, RideInvite.STATUS_ACCEPTED)
        draft.refresh_from_db()
        self.assertEqual(draft.status, RideBooking.STATUS_CONFIRMED)
        self.assertEqual(draft.agreed_price_cents, 4100)
        self.assertEqual(RideBooking.objects.filter(passenger=self.rider).count(), 1)
        self.assertEqual(self.ride.seats_available, self.ride.seats_total - 2)

    def test_invite_accept_needs_seats_for_the_draft(self):
        RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=2,
                                   agreed_price_cents=5100, status=RideBooking.STATUS_DRAFT)
        Rides.objects.filter(pk=self.ride.pk).update(seats_available=1)
        resp, invite = self._accept_invite()
        self.assertContains(resp, 'enough seats')
        self.assertNotEqual(invite.status, RideInvite.STATUS_ACCEPTED)
        self.assertEqual(self.ride.seats_available, 1)

    def _ride_form(self, **overrides):
        depart_dt = timezone.now() + timedelta(days=2)
        payload = {
            'origin': 'Toronto, ON', 'destination': 'Ottawa, ON',
            'date': depart_dt.date().isoformat(), 'depart_time': '10:00',
            'depart_utc': depart_dt.isoformat().replace('+00:00', 'Z'),
            'seats': '3', 'vehicle': str(self.driver.vehicles.first().id), 'price': '25.00',
            'origin_lat': '43.65', 'origin_lng': '-79.38',
            'destination_lat': '45.42', 'destination_lng': '-75.69',
        }
        payload.update(overrides)
        return payload

    def test_out_of_range_numbers_are_form_errors_not_500s(self):
        # L-10: "inf" and huge numbers raised OverflowError or overflowed
        # integer columns.
        driver = self._client('au_driver@example.com')
        rides_before = Rides.objects.count()
        for price in ('inf', 'nan', '1e30', '1000.01'):
            resp = driver.post(reverse('post-ride'), self._ride_form(price=price))
            self.assertEqual(resp.status_code, 200, price)
            self.assertIn('price', resp.context['errors'], price)
        self.assertEqual(Rides.objects.count(), rides_before)

        resp = driver.post(reverse('post-ride'), self._ride_form(duration_seconds='9' * 20))
        self.assertEqual(resp.status_code, 302)
        ride = Rides.objects.latest('id')
        self.assertIsNone(ride.arrive_at)
        self.assertIsNone(ride.expected_duration_seconds)

        edit_url = reverse('edit-ride', args=[encode_id(self.ride.id)])
        resp = driver.post(edit_url, self._ride_form(price='inf'))
        self.assertIn('price', resp.context['errors'])
        resp = driver.post(edit_url, self._ride_form(depart_utc='', timezone_offset='9' * 20))
        self.assertIn('depart_time', resp.context['errors'])
        resp = driver.post(edit_url, self._ride_form(
            depart_utc='', date='9999-12-31', depart_time='23:59', timezone_offset='840'))
        self.assertIn('depart_time', resp.context['errors'])
        resp = driver.post(edit_url, self._ride_form(duration_seconds='9' * 20))
        self.assertRedirects(resp, reverse('my-rides'), fetch_redirect_response=False)

        rider = self._client('au_rider@example.com')
        resp = rider.post(reverse('search-rides'), {
            'from': 'Toronto, ON', 'to': 'Ottawa, ON',
            'date': self.ride.depart_at.date().isoformat(),
            'origin_lat': '43.65', 'origin_lng': '-79.38', 'dest_lat': '45.42', 'dest_lng': '-75.69',
            'seats': '9' * 20, 'max_price': 'inf',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(RideSearch.objects.get(user=self.rider).passenger_count, 8)

        booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                             agreed_price_cents=2600, status=RideBooking.STATUS_CONFIRMED)
        resp = rider.post(reverse('create-price-offer', args=[encode_id(booking.id)]), {'price_cents': 'inf'})
        self.assertEqual(resp.status_code, 400)

    def test_ride_summary_report_uses_the_report_flow(self):
        # L-11: "Report issue" showed a made-up ticket number and sent nothing.
        booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                             agreed_price_cents=2600, status=RideBooking.STATUS_COMPLETED)
        Rides.objects.filter(pk=self.ride.pk).update(status='completed')
        url = reverse('ride-summary', args=[encode_id(self.ride.id)])
        report_button = f'data-safety="report" data-booking="{encode_id(booking.id)}"'
        for email in ('au_rider@example.com', 'au_driver@example.com'):
            resp = self._client(email).get(url)
            self.assertContains(resp, report_button, msg_prefix=email)
            self.assertContains(resp, 'id="rbRoot"', msg_prefix=email)
            self.assertNotContains(resp, 'SR-', msg_prefix=email)

    def test_ride_summary_review_flags_from_one_query(self):
        # F-5: already_reviewed came from one Review query per booking.
        Rides.objects.filter(pk=self.ride.pk).update(status='completed')
        bookings = []
        for i in range(3):
            p = self.rider if i == 0 else make_user(f'au_sum{i}@example.com')
            bookings.append(RideBooking.objects.create(
                ride=self.ride, passenger=p, seats=1, agreed_price_cents=2600,
                status=RideBooking.STATUS_COMPLETED))
        Review.objects.create(booking=bookings[1], reviewer=self.driver,
                              reviewee=bookings[1].passenger, rating=5)
        # Someone else's review of booking 2 doesn't count for the driver.
        Review.objects.create(booking=bookings[2], reviewer=bookings[2].passenger,
                              reviewee=self.driver, rating=4)
        url = reverse('ride-summary', args=[encode_id(self.ride.id)])
        driver = self._client('au_driver@example.com')
        resp = driver.get(url)
        flags = {b['booking_id']: b['already_reviewed'] for b in resp.context['bookings']}
        self.assertEqual(flags, {encode_id(bookings[0].id): False,
                                 encode_id(bookings[1].id): True,
                                 encode_id(bookings[2].id): False})
        # Adding bookings doesn't add queries.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as three:
            driver.get(url)
        for i in range(3, 6):
            RideBooking.objects.create(ride=self.ride, passenger=make_user(f'au_sum{i}@example.com'),
                                       seats=1, agreed_price_cents=2600,
                                       status=RideBooking.STATUS_COMPLETED)
        with CaptureQueriesContext(connection) as six:
            driver.get(url)
        self.assertEqual(len(six), len(three))

    def test_ride_success_copy_link_shares_the_booking_page(self):
        # L-13: "copied" showed even when the copy failed, and the link was
        # this driver-only page.
        resp = self._client('au_driver@example.com').get(
            reverse('ride-success', args=[encode_id(self.ride.id)]))
        self.assertEqual(resp.status_code, 200)
        booking_url = reverse('create-ride-booking', args=[encode_id(self.ride.id)])
        self.assertEqual(resp.context['share']['url'], 'http://testserver' + booking_url)
        self.assertNotContains(resp, 'writeText(window.location.href)')
        # The Share row is live and its data is JSON, not markup.
        self.assertContains(resp, 'id="shareWhatsApp"')
        self.assertContains(resp, '<script id="shareData" type="application/json">')

    def test_shared_ride_link_survives_signup(self):
        # Someone without an account opens a shared ride link: signup, first
        # login and role selection all carry them back to the ride.
        booking_url = reverse('create-ride-booking', args=[encode_id(self.ride.id)])
        c = Client()
        resp = c.get(booking_url)
        login_url = f"{reverse('login')}?next={booking_url}"
        self.assertRedirects(resp, login_url, fetch_redirect_response=False)
        self.assertContains(c.get(login_url), f"{reverse('signup')}?next={booking_url}")

        resp = c.post(reverse('signup'), {
            'full_name': 'Sam Share', 'email': 'sam@example.com',
            'country_code': '+1', 'phone_number': '4155552672', 'password': 'GoodPassword1!',
            'confirm_password': 'GoodPassword1!', 'dob': '1995-01-01', 'agreed_to_terms': '1', 'next': booking_url,
        })
        self.assertRedirects(resp, login_url, fetch_redirect_response=False)
        resp = c.post(reverse('login'), {'email': 'sam@example.com', 'password': 'GoodPassword1!',
                                         'next': booking_url})
        role_url = f"{reverse('role-select')}?next={booking_url}"
        self.assertRedirects(resp, role_url, fetch_redirect_response=False)
        self.assertContains(c.get(role_url), f'name="next" value="{booking_url}"')
        resp = c.post(reverse('role-select'), {'role': 'rider', 'next': booking_url})
        self.assertRedirects(resp, booking_url, fetch_redirect_response=False)

        # Off-site targets are dropped.
        resp = c.post(reverse('role-select'), {'role': 'rider', 'next': 'https://evil.example/'})
        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)

    def test_deleted_account_searches_go_at_once_and_it_cant_be_invited(self):
        # P-14: the privacy policy says search logs are deleted immediately;
        # they used to wait for the 45-day purge, and kept the closed account
        # on drivers' Route Demand lists.
        from komute.views import _make_deactivate_token
        RideSearch.objects.create(
            user=self.rider, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
            destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
            search_date=date.today(), passenger_count=1)
        # A prior booking with this driver also makes a rider inviteable.
        RideBooking.objects.create(ride=make_ride(self.driver), passenger=self.rider, seats=1,
                                   agreed_price_cents=2600, status=RideBooking.STATUS_COMPLETED)
        driver = self._client('au_driver@example.com')
        rider_name = 'Test au_rider@example.com'.title()
        self.assertContains(driver.get(reverse('route-demand')), rider_name)

        resp = Client().get(reverse('confirm-delete-account', args=[_make_deactivate_token(self.rider)]))
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)
        self.assertFalse(RideSearch.objects.filter(user=self.rider).exists())
        self.assertTrue(CustomUser.objects.filter(pk=self.rider.pk).exists())  # rest waits for the purge

        self.assertNotContains(driver.get(reverse('route-demand')), rider_name)
        resp = driver.post(reverse('send-invite'), content_type='application/json',
                           data=json.dumps({'rider_id': self.rider.id, 'ride_id': encode_id(self.ride.id)}))
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(RideInvite.objects.filter(passenger=self.rider).exists())
        self.assertEqual(len(mail.outbox), 0)


    def test_accept_does_not_overwrite_a_concurrent_cancel(self):
        from komute import views
        booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                             agreed_price_cents=2600, status=RideBooking.STATUS_REQUESTED)
        real_get = views.get_object_or_404

        def read_then_rider_cancels(*args, **kwargs):
            obj = real_get(*args, **kwargs)
            RideBooking.objects.filter(id=booking.id).update(status=RideBooking.STATUS_CANCELLED)
            return obj

        with patch('komute.views.get_object_or_404', side_effect=read_then_rider_cancels):
            resp = self._client('au_driver@example.com').post(reverse('accept-booking', args=[encode_id(booking.id)]))
        self.assertEqual(resp.status_code, 400)
        booking.refresh_from_db()
        self.assertEqual(booking.status, RideBooking.STATUS_CANCELLED)
    def test_rate_limit_ignores_client_supplied_forwarded_for(self):
        limits = {**settings.RATE_LIMITS, 'forgot-password-ip': (2, 3600), 'forgot-password-email': (100, 3600)}
        with override_settings(RATE_LIMITS=limits):
            for i in range(3):
                # A fresh fake address each time, Nginx's real one appended last.
                Client().post(reverse('forgot-password'), {'email': 'au_rider@example.com'},
                              HTTP_X_FORWARDED_FOR=f'10.0.0.{i}, 203.0.113.7')
        self.assertEqual(len(mail.outbox), 2)

    def test_allauth_pages_go_to_the_apps_own(self):
        c = Client()
        for path, name in (('/accounts/signup/', 'signup'), ('/accounts/login/', 'login'),
                           ('/accounts/password/reset/', 'forgot-password')):
            self.assertRedirects(c.get(path), reverse(name), fetch_redirect_response=False)
        resp = c.post('/accounts/signup/', {'email': 'sneaky@example.com', 'password1': 'Xx1!aaaaaaaa', 'password2': 'Xx1!aaaaaaaa'})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(CustomUser.objects.filter(email='sneaky@example.com').exists())

    def test_email_confirmation_link_still_works(self):
        from allauth.account.models import EmailAddress, EmailConfirmationHMAC
        address = EmailAddress.objects.create(user=self.rider, email=self.rider.email, primary=True, verified=False)
        key = EmailConfirmationHMAC(address).key
        c = Client()
        url = reverse('account_confirm_email', args=[key])
        resp = c.get(url)
        if resp.status_code == 200:   # confirmation page with a confirm button
            resp = c.post(url)
        self.assertIn(resp.status_code, (301, 302))
        self.rider.refresh_from_db()
        self.assertTrue(self.rider.email_verified)

    def test_permissions_policy_header(self):
        resp = Client().get(reverse('home'))
        policy = resp['Permissions-Policy']
        self.assertIn('camera=(self)', policy)
        self.assertIn('geolocation=(self)', policy)
        self.assertIn('microphone=()', policy)

    def test_home_page_claims_email_not_phone_verification(self):
        # Audit L-14: phone numbers are never verified (phone_verified is
        # never set), so the home page mustn't say they are.
        html = Client().get(reverse('home')).content.decode()
        self.assertNotIn('verified with phone', html)
        self.assertIn('has a verified email address', html)

    def test_no_made_up_usage_numbers(self):
        # Audit L-12: the rider, tonnage and city counts were never real.
        for name in ('home', 'login', 'signup'):
            html = Client().get(reverse(name)).content.decode()
            for claim in ('25,000', '1,200+ tons', '100+ Cities',
                          'Carbon Neutral', '100% Green Energy'):
                self.assertNotIn(claim, html, f'{claim!r} on {name}')
        html = Client().get(reverse('home')).content.decode()
        self.assertIn('Save up to 80 kg of CO<sub>2</sub>', html)
        self.assertIn('for every 100 km carpooled', html)
        self.assertIn('four passengers share one car', html)

    def test_purge_removes_allauth_email_addresses(self):
        from allauth.account.models import EmailAddress
        EmailAddress.objects.create(user=self.rider, email=self.rider.email, primary=True, verified=True)
        EmailAddress.objects.create(user=self.rider, email='old-address@example.com', verified=True)
        CustomUser.objects.filter(pk=self.rider.pk).update(
            is_active=False, deactivated_at=timezone.now() - timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS + 1))
        call_command('purge_deactivated_accounts')
        self.assertFalse(EmailAddress.objects.filter(user=self.rider).exists())
        self.assertFalse(EmailAddress.objects.filter(email__in=['au_rider@example.com', 'old-address@example.com']).exists())
    def test_nameless_users_are_not_shown_by_email(self):
        CustomUser.objects.filter(pk=self.rider.pk).update(full_name='')
        RideSearch.objects.create(
            user=self.rider, origin='Toronto, ON', origin_lat=43.65, origin_lng=-79.38,
            destination='Ottawa, ON', dest_lat=45.42, dest_lng=-75.69,
            search_date=date.today(), passenger_count=1,
        )
        RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                   agreed_price_cents=2600, status=RideBooking.STATUS_REQUESTED)
        driver = self._client('au_driver@example.com')
        for name in ('route-demand', 'driver-inbox', 'my-rides'):
            resp = driver.get(reverse(name))
            self.assertNotContains(resp, 'au_rider@example.com', msg_prefix=name)
    def test_edit_ride_accepts_departure_without_offset(self):
        c = self._client('au_driver@example.com')
        depart = (timezone.now() + timedelta(days=2)).replace(microsecond=0, tzinfo=None)
        resp = c.post(reverse('edit-ride', args=[encode_id(self.ride.id)]), {
            'origin': 'Toronto, ON', 'destination': 'Ottawa, ON',
            'origin_lat': '43.65', 'origin_lng': '-79.38', 'destination_lat': '45.42', 'destination_lng': '-75.69',
            'date': depart.date().isoformat(), 'depart_time': depart.strftime('%H:%M'),
            'depart_utc': depart.isoformat(), 'seats': '3', 'price': '25.00',
            'vehicle': str(self.driver.vehicles.first().id),
        })
        self.assertIn(resp.status_code, (200, 302))
    def test_zero_fee_adds_nothing_and_shows_no_fee_line(self):
        with override_settings(SERVICE_FEE_CENTS=0):
            page = self._client('au_rider@example.com').get(reverse('create-ride-booking', args=[encode_id(self.ride.id)]))
            self.assertNotContains(page, 'Service fee')
            self._client('au_rider@example.com').post(
                reverse('create-ride-booking', args=[encode_id(self.ride.id)]), {'seats': '2'})
        booking = RideBooking.objects.get(ride=self.ride, passenger=self.rider)
        self.assertEqual(booking.agreed_price_cents, 2 * self.ride.price_cents)
    def test_driver_phone_hidden_until_booking_confirmed(self):
        CustomUser.objects.filter(pk=self.driver.pk).update(phone='+14165550123')
        url = reverse('create-ride-booking', args=[encode_id(self.ride.id)])
        rider = self._client('au_rider@example.com')
        self.assertNotContains(rider.get(url), '+14165550123')
        booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                             agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED)
        self.assertNotContains(rider.get(url), '+14165550123')
        RideBooking.objects.filter(pk=booking.pk).update(status=RideBooking.STATUS_CONFIRMED)
        self.assertContains(rider.get(url), '+14165550123')

class AuditChatFixTests(TransactionTestCase):
    """Socket-side fixes from the full security audit."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('ac_driver@example.com')
        self.rider = make_user('ac_rider@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(
            ride=self.ride, passenger=self.rider, seats=1,
            agreed_price_cents=2600, status=RideBooking.STATUS_DRAFT,
        )

    def test_socket_offers_are_bounded(self):
        offer = async_to_sync(ChatConsumer().save_price_offer)
        for price in (-5, 0, 1000.01, 'inf', 'nan', 'abc', None):
            self.assertEqual(offer(self.rider, self.booking.id, price), (None, 'bad_price'), price)
        self.assertFalse(PriceOffer.objects.exists())
        created, error = offer(self.rider, self.booking.id, 12.5)
        self.assertIsNone(error)
        self.assertEqual(created.price_cents, 1250)

    def test_draft_on_cancelled_ride_cannot_be_confirmed(self):
        offer = PriceOffer.objects.create(booking=self.booking, proposed_by=self.driver, price_cents=2000)
        Rides.objects.filter(pk=self.ride.pk).update(status='cancelled')
        result, _ = async_to_sync(ChatConsumer().accept_price_offer)(self.rider, self.booking.id, offer.id, None)
        self.assertEqual(result, 'booking_inactive')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, RideBooking.STATUS_DRAFT)


class AuditRecurringRideTests(TestCase):
    def test_recurring_series_capped_at_four_months(self):
        # Audit D-8: the same limit as a single ride.
        from komute.views import _max_ride_date
        driver = make_driver_with_vehicle('ar_driver@example.com')
        tz = ZoneInfo('America/Toronto')
        first = timezone.localdate(timezone=tz) + timedelta(days=7)
        depart = datetime.combine(first, datetime.min.time().replace(hour=8), tzinfo=tz)
        c = Client()
        c.login(username='ar_driver@example.com', password='TestPass123')
        base = {
            'origin': 'Toronto, ON', 'destination': 'Ottawa, ON',
            'date': first.isoformat(), 'depart_time': '08:00',
            'depart_utc': depart.astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00', 'Z'),
            'tz': 'America/Toronto',
            'seats': '3', 'vehicle': str(driver.vehicles.first().id), 'price': '25.00',
            'origin_lat': '43.65', 'origin_lng': '-79.38',
            'destination_lat': '45.42', 'destination_lng': '-75.69',
            'is_recurring': '1', 'recurring_days': '0,1,2,3,4,5,6',
        }
        resp = c.post(reverse('post-ride'), {**base, 'recurring_end_date': '9999-12-31'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('recurring_end_date', resp.context['errors'])
        self.assertFalse(Rides.objects.filter(driver=driver).exists())
        max_date = _max_ride_date()
        resp = c.post(reverse('post-ride'), {**base, 'recurring_end_date': (max_date + timedelta(days=1)).isoformat()})
        self.assertIn('recurring_end_date', resp.context['errors'])
        self.assertFalse(Rides.objects.filter(driver=driver).exists())
        resp = c.post(reverse('post-ride'), {**base, 'recurring_end_date': max_date.isoformat()})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Rides.objects.filter(driver=driver).count(), (max_date - first).days + 1)


class ContentSecurityPolicyTests(TestCase):
    """Audit S-8: a CSP on every page, report-only until CSP_ENFORCE, with
    violations logged by /csp-report/."""

    def setUp(self):
        cache.clear()

    def _report(self, client=None, **fields):
        body = {'csp-report': {
            'document-uri': 'https://komute.example/reset-password/secret-token-123/',
            'effective-directive': 'script-src-elem',
            'blocked-uri': 'https://evil.example/x.js?session=abc',
            'source-file': 'https://komute.example/reset-password/secret-token-123/',
            'line-number': 42,
            **fields,
        }}
        return (client or Client()).post(reverse('csp-report'), json.dumps(body),
                                         content_type='application/csp-report')

    def test_report_only_header_by_default(self):
        resp = Client().get(reverse('home'))
        self.assertFalse(resp.has_header('Content-Security-Policy'))
        policy = resp['Content-Security-Policy-Report-Only']
        for part in ("default-src 'self'", "object-src 'none'", "frame-ancestors 'none'",
                     'https://unpkg.com', "font-src 'self'", 'https://tile.openstreetmap.org',
                     'https://photon.komoot.io', 'https://router.project-osrm.org', 'report-uri /csp-report/'):
            self.assertIn(part, policy)
        # Fonts are self-hosted (audit F-7).
        self.assertNotIn('fonts.googleapis.com', policy)
        self.assertNotIn('fonts.gstatic.com', policy)

    @override_settings(CSP_ENFORCE=True)
    def test_enforced_when_setting_is_on(self):
        resp = Client().get(reverse('home'))
        self.assertIn("default-src 'self'", resp['Content-Security-Policy'])
        self.assertFalse(resp.has_header('Content-Security-Policy-Report-Only'))

    def test_report_is_logged_without_tokens_or_query_strings(self):
        with self.assertLogs('komute.csp', level='WARNING') as logs:
            resp = self._report()
        self.assertEqual(resp.status_code, 204)
        line = logs.output[0]
        self.assertIn("directive='script-src-elem'", line)
        self.assertIn("blocked='https://evil.example'", line)
        self.assertIn("page='reset-password'", line)
        self.assertIn('line=42', line)
        self.assertNotIn('secret-token-123', line)
        self.assertNotIn('session=abc', line)

    def test_inline_and_static_sources(self):
        with self.assertLogs('komute.csp', level='WARNING') as logs:
            self._report(**{'blocked-uri': 'inline', 'source-file': f'https://komute.example{settings.STATIC_URL}js/place-autocomplete.js'})
        self.assertIn("blocked='inline'", logs.output[0])
        self.assertIn(f"source='{settings.STATIC_URL}js/place-autocomplete.js'", logs.output[0])

    def test_signed_in_user_without_accepted_terms_is_not_redirected(self):
        user = CustomUser.objects.create_user(email='csp_user@example.com', password='TestPass123', full_name='Csp User')
        c = Client()
        c.force_login(user)
        with self.assertLogs('komute.csp', level='WARNING'):
            resp = self._report(client=c)
        self.assertEqual(resp.status_code, 204)

    def test_bad_reports_are_dropped(self):
        url = reverse('csp-report')
        self.assertEqual(Client().get(url).status_code, 405)
        with self.assertNoLogs('komute.csp', level='WARNING'):
            for body in ('not json', '[]', '{"other": 1}', '{"csp-report": "x"}',
                         json.dumps({'csp-report': {'blocked-uri': 'x' * 10000}})):
                resp = Client().post(url, body, content_type='application/csp-report')
                self.assertEqual(resp.status_code, 204)

    def test_rate_limited(self):
        limits = {**settings.RATE_LIMITS, 'csp-report': (2, 600)}
        with override_settings(RATE_LIMITS=limits), self.assertLogs('komute.csp', level='WARNING') as logs:
            for _ in range(5):
                self.assertEqual(self._report().status_code, 204)
        self.assertEqual(len(logs.output), 2)


@override_settings(AXES_ENABLED=True)
class AxesClientIpTests(TestCase):
    """Audit S-16: axes must see the address Nginx saw (the last
    X-Forwarded-For entry), not Daphne's REMOTE_ADDR, which is 127.0.0.1
    for everyone in production."""

    def setUp(self):
        from axes.models import AccessAttempt
        cache.clear()
        AccessAttempt.objects.all().delete()
        self.user = make_user('ax_user@example.com')

    def _login(self, email, password, ip):
        return Client().post(reverse('login'), {'email': email, 'password': password},
                             REMOTE_ADDR='127.0.0.1', HTTP_X_FORWARDED_FOR=ip)

    def test_failures_recorded_against_forwarded_ip(self):
        from axes.models import AccessAttempt
        self._login('nobody@example.com', 'wrong', '203.0.113.5')
        attempt = AccessAttempt.objects.get()
        self.assertEqual(attempt.ip_address, '203.0.113.5')
        self.assertEqual(attempt.username, 'nobody@example.com')

    def test_one_ip_failing_does_not_lock_out_others(self):
        for i in range(6):
            self._login(f'nobody{i}@example.com', 'wrong', '203.0.113.5')
        resp = self._login('ax_user@example.com', 'TestPass123', '198.51.100.7')
        self.assertRedirects(resp, reverse('role-select'), fetch_redirect_response=False)

    def test_account_locked_after_failures_from_many_ips(self):
        for i in range(5):
            self._login('ax_user@example.com', 'wrong', f'203.0.113.{i}')
        resp = self._login('ax_user@example.com', 'TestPass123', '198.51.100.7')
        self.assertEqual(resp.status_code, 429)
        self.assertNotIn('_auth_user_id', resp.client.session)


class AccountActionThrottleTests(TestCase):
    """Audit S-15: signed-in actions that email an address or check the
    password are throttled per account (not per IP), and mail to any one
    address is capped across accounts."""

    def setUp(self):
        cache.clear()
        self.user = make_user('th_user@example.com')
        self.other = make_user('th_other@example.com')

    def _client(self, email):
        c = Client()
        c.login(username=email, password='TestPass123')
        return c

    def test_change_password_limited_across_ips(self):
        c = self._client('th_user@example.com')
        wrong = {'current_password': 'nope', 'new_password': 'NewPassword12!', 'confirm_password': 'NewPassword12!'}
        for i in range(5):
            resp = c.post(reverse('change-password'), wrong, REMOTE_ADDR=f'203.0.113.{i}')
            self.assertEqual(resp.status_code, 400)
        # Right password, new network: still blocked.
        resp = c.post(reverse('change-password'), {**wrong, 'current_password': 'TestPass123'},
                      REMOTE_ADDR='198.51.100.1')
        self.assertEqual(resp.status_code, 429)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('TestPass123'))

    def test_change_email_limited_per_account(self):
        c = self._client('th_user@example.com')
        for i in range(5):
            resp = c.post(reverse('change-email'), {'new_email': 'x@example.com', 'password': 'wrong'},
                          REMOTE_ADDR=f'203.0.113.{i}')
            self.assertEqual(resp.status_code, 400)
        resp = c.post(reverse('change-email'), {'new_email': 'x@example.com', 'password': 'TestPass123'})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(len(mail.outbox), 0)

    def test_mail_to_one_address_capped_across_accounts(self):
        victim = {'new_email': 'victim@example.com', 'password': 'TestPass123'}
        a, b = self._client('th_user@example.com'), self._client('th_other@example.com')
        for c in (a, a, a, b, b):
            self.assertEqual(c.post(reverse('change-email'), victim).status_code, 200)
        self.assertEqual(b.post(reverse('change-email'), victim).status_code, 429)
        # Resending to the pending address counts against the same cap.
        self.assertEqual(a.post(reverse('send_verification_email')).status_code, 429)
        self.assertEqual(len([m for m in mail.outbox if m.to == ['victim@example.com']]), 5)

    def test_send_verification_limited(self):
        c = self._client('th_user@example.com')
        for _ in range(5):
            self.assertEqual(c.post(reverse('send_verification_email')).status_code, 200)
        resp = c.post(reverse('send_verification_email'))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()['error'], 'Too many attempts. Please try again later.')
        self.assertEqual(len(mail.outbox), 5)

    def test_profile_delete_request_limited(self):
        c = self._client('th_user@example.com')
        for _ in range(3):
            self.assertEqual(c.post(reverse('request-delete-account')).status_code, 200)
        self.assertEqual(c.post(reverse('request-delete-account')).status_code, 429)
        self.assertEqual(len(mail.outbox), 3)


class EmailQueueTests(TestCase):
    """Audit F-6: fire-and-forget emails go through the django-q2 queue."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('eq_driver@example.com')
        self.rider = make_user('eq_rider@example.com')
        self.ride = make_ride(self.driver)
        self.booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                                  agreed_price_cents=2500)

    def _queued(self):
        from django_q.models import OrmQ
        return [q.task for q in OrmQ.objects.all()]

    def test_email_is_queued_and_worker_sends_it(self):
        from django_q.conf import Conf
        from komute.tasks import send_email
        from komute.views import _send_booking_request_email
        with patch.object(Conf, 'SYNC', False):
            _send_booking_request_email(self.booking)
        self.assertEqual(len(mail.outbox), 0)
        [job] = self._queued()
        self.assertEqual(job['func'], 'komute.tasks.send_email')
        # The pickled message survives the round trip and sends as built.
        send_email(*job['args'])
        self.assertEqual(mail.outbox[0].to, ['eq_driver@example.com'])
        self.assertIn('New booking request', mail.outbox[0].subject)

    def test_rolled_back_request_queues_nothing(self):
        from django.db import transaction
        from django_q.conf import Conf
        from komute.views import _send_booking_request_email
        with patch.object(Conf, 'SYNC', False):
            try:
                with transaction.atomic():
                    _send_booking_request_email(self.booking)
                    raise RuntimeError
            except RuntimeError:
                pass
        self.assertEqual(self._queued(), [])

    def test_failed_send_raises_so_the_job_is_retried(self):
        from django_q.conf import Conf
        from komute.tasks import send_email, send_later
        with patch.object(Conf, 'SYNC', False):
            send_later(mail.EmailMessage('s', 'b', to=['x@example.com']), EmailLog.KIND_WELCOME)
        [job] = self._queued()
        log = EmailLog.objects.get()
        with patch('django.core.mail.EmailMessage.send', side_effect=OSError('SES down')),                 patch.object(Conf, 'SYNC', False), patch.object(Conf, 'MAX_ATTEMPTS', 3):
            for attempt, status in ((1, 'retrying'), (2, 'retrying'), (3, 'failed')):
                with self.assertRaises(OSError):
                    send_email(*job['args'])
                log.refresh_from_db()
                self.assertEqual((log.attempts, log.status), (attempt, status))
        self.assertEqual(log.last_error, 'OSError: SES down')

    def test_queue_unavailable_falls_back_to_sending_now(self):
        from komute.tasks import send_later
        with patch('django_q.tasks.async_task', side_effect=RuntimeError('db down')),                 self.assertLogs('komute.tasks', 'ERROR'):
            send_later(mail.EmailMessage('fallback', 'b', to=['x@example.com']), EmailLog.KIND_WELCOME)
        self.assertEqual([m.subject for m in mail.outbox], ['fallback'])
        self.assertEqual(EmailLog.objects.get().status, EmailLog.STATUS_SENT)

    def test_email_log_row_tracks_the_send(self):
        from komute.views import _send_booking_request_email
        _send_booking_request_email(self.booking)
        log = EmailLog.objects.get()
        self.assertEqual(log.kind, EmailLog.KIND_BOOKING_REQUEST)
        self.assertEqual(log.user, self.driver)
        self.assertEqual(log.to_masked, 'eq***@example.com')
        self.assertIn('New booking request', log.subject)
        self.assertEqual((log.status, log.attempts, log.last_error), ('sent', 1, ''))
        self.assertIsNotNone(log.sent_at)

    def test_email_log_admin_is_read_only_and_searchable(self):
        from komute.views import _send_booking_request_email
        _send_booking_request_email(self.booking)
        staff = make_user('eq_staff@example.com', is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(staff)
        url = reverse('admin:komute_emaillog_changelist')
        resp = c.get(url, {'q': 'eq_driver@example.com'})
        self.assertContains(resp, 'eq***@example.com')
        resp = c.get(url, {'q': 'nobody@example.com'})
        self.assertNotContains(resp, 'eq***@example.com')
        self.assertEqual(c.get(url, {'status__exact': 'sent'}).status_code, 200)
        self.assertEqual(c.get(reverse('admin:komute_emaillog_add')).status_code, 403)
        log = EmailLog.objects.get()
        resp = c.get(reverse('admin:komute_emaillog_change', args=[log.id]))
        self.assertNotContains(resp, 'name="status"')

    def test_successful_sends_leave_no_record(self):
        # The job arguments are the whole email, so successes aren't kept.
        from django_q.models import Task
        from komute.views import _send_booking_request_email
        _send_booking_request_email(self.booking)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(Task.objects.count(), 0)

    def test_purge_task_log_drops_old_failures(self):
        from django_q.models import Task
        now = timezone.now()
        old = now - timedelta(days=settings.TASK_LOG_RETENTION_DAYS + 1)
        for tid, when in (('old', old), ('new', now)):
            Task.objects.create(id=tid, name=tid, func='komute.tasks.send_email',
                                started=when, stopped=when, success=False)
        for subject in ('old', 'new'):
            EmailLog.objects.create(kind=EmailLog.KIND_WELCOME, to_masked='x***@example.com',
                                    subject=subject, status=EmailLog.STATUS_SENT)
        EmailLog.objects.filter(subject='old').update(created_at=old)
        call_command('purge_task_log', stdout=io.StringIO())
        self.assertEqual(list(Task.objects.values_list('id', flat=True)), ['new'])
        self.assertEqual(list(EmailLog.objects.values_list('subject', flat=True)), ['new'])

    def test_accept_queues_confirmation_emails(self):
        c = Client()
        c.login(username='eq_driver@example.com', password='TestPass123')
        resp = c.post(reverse('accept-booking', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                         ['eq_driver@example.com', 'eq_rider@example.com'])


class SelfHostedFontsTests(TestCase):
    """Audit F-7: fonts come from static/, not Google."""

    def test_pages_link_local_fonts_not_google(self):
        for name in ('home', 'login', 'signup', 'about', 'privacy-policy', 'contact-us'):
            resp = Client().get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            self.assertContains(resp, 'css/fonts.css', msg_prefix=name)
            self.assertNotContains(resp, 'fonts.googleapis.com', msg_prefix=name)
            self.assertNotContains(resp, 'fonts.gstatic.com', msg_prefix=name)

    def test_no_template_uses_google_fonts(self):
        templates = Path(settings.BASE_DIR) / 'templates'
        hits = [p.name for p in templates.rglob('*.html')
                if 'fonts.googleapis.com' in p.read_text(encoding='utf-8')]
        self.assertEqual(hits, [])

    def test_every_font_file_in_the_stylesheet_exists(self):
        import re
        static = Path(settings.BASE_DIR) / 'static'
        css = (static / 'css' / 'fonts.css').read_text(encoding='utf-8')
        files = set(re.findall(r'url\(\.\./fonts/([\w-]+\.woff2)\)', css))
        self.assertEqual(len(files), 14)
        for name in files:
            self.assertEqual((static / 'fonts' / name).read_bytes()[:4], b'wOF2', name)
        self.assertEqual(css.count('font-display: swap;'), css.count('@font-face'))


class SiteAndEmailSubjectTests(TestCase):
    """Audit S-18: allauth mail must not say "[example.com]"."""

    def test_site_row_points_at_real_domain(self):
        from django.contrib.sites.models import Site
        site = Site.objects.get_current()
        self.assertEqual(site.domain, 'komute.net')
        self.assertEqual(site.name, 'Komüte')

    def test_allauth_subject_prefix(self):
        from allauth.account.adapter import get_adapter
        self.assertEqual(get_adapter().format_email_subject('Hello'), '[Komüte] Hello')


class TemplateAccessibilityTests(TestCase):
    """Audit A-1 and A-2: controls work from the keyboard and labels name their fields."""

    def _templates(self):
        import re
        root = Path(settings.BASE_DIR) / 'templates'
        for p in sorted(root.rglob('*.html')):
            # Blank out comments but keep their newlines so line numbers stay right.
            yield p.name, re.sub(r'<!--.*?-->', lambda m: '\n' * m.group().count('\n'),
                                 p.read_text(encoding='utf-8'), flags=re.S)

    def test_every_label_is_linked_to_a_control(self):
        import re
        bad = []
        for name, html in self._templates():
            for m in re.finditer(r'<label\b([^>]*)>(.*?)</label>', html, re.S):
                if 'for=' in m.group(1) or re.search(r'<(input|select|textarea)\b', m.group(2)):
                    continue
                bad.append(f'{name}:{html.count(chr(10), 0, m.start()) + 1}')
        self.assertEqual(bad, [])

    def test_every_label_for_points_at_an_id(self):
        import re
        bad = []
        for name, html in self._templates():
            ids = set(re.findall(r'\bid="([^"]+)"', html))
            bad += [f'{name}: {f}' for f in re.findall(r'\bfor="([^"]+)"', html) if f not in ids]
        self.assertEqual(bad, [])

    def test_no_click_only_divs(self):
        # Backdrops (close on outside click), stopPropagation wrappers, the
        # inbox cards (whose name is a real link) and the vehicle photo zone
        # (which holds a real "Click to upload" button) are the only allowed ones.
        import re
        allowed = ('event.stopPropagation()', 'event.target===this', 'khCloseDrawer()', 'openChat(',
                   ".vehicle-img-file').click()")
        bad = []
        for name, html in self._templates():
            for m in re.finditer(r'<(div|span|li|td|tr|label|p|img)\b[^>]*\bonclick="([^"]*)"', html, re.S):
                if not any(a in m.group(2) for a in allowed):
                    bad.append(f'{name}:{html.count(chr(10), 0, m.start()) + 1}')
        self.assertEqual(bad, [])

    def test_inbox_cards_link_to_the_chat(self):
        for name in ('driver-inbox.html', 'rider-inbox.html'):
            html = dict(self._templates())[name]
            self.assertIn('<a class="convo-link" href="{% url \'chat\' convo.chat_token %}">', html, name)

    def test_icon_only_buttons_have_names(self):
        # Audit A-3. connBtn gets its text from JS.
        import re
        bad = []
        for name, html in self._templates():
            for m in re.finditer(r'<(button|a)\b([^>]*)>(.*?)</\1>', html, re.S):
                attrs, inner = m.group(2), m.group(3)
                if 'aria-label' in attrs or 'id="connBtn"' in attrs:
                    continue
                text = re.sub(r'<svg.*?</svg>|<[^>]+>|&[a-z#0-9]+;|[\s×✕←→+−-]', '', inner, flags=re.S)
                if not text:
                    bad.append(f'{name}:{html.count(chr(10), 0, m.start()) + 1}')
        self.assertEqual(bad, [])

    def test_reduced_motion_everywhere(self):
        # Audit A-4: base.css (linked on every page that loads fonts.css) turns
        # animations off, and the JS confetti checks the setting itself.
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'base.css').read_text(encoding='utf-8')
        self.assertIn('@media (prefers-reduced-motion: reduce)', css)
        missing = [n for n, html in self._templates() if 'css/fonts.css' in html and 'css/base.css' not in html]
        self.assertEqual(missing, [])
        self.assertIn("matchMedia('(prefers-reduced-motion: reduce)')", dict(self._templates())['ride-success.html'])

    def test_every_dialog_gets_keyboard_handling(self):
        # Audit A-5: static/js/dialog.js handles everything marked data-dialog.
        import re
        bad = []
        for name, html in self._templates():
            for m in re.finditer(r'<[a-z]+\b[^>]*role="dialog"[^>]*>', html):
                if 'data-dialog' not in m.group(0) or 'aria-modal="true"' not in m.group(0):
                    bad.append(f'{name}: {m.group(0)[:60]}')
        self.assertEqual(bad, [])
        templates = dict(self._templates())
        for name in ('header.html', 'report-block.html'):
            self.assertIn("js/dialog.js", templates[name], name)
        profile = templates['profile.html']
        for modal in ('vehicleDeleteModal', 'emailModal', 'passwordModal', 'deleteAccountModal',
                      'cameraModal', 'verifyModal', 'verifySelfieModal'):
            self.assertRegex(profile, rf'id="{modal}" data-dialog role="dialog"', modal)

    def test_no_light_grey_text(self):
        # Audit A-6: these greys are under 3:1 on white.
        import re
        bad = []
        for name, html in self._templates():
            for m in re.finditer(r'(?<![-\w])color:\s*#(9ca3af|94a3b8|9db8a2)\b', html, re.I):
                bad.append(f'{name}:{html.count(chr(10), 0, m.start()) + 1}')
        self.assertEqual(bad, [])

    def test_accessibility_statement(self):
        # Audit A-7.
        resp = Client().get(reverse('accessibility'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'partially conformant')
        self.assertContains(resp, 'support@komute.net')
        self.assertContains(Client().get(reverse('home')), reverse('accessibility'))


class PrivacyPolicyProcessorTests(TestCase):
    """Audit P-7: the Privacy Policy names every outside service we use."""

    # Every outside host the CSP allows, and the name the policy gives it.
    # Adding a host to the CSP fails test_every_csp_host_is_named until the
    # policy (section 3) and this map are updated.
    CSP_HOSTS = {
        'unpkg.com': 'unpkg',
        'tile.openstreetmap.org': 'OpenStreetMap Foundation',
        '*.tile.openstreetmap.org': 'OpenStreetMap Foundation',
        'images.unsplash.com': 'Unsplash',
        'photon.komoot.io': 'Photon',
        'router.project-osrm.org': 'OSRM',
        # form-action only: the sign-in buttons redirect there.
        'accounts.google.com': 'Google',
        'appleid.apple.com': 'Apple',
    }

    def setUp(self):
        self.html = Client().get(reverse('privacy-policy')).content.decode()

    def test_every_csp_host_is_named(self):
        import re
        from komute.middleware import CONTENT_SECURITY_POLICY
        hosts = set(re.findall(r'https://([\w.*-]+)', CONTENT_SECURITY_POLICY))
        self.assertEqual(hosts - set(self.CSP_HOSTS), set(), 'CSP host missing from CSP_HOSTS')
        for host in hosts:
            self.assertIn(self.CSP_HOSTS[host], self.html, host)

    def test_server_side_providers_are_named(self):
        for name in ('DigitalOcean', 'Amazon Web Services, Simple Email Service'):
            self.assertIn(name, self.html)

    def test_verification_is_described_as_in_house(self):
        self.assertIn("Driver verification runs on KOMUTE's own servers", self.html)
        self.assertNotIn('trusted third-party services', self.html)
        self.assertNotIn('verification service provider', self.html)


class LegalTextAccuracyTests(TestCase):
    """Audit P-16: the legal pages don't promise things the site doesn't do."""

    def test_no_analytics_or_mobile_app_claims(self):
        import re
        for name in ('privacy-policy', 'terms-condition', 'signup', 'how-it-works', 'community-guidelines', 'delete-account'):
            html = re.sub(r'<!--.*?-->', '', Client().get(reverse(name)).content.decode(), flags=re.S)
            for claim in ('analyze usage', 'Analyze trends',
                          'mobile app', 'through the app', 'use the app'):
                self.assertNotIn(claim, html, f'{claim!r} on {name}')

    def test_google_and_apple_sign_in_are_described(self):
        # Sign in with Google / Apple (komute/social.py), in both legal texts.
        terms = 'or with your Google or Apple account'
        self.assertContains(Client().get(reverse('terms-condition')), terms)
        self.assertContains(Client().get(reverse('signup')), terms)
        policy = Client().get(reverse('privacy-policy'))
        self.assertContains(policy, 'Google or Apple sign-in:')
        self.assertContains(policy, "We don't receive your Google or Apple password")
        self.assertContains(policy, 'Google or Apple sign-in link:')

    def test_cookie_section_matches_the_site(self):
        html = Client().get(reverse('privacy-policy')).content.decode()
        self.assertIn("We don't use analytics, advertising or tracking cookies.", html)

    def test_last_updated_matches_the_versions(self):
        for name, version in (('privacy-policy', settings.PRIVACY_VERSION), ('terms-condition', settings.TERMS_VERSION)):
            d = date.fromisoformat(version)
            self.assertContains(Client().get(reverse(name)), f'{d:%B} {d.day}, {d.year}', msg_prefix=name)


class SearchEngineFilesTests(TestCase):
    """robots.txt, sitemap.xml and a meta description on every public page."""

    def test_robots_txt_points_at_the_sitemap_and_hides_private_areas(self):
        resp = Client().get('/robots.txt')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/plain')
        body = resp.content.decode()
        self.assertIn('Sitemap: https://komute.net/sitemap.xml', body)
        self.assertIn('Disallow: /profile/', body)
        self.assertNotIn(settings.ADMIN_URL.strip('/'), body)

    def test_sitemap_lists_only_public_pages(self):
        from komute.sitemaps import PUBLIC_PAGES
        resp = Client().get('/sitemap.xml')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        for name in PUBLIC_PAGES:
            self.assertIn(f'<loc>https://komute.net{reverse(name)}</loc>', body)
        self.assertNotIn('/profile/', body)

    def test_public_pages_open_without_login_and_have_a_description(self):
        from komute.sitemaps import PUBLIC_PAGES
        for name in PUBLIC_PAGES:
            resp = Client().get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            self.assertContains(resp, 'name="description"', count=1, msg_prefix=name)

    def test_signed_in_user_who_has_not_accepted_terms_still_gets_them(self):
        c = Client()
        c.force_login(make_user('gate@example.com'))
        self.assertEqual(c.get('/robots.txt').status_code, 200)
        self.assertEqual(c.get('/sitemap.xml').status_code, 200)


class DataExportTests(TestCase):
    """Audit P-9: "Download my data" on the profile."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.me = make_driver_with_vehicle('me@example.com')
        self.me.license_number = 'LIC-12345'
        self.me.license_province = 'ON'
        self.me.save()
        self.other = make_user('someone.else@example.com', full_name='Olive Other')
        self.mine = make_ride(self.me)
        RideBooking.objects.create(ride=self.mine, passenger=self.other, seats=1, agreed_price_cents=2500,
                                   status=RideBooking.STATUS_CONFIRMED)
        Message.objects.create(ride=self.mine, sender=self.other, receiver=self.me, body='See you at 8')
        Review.objects.create(reviewer=self.other, reviewee=self.me, rating=5, body='Great', is_anonymous=True)
        UserReport.objects.create(reporter=self.me, reported=self.other, reason='other', details='late',
                                  staff_note='internal note')
        UserReport.objects.create(reporter=self.other, reported=self.me, reason='other', details='secret reporter')
        UserBlock.objects.create(blocker=self.me, blocked=self.other)
        self.c = Client()
        self.c.force_login(self.me)

    def _export(self):
        resp = self.c.get(reverse('download-my-data'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment; filename="komute-my-data-', resp['Content-Disposition'])
        return resp, json.loads(resp.content)

    def test_export_contains_my_data(self):
        _, data = self._export()
        self.assertEqual(data['account']['email'], 'me@example.com')
        self.assertEqual(data['account']['licence_number'], 'LIC-12345')  # decrypted: it's theirs
        self.assertEqual(len(data['vehicles']), 1)
        self.assertEqual(data['rides_as_driver'][0]['bookings'][0]['passenger'], 'Olive Other')
        self.assertEqual(data['messages'][0]['text'], 'See you at 8')
        self.assertEqual(data['messages'][0]['direction'], 'received')
        self.assertEqual(data['reports_you_made'][0]['details'], 'late')
        self.assertEqual(data['users_you_blocked'][0]['name'], 'Olive Other')
        self.assertTrue(data['terms_acceptances'])

    def test_export_leaks_nothing_about_others(self):
        resp, data = self._export()
        body = resp.content.decode()
        self.assertNotIn('someone.else@example.com', body)   # names only, never emails
        self.assertEqual(data['reviews_received'][0]['from'], 'Anonymous')
        self.assertNotIn('secret reporter', body)              # reports about me stay out
        self.assertNotIn('internal note', body)                # staff notes stay out
        self.assertNotIn('password', body)

    def test_needs_login(self):
        resp = Client().get(reverse('download-my-data'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_rate_limited(self):
        limit, _ = settings.RATE_LIMITS['data-export']
        for _ in range(limit):
            self.assertEqual(self.c.get(reverse('download-my-data')).status_code, 200)
        self.assertRedirects(self.c.get(reverse('download-my-data')), reverse('profile'), fetch_redirect_response=False)

    def test_profile_and_policy_offer_it(self):
        self.assertContains(self.c.get(reverse('profile')), reverse('download-my-data'))
        self.assertContains(Client().get(reverse('privacy-policy')), 'Download my data</strong> on your profile page')


class SocialSignInTests(TestCase):
    """Sign in with Google / Apple (komute/social.py). The provider round trip
    is skipped: each test hands allauth the profile Google or Apple would
    send back, as their callback views do."""

    GOOGLE = {'sub': 'g-123', 'email': 'new.user@gmail.com', 'email_verified': True,
              'name': 'New User', 'given_name': 'New', 'family_name': 'User',
              'picture': 'https://lh3.googleusercontent.com/x', 'locale': 'en'}

    def setUp(self):
        cache.clear()

    def _sign_in(self, provider_id, data, state=None):
        """Completes a sign-in as the provider's callback would, and gives
        self.client that session. Returns (response, messages)."""
        from allauth.core import context
        from allauth.socialaccount.adapter import get_adapter
        from allauth.socialaccount.helpers import complete_social_login
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory
        request = RequestFactory().get(f'/accounts/{provider_id}/login/callback/')
        SessionMiddleware(lambda r: None).process_request(request)
        request.user = AnonymousUser()
        request._messages = FallbackStorage(request)
        with context.request_context(request):
            provider = get_adapter(request).get_provider(request, provider_id)
            sociallogin = provider.sociallogin_from_response(request, data)
            sociallogin.state = state or {}
            resp = complete_social_login(request, sociallogin)
        request.session.save()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = request.session.session_key
        return resp, [str(m) for m in request._messages]

    def _finish(self, **fields):
        data = {'full_name': 'New User', 'country_code': '+1', 'phone_number': '4155552671',
                'dob': '1990-05-01', 'agreed_to_terms': '1', **fields}
        return self.client.post(reverse('socialaccount_signup'), data)

    def _logged_in_user(self):
        return self.client.get(reverse('profile')).wsgi_request.user

    def test_buttons_post_to_the_providers(self):
        for name in ('login', 'signup'):
            html = Client().get(reverse(name)).content.decode()
            self.assertIn('action="/accounts/google/login/', html, name)
            self.assertIn('action="/accounts/apple/login/', html, name)
            self.assertIn('Continue with Google', html, name)

    @override_settings(SOCIALACCOUNT_PROVIDERS={})
    def test_no_buttons_without_keys(self):
        for name in ('login', 'signup'):
            html = Client().get(reverse(name)).content.decode()
            self.assertNotIn('/accounts/google/login/', html, name)
            self.assertNotIn('or continue with', html, name)

    def test_start_sends_the_registered_callback_url(self):
        resp = Client().post('/accounts/google/login/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith('https://accounts.google.com/'))
        self.assertIn('redirect_uri=http%3A%2F%2Ftestserver%2Faccounts%2Fgoogle%2Flogin%2Fcallback%2F',
                      resp['Location'])
        # allauth refuses to start on a GET (login CSRF).
        self.assertNotEqual(Client().get('/accounts/google/login/').status_code, 302)

    def test_csp_allows_the_redirect_to_the_providers(self):
        policy = Client().get(reverse('login'))['Content-Security-Policy-Report-Only']
        self.assertIn("form-action 'self' https://accounts.google.com https://appleid.apple.com", policy)

    def test_new_user_finishes_signup_with_phone_dob_and_terms(self):
        from allauth.socialaccount.models import SocialAccount
        resp, _ = self._sign_in('google', self.GOOGLE)
        self.assertRedirects(resp, reverse('socialaccount_signup'), fetch_redirect_response=False)
        self.assertFalse(CustomUser.objects.filter(email='new.user@gmail.com').exists())

        page = self.client.get(reverse('socialaccount_signup'))
        self.assertContains(page, 'Signed in with Google')
        self.assertContains(page, 'value="new.user@gmail.com"')
        self.assertContains(page, 'value="New User"')
        self.assertNotContains(page, 'name="password"')

        resp = self._finish(email='attacker@example.com')
        self.assertRedirects(resp, reverse('role-select'), fetch_redirect_response=False)
        user = CustomUser.objects.get(email='new.user@gmail.com')
        self.assertEqual(user.username, 'new.user@gmail.com')
        self.assertEqual(user.phone, '+14155552671')
        self.assertEqual(user.dob, date(1990, 5, 1))
        self.assertTrue(user.email_verified)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(CustomUser.objects.filter(email='attacker@example.com').exists())
        self.assertTrue(LegalAcceptance.objects.filter(
            user=user, terms_version=settings.TERMS_VERSION, privacy_version=settings.PRIVACY_VERSION,
            source=LegalAcceptance.SOURCE_SIGNUP).exists())
        account = SocialAccount.objects.get(user=user, provider='google', uid='g-123')
        self.assertEqual(set(account.extra_data), {'sub', 'email', 'email_verified'})
        self.assertEqual([m.to for m in mail.outbox], [['new.user@gmail.com']])
        self.assertEqual(self._logged_in_user(), user)

    def test_new_user_picks_a_role_even_when_sent_elsewhere(self):
        # Started from /login/?next=/profile/: allauth carries `next` in the state.
        resp, _ = self._sign_in('google', self.GOOGLE, state={'next': '/profile/'})
        self.assertRedirects(resp, reverse('socialaccount_signup'), fetch_redirect_response=False)
        resp = self._finish()
        self.assertRedirects(resp, f"{reverse('role-select')}?next=/profile/", fetch_redirect_response=False)

    def test_finish_signup_applies_the_signup_rules(self):
        self._sign_in('google', self.GOOGLE)
        too_young = (date.today() - timedelta(days=17 * 365)).isoformat()
        resp = self._finish(dob=too_young, phone_number='12ab', agreed_to_terms='')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'You must be at least 18 years old.')
        self.assertContains(resp, 'Enter a valid phone number (digits only).')
        self.assertContains(resp, 'You must accept the Terms')
        self.assertFalse(CustomUser.objects.filter(email='new.user@gmail.com').exists())

    def test_apple_name_and_hidden_email(self):
        resp, _ = self._sign_in('apple', {
            'sub': '001234.abcd', 'email': 'x7k2@privaterelay.appleid.com', 'email_verified': 'true',
            'is_private_email': 'true', 'name': {'firstName': 'Ada', 'lastName': 'Lovelace'}})
        self.assertRedirects(resp, reverse('socialaccount_signup'), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('socialaccount_signup')), 'value="Ada Lovelace"')
        self._finish(full_name='Ada Lovelace')
        user = CustomUser.objects.get(email='x7k2@privaterelay.appleid.com')
        self.assertEqual(user.full_name, 'Ada Lovelace')
        self.assertTrue(user.email_verified)

    def test_returning_user_signs_straight_in(self):
        self._sign_in('google', self.GOOGLE)
        self._finish()
        self.client.logout()
        resp, _ = self._sign_in('google', self.GOOGLE)
        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(resp['Location'], reverse('socialaccount_signup'))
        self.assertEqual(self._logged_in_user().email, 'new.user@gmail.com')

    def test_existing_verified_account_is_linked_and_keeps_its_password(self):
        from allauth.socialaccount.models import SocialAccount
        # Verified by the old flow, with no allauth EmailAddress row.
        user = make_user('new.user@gmail.com', email_verified=True)
        resp, _ = self._sign_in('google', self.GOOGLE)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._logged_in_user(), user)
        user.refresh_from_db()
        self.assertTrue(user.check_password('TestPass123'))
        self.assertTrue(SocialAccount.objects.filter(user=user, provider='google').exists())

    def test_unverified_account_loses_the_password_it_was_created_with(self):
        # Someone signed up with this address but never proved they own it.
        user = make_user('new.user@gmail.com', email_verified=False)
        self._sign_in('google', self.GOOGLE)
        self.assertEqual(self._logged_in_user(), user)
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.email_verified)

    def test_unverified_provider_email_does_not_take_over_an_account(self):
        user = make_user('new.user@gmail.com')
        resp, _ = self._sign_in('google', {**self.GOOGLE, 'email_verified': False})
        self.assertRedirects(resp, reverse('socialaccount_signup'), fetch_redirect_response=False)
        resp = self._finish()
        self.assertContains(resp, 'An account already uses this email address.')
        self.assertFalse(self._logged_in_user().is_authenticated)
        self.assertFalse(user.socialaccount_set.exists())

    def test_closed_account_cannot_sign_in(self):
        make_user('new.user@gmail.com', is_active=False, deactivated_at=timezone.now())
        resp, msgs = self._sign_in('google', self.GOOGLE)
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)
        self.assertIn('This account is closed or deactivated.', msgs[0])
        self.assertFalse(self._logged_in_user().is_authenticated)

    def test_cancel_at_google_returns_to_login(self):
        from urllib.parse import parse_qs, urlsplit
        start = self.client.post('/accounts/google/login/')
        state = parse_qs(urlsplit(start['Location']).query)['state'][0]
        resp = self.client.get(f'/accounts/google/login/callback/?error=access_denied&state={state}')
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('login')), 'Sign-in with Google was cancelled.')

    def test_failed_sign_in_returns_to_login(self):
        resp = self.client.get('/accounts/google/login/callback/?code=x&state=unknown')
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('login')), "Sign-in with Google didn&#x27;t work.")

    def test_allauth_social_pages_are_not_reachable(self):
        self.assertRedirects(self.client.get('/accounts/3rdparty/'), reverse('profile'),
                             fetch_redirect_response=False)
        # No sign-in in progress: allauth's login URL, which redirects to ours.
        self.assertRedirects(self.client.get(reverse('socialaccount_signup')), reverse('account_login'),
                             fetch_redirect_response=False)

    def test_export_and_purge_cover_the_sign_in_link(self):
        from allauth.socialaccount.models import SocialAccount
        self._sign_in('google', self.GOOGLE)
        self._finish()
        user = CustomUser.objects.get(email='new.user@gmail.com')
        export = json.loads(self.client.get(reverse('download-my-data')).content)
        self.assertEqual(export['sign_in_with'][0]['provider'], 'google')
        self.assertNotIn('picture', export['sign_in_with'][0]['details'])
        CustomUser.objects.filter(pk=user.pk).update(
            is_active=False, deactivated_at=timezone.now() - timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS + 1))
        call_command('purge_deactivated_accounts')
        self.assertFalse(SocialAccount.objects.filter(uid='g-123').exists())


class AppleContactEmailTests(TestCase):
    """Apple "Hide My Email" accounts can give a real address for our emails,
    confirmed through an emailed link (komute/contact_email.py)."""

    RELAY = 'x7k2@privaterelay.appleid.com'
    APPLE = {'sub': '001234.abcd', 'email': RELAY, 'email_verified': 'true',
             'is_private_email': 'true', 'name': {'firstName': 'Ada', 'lastName': 'Lovelace'}}

    _sign_in = SocialSignInTests._sign_in
    _finish = SocialSignInTests._finish

    def setUp(self):
        cache.clear()

    def _relay_user(self, contact=None, confirmed=False):
        user = make_user(self.RELAY, contact_email=contact, contact_email_confirmed=confirmed)
        self.client.force_login(user)
        return user

    def _confirm_url(self, user):
        from komute.contact_email import make_token
        return reverse('confirm-contact-email', args=[make_token(user)])

    def test_finish_signup_asks_only_when_apple_hid_the_email(self):
        self._sign_in('apple', self.APPLE)
        page = self.client.get(reverse('socialaccount_signup'))
        self.assertContains(page, 'name="contact_email"')
        self.assertContains(page, 'This will be the main email we use to reach you')

        self.client = Client()
        self._sign_in('google', SocialSignInTests.GOOGLE)
        self.assertNotContains(self.client.get(reverse('socialaccount_signup')), 'name="contact_email"')

    def test_signup_saves_it_unconfirmed_and_emails_the_link(self):
        self._sign_in('apple', self.APPLE)
        resp = self._finish(full_name='Ada Lovelace', contact_email=' Ada@Example.com ')
        self.assertRedirects(resp, reverse('role-select'), fetch_redirect_response=False)
        user = CustomUser.objects.get(email=self.RELAY)
        self.assertEqual(user.contact_email, 'ada@example.com')
        self.assertFalse(user.contact_email_confirmed)
        # Not used until confirmed: the welcome email still goes through Apple.
        self.assertEqual(user.mail_to, self.RELAY)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['ada@example.com', self.RELAY])
        link = next(m for m in mail.outbox if m.to == ['ada@example.com'])
        self.assertIn('/contact-email/confirm/', link.body)

    def test_signup_without_it_is_fine(self):
        self._sign_in('apple', self.APPLE)
        self._finish(full_name='Ada Lovelace')
        user = CustomUser.objects.get(email=self.RELAY)
        self.assertIsNone(user.contact_email)
        self.assertEqual([m.to for m in mail.outbox], [[self.RELAY]])

    def test_signup_rejects_a_bad_or_relay_address(self):
        self._sign_in('apple', self.APPLE)
        resp = self._finish(full_name='Ada Lovelace', contact_email='not-an-email')
        self.assertContains(resp, 'Enter a valid email address.')
        resp = self._finish(full_name='Ada Lovelace', contact_email='abc@privaterelay.appleid.com')
        self.assertContains(resp, 'not an Apple relay address')
        self.assertFalse(CustomUser.objects.filter(email=self.RELAY).exists())

    def test_link_confirms_only_on_post(self):
        user = self._relay_user('ada@example.com')
        url = self._confirm_url(user)
        self.assertContains(Client().get(url), 'Confirm Your Communication Email')
        user.refresh_from_db()
        self.assertFalse(user.contact_email_confirmed)

        self.assertContains(Client().post(url), 'Email Confirmed')
        user.refresh_from_db()
        self.assertTrue(user.contact_email_confirmed)
        self.assertEqual(user.mail_to, 'ada@example.com')

    def test_confirmed_address_gets_our_emails(self):
        self._relay_user('ada@example.com', confirmed=True)
        resp = self.client.post(reverse('request-delete-account'))
        self.assertIn('ada@example.com', resp.json()['message'])
        self.assertEqual(mail.outbox[-1].to, ['ada@example.com'])

    def test_old_or_tampered_links_do_nothing(self):
        user = self._relay_user('old@example.com')
        old_url = self._confirm_url(user)
        user.contact_email = 'new@example.com'
        user.save()
        self.assertContains(Client().post(old_url), 'Link Expired')
        self.assertContains(Client().post(old_url[:-4] + 'abc/'), 'Link Expired')
        user.refresh_from_db()
        self.assertFalse(user.contact_email_confirmed)

    def test_never_used_to_sign_in_or_reset_a_password(self):
        self._relay_user('ada@example.com', confirmed=True)
        self.assertFalse(Client().login(username='ada@example.com', password='TestPass123'))
        mail.outbox.clear()
        Client().post(reverse('forgot-password'), {'email': 'ada@example.com'})
        self.assertEqual(mail.outbox, [])
        # Two accounts may share one.
        make_user('other@example.com', contact_email='ada@example.com')
        self.assertEqual(CustomUser.objects.filter(contact_email='ada@example.com').count(), 2)

    def test_profile_shows_the_communication_email(self):
        self._relay_user('ada@example.com')
        page = self.client.get(reverse('profile'))
        self.assertContains(page, 'Communication Email')
        self.assertContains(page, 'value="ada@example.com"')
        self.assertContains(page, 'Resend link')
        self.assertContains(page, f'You sign in with Apple ({self.RELAY})')
        self.assertNotContains(page, 'id="emailInput"')

        CustomUser.objects.filter(email=self.RELAY).update(contact_email_confirmed=True)
        page = self.client.get(reverse('profile'))
        self.assertContains(page, 'This is the main email we use to reach you.')
        self.assertNotContains(page, 'Resend link')

    def test_profile_unchanged_for_other_accounts(self):
        self.client.force_login(make_user('plain@example.com'))
        page = self.client.get(reverse('profile'))
        self.assertContains(page, 'id="emailInput"')
        self.assertNotContains(page, 'Communication Email')
        resp = self.client.post(reverse('set-contact-email'), {'contact_email': 'x@example.com'})
        self.assertEqual(resp.status_code, 400)

    def test_profile_add_change_resend_and_remove(self):
        user = self._relay_user()
        url = reverse('set-contact-email')
        resp = self.client.post(url, {'contact_email': 'Ada@Example.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mail.outbox[-1].to, ['ada@example.com'])
        user.refresh_from_db()
        self.assertEqual((user.contact_email, user.contact_email_confirmed), ('ada@example.com', False))

        # Resend: same address, another email.
        self.assertEqual(self.client.post(url, {'contact_email': 'ada@example.com'}).status_code, 200)
        self.assertEqual(len(mail.outbox), 2)

        # A change goes back to unconfirmed, so mail returns to the relay.
        CustomUser.objects.filter(pk=user.pk).update(contact_email_confirmed=True)
        self.assertEqual(self.client.post(url, {'contact_email': 'ada@example.com'}).status_code, 400)
        self.client.post(url, {'contact_email': 'new@example.com'})
        user.refresh_from_db()
        self.assertEqual((user.contact_email, user.mail_to), ('new@example.com', self.RELAY))

        self.client.post(url, {'contact_email': ''})
        user.refresh_from_db()
        self.assertIsNone(user.contact_email)

    def test_rate_limited(self):
        self._relay_user()
        limit, _ = settings.RATE_LIMITS['email-confirm-target']
        for _ in range(limit):
            self.client.post(reverse('set-contact-email'), {'contact_email': 'ada@example.com'})
        resp = self.client.post(reverse('set-contact-email'), {'contact_email': 'ada@example.com'})
        self.assertEqual(resp.status_code, 429)

    def test_in_data_export_and_cleared_by_purge(self):
        user = self._relay_user('ada@example.com', confirmed=True)
        export = json.loads(self.client.get(reverse('download-my-data')).content)
        self.assertEqual(export['account']['communication_email'], 'ada@example.com')
        CustomUser.objects.filter(pk=user.pk).update(
            is_active=False, deactivated_at=timezone.now() - timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS + 1))
        call_command('purge_deactivated_accounts')
        user.refresh_from_db()
        self.assertIsNone(user.contact_email)


class ProfileInputLimitTests(TestCase):
    """The profile page refuses a gender outside the dropdown's choices
    (ride gender preferences match on those exact values) and vehicle values
    that don't fit their database columns, which used to end in a server
    error. The same rules apply in the API (komute.services)."""

    def setUp(self):
        self.driver = make_user('limits_driver@example.com', role='driver')
        self.client.login(username='limits_driver@example.com', password='TestPass123')

    def _post(self, **overrides):
        payload = {'full_name': 'Limits Driver', 'dob': '1990-01-01', 'role': 'driver',
                   'vehicle_make': ['Toyota'], 'vehicle_model': ['Corolla'], 'vehicle_year': ['2020'],
                   'vehicle_color': ['Blue'], 'vehicle_plate': ['LIM001']}
        payload.update(overrides)
        return self.client.post(reverse('profile'), payload)

    def test_gender_must_be_a_dropdown_choice(self):
        resp = self._post(gender='robot')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['errors']['gender'], 'Please select a valid gender.')
        self.assertContains(resp, 'Please select a valid gender.')
        self.driver.refresh_from_db()
        self.assertIsNone(self.driver.gender)
        for ok in ('female', 'male', 'other', ''):
            self.assertRedirects(self._post(gender=ok), reverse('profile'))
            self.driver.refresh_from_db()
            self.assertEqual(self.driver.gender, ok or None)

    def test_vehicle_text_must_fit(self):
        cases = [
            ({'vehicle_make': ['M' * 101]}, 'Vehicle 1: Make can be at most 100 characters.'),
            ({'vehicle_model': ['M' * 101]}, 'Vehicle 1: Model can be at most 100 characters.'),
            ({'vehicle_color': ['C' * 51]}, 'Vehicle 1: Color can be at most 50 characters.'),
            ({'vehicle_plate': ['P' * 21]}, 'Vehicle 1: Plate can be at most 20 characters.'),
        ]
        for overrides, message in cases:
            resp = self._post(**overrides)
            self.assertEqual(resp.status_code, 200, overrides)
            self.assertEqual(resp.context['errors']['vehicles'], message)
        self.assertFalse(Vehicle.objects.filter(owner=self.driver).exists())

    def test_vehicle_year_range(self):
        next_year = timezone.localdate().year + 1
        for bad in ('99999999999', '1899', str(next_year + 1)):
            resp = self._post(vehicle_year=[bad])
            self.assertEqual(resp.context['errors']['vehicles'],
                             f'Vehicle 1: Year must be between 1900 and {next_year}.', bad)
        self.assertFalse(Vehicle.objects.filter(owner=self.driver).exists())
        self.assertRedirects(self._post(vehicle_year=[str(next_year)]), reverse('profile'))
        self.assertEqual(Vehicle.objects.get(owner=self.driver).year, next_year)

    def test_error_names_the_row(self):
        resp = self._post(vehicle_make=['Toyota', 'Honda'], vehicle_model=['Corolla', 'Civic'],
                          vehicle_year=['2020', '2019'], vehicle_color=['Blue', 'R' * 51],
                          vehicle_plate=['LIM001', 'LIM002'])
        self.assertEqual(resp.context['errors']['vehicles'], 'Vehicle 2: Color can be at most 50 characters.')

    def test_unchanged_for_valid_rows(self):
        self.assertRedirects(self._post(), reverse('profile'))
        vehicle = Vehicle.objects.get(owner=self.driver)
        self.assertEqual((vehicle.make, vehicle.plate, vehicle.year), ('Toyota', 'LIM001', 2020))

    def test_bio_limit_matches_the_box(self):
        resp = self._post(bio='b' * 281)
        self.assertEqual(resp.context['errors']['bio'], 'Self description can be at most 280 characters.')
        self.assertContains(resp, 'Self description can be at most 280 characters.')
        # The box counts a line break as one character; the form sends two.
        with_breaks = ('a' * 69 + '\r\n') * 4  # 280 as the box counts it, 284 as sent
        self.assertRedirects(self._post(bio=with_breaks), reverse('profile'))
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.bio, with_breaks.strip())


class ReviewFixesTests(TestCase):
    """Review page and submit fixes (2026-11-10): the page opens only for a
    trip that happened, a malformed request is refused instead of a 500, the
    rating is whole stars, tags, detailed ratings and the comment are what
    the page offers, and the page carries the opaque id."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('rf_driver@example.com')
        self.rider = make_user('rf_rider@example.com')
        self.ride = make_ride(self.driver)
        Rides.objects.filter(pk=self.ride.pk).update(status='completed')
        self.booking = RideBooking.objects.create(ride=self.ride, passenger=self.rider, seats=1,
                                                  agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED)
        self.client.login(username='rf_rider@example.com', password='TestPass123')

    def _submit(self, **overrides):
        payload = {'booking_id': encode_id(self.booking.id), 'reviewee_id': encode_id(self.driver.id), 'rating': 5}
        payload.update(overrides)
        return self.client.post(reverse('submit-review'), data=json.dumps(payload),
                                content_type='application/json')

    def test_page_only_for_a_trip_that_happened(self):
        self.assertEqual(self.client.get(reverse('leave-review', args=[encode_id(self.booking.id)])).status_code,
                         200)
        for status in (RideBooking.STATUS_CANCELLED, RideBooking.STATUS_REJECTED, RideBooking.STATUS_DRAFT,
                       RideBooking.STATUS_REQUESTED):
            RideBooking.objects.filter(pk=self.booking.pk).update(status=status)
            resp = self.client.get(reverse('leave-review', args=[encode_id(self.booking.id)]))
            self.assertRedirects(resp, reverse('my-rides'), fetch_redirect_response=False)

    def test_page_carries_the_opaque_id_and_submit_takes_it(self):
        resp = self.client.get(reverse('leave-review', args=[encode_id(self.booking.id)]))
        self.assertEqual(resp.context['reviewee_id'], encode_id(self.driver.id))
        self.assertNotIn(f'revieweeId: "{self.driver.id}"', resp.content.decode())
        self.assertEqual(self._submit().status_code, 200)
        self.assertEqual(Review.objects.get().reviewee, self.driver)

    def test_old_pages_with_the_database_id_still_work(self):
        self.assertEqual(self._submit(reviewee_id=self.driver.id).status_code, 200)
        self.assertEqual(self._submit(reviewee_id=str(self.driver.id)).status_code, 400)  # already reviewed

    def test_malformed_requests_are_refused_not_500(self):
        for body in ('[]', '5', '"text"', 'null'):
            resp = self.client.post(reverse('submit-review'), data=body, content_type='application/json')
            self.assertEqual(resp.status_code, 400, body)
        self.assertEqual(self._submit(comment=42).json()['error'], 'Your comment must be text.')
        self.assertEqual(self._submit(booking_id=123).status_code, 400)
        self.assertFalse(Review.objects.exists())

    def test_whole_stars_only(self):
        for bad in (4.7, 6, True, 'five', '4.5'):
            self.assertEqual(self._submit(rating=bad).json()['error'], 'Rating must be between 1 and 5.', bad)
        # 0 counts as missing, as before.
        self.assertEqual(self._submit(rating=0).json()['error'], 'booking_id, reviewee_id, and rating are required.')
        self.assertEqual(self._submit(rating='4').status_code, 200)
        self.assertEqual(Review.objects.get().rating, 4)

    def test_tags_detailed_ratings_and_comment_as_the_page_offers(self):
        self.assertEqual(self._submit(tags=['<b>evil</b>']).json()['error'], 'Please choose tags from the list.')
        self.assertEqual(self._submit(tags='punctual').status_code, 400)
        self.assertEqual(self._submit(subratings={'vibes': 5}).status_code, 400)
        self.assertEqual(self._submit(subratings={'punctuality': 9}).status_code, 400)
        self.assertEqual(self._submit(comment='c' * 501).json()['error'],
                         'Please keep your comment under 500 characters.')
        self.assertFalse(Review.objects.exists())
        resp = self._submit(comment='  ' + 'c' * 500 + '  ', tags=['punctual', 'punctual', 'safe-driver'],
                            subratings={'punctuality': 5, 'cleanliness': '4'})
        self.assertEqual(resp.status_code, 200)
        review = Review.objects.get()
        self.assertEqual((len(review.body), review.tags_json, review.subratings_json),
                         (500, ['punctual', 'safe-driver'], {'punctuality': 5, 'cleanliness': 4}))


class CarriedPassengersTests(TestCase):
    """Fix (2026-11-10): a completed ride's summary, receipt and My Rides
    earnings count only the riders the trip carried. A pre-request chat or
    an unanswered request stays as it was when the ride completes, and used
    to be counted as a passenger and in "Total received"."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('cp_driver@example.com')
        self.ride = make_ride(self.driver)
        Rides.objects.filter(pk=self.ride.pk).update(status='completed', seats_total=4, seats_available=1,
                                                     price_cents=2500)
        self.ride.refresh_from_db()
        self.carried = make_user('cp_carried@example.com', full_name='Carried Rider')
        RideBooking.objects.create(ride=self.ride, passenger=self.carried, seats=2, agreed_price_cents=5000,
                                   status=RideBooking.STATUS_COMPLETED)
        RideBooking.objects.create(ride=self.ride, passenger=make_user('cp_draft@example.com', full_name='Draft Chat'),
                                   seats=1, agreed_price_cents=2500, status=RideBooking.STATUS_DRAFT)
        RideBooking.objects.create(ride=self.ride, passenger=make_user('cp_req@example.com', full_name='Never Answered'),
                                   seats=1, agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED)
        self.client.login(username='cp_driver@example.com', password='TestPass123')

    def test_summary(self):
        resp = self.client.get(reverse('ride-summary', args=[encode_id(self.ride.id)]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['passenger_count'], 2)
        self.assertEqual(resp.context['total_contributions'], '$50.00')
        self.assertEqual([b['passenger_name'] for b in resp.context['bookings']], ['Carried Rider'])

    def test_receipt(self):
        from komute.services.receipts import build_receipt_pdf
        from komute.services.rides import summary_stats
        ride = Rides.objects.prefetch_related('stops', 'bookings__passenger', 'bookings__pickup_stop').get(
            pk=self.ride.pk)
        stats = summary_stats(ride)
        self.assertEqual((stats['passenger_count'], stats['total_cents']), (2, 5000))
        pdf, _ = build_receipt_pdf(ride)
        self.assertTrue(pdf.startswith(b'%PDF'))
        resp = self.client.get(reverse('ride-receipt-pdf', args=[encode_id(self.ride.id)]))
        self.assertEqual(resp.status_code, 200)

    def test_my_rides_earnings(self):
        resp = self.client.get(reverse('my-rides'))
        self.assertEqual(resp.context['passengers_helped'], 2)
        self.assertEqual(resp.context['total_earned'], '$50.00')


class OldNegotiationEndpointLockTests(TestCase):
    """Fix (2026-11-10): /negotiations/ follows the chat's lock while a
    request waits for the driver."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('ol_driver@example.com')
        self.rider = make_user('ol_rider@example.com')
        self.booking = RideBooking.objects.create(ride=make_ride(self.driver), passenger=self.rider, seats=1,
                                                  agreed_price_cents=2500, status=RideBooking.STATUS_REQUESTED)
        self.client.login(username='ol_rider@example.com', password='TestPass123')

    def test_locked_while_requested(self):
        resp = self.client.post(reverse('create-price-offer', args=[encode_id(self.booking.id)]),
                                {'price_cents': '2000'})
        self.assertEqual((resp.status_code, resp.json()['error']),
                         (400, 'Price negotiation opens once the driver accepts the booking request.'))
        offer = PriceOffer.objects.create(booking=self.booking, proposed_by=self.driver, price_cents=2000)
        resp = self.client.post(reverse('respond-price-offer', args=[encode_id(offer.id)]), {'action': 'accept'})
        self.assertEqual(resp.status_code, 400)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.agreed_price_cents, 2500)

    def test_open_once_confirmed(self):
        RideBooking.objects.filter(pk=self.booking.pk).update(status=RideBooking.STATUS_CONFIRMED)
        resp = self.client.post(reverse('create-price-offer', args=[encode_id(self.booking.id)]),
                                {'price_cents': '2000'})
        self.assertEqual(resp.status_code, 201)


class ChatSocketBadFrameTests(TransactionTestCase):
    """Fix (2026-11-10): frames the page wouldn't send are answered with an
    error and the socket stays open (they used to drop it)."""

    def setUp(self):
        self.driver = make_driver_with_vehicle('sb_driver@example.com')
        self.rider = make_user('sb_rider@example.com')
        self.booking = RideBooking.objects.create(ride=make_ride(self.driver), passenger=self.rider, seats=1,
                                                  agreed_price_cents=2500, status=RideBooking.STATUS_CONFIRMED)

    def test_bad_frames(self):
        token = make_chat_token(self.booking.id)

        async def run():
            comm = WebsocketCommunicator(ChatConsumer.as_asgi(), f'/ws/chat/{token}/')
            comm.scope['user'] = self.rider
            comm.scope['url_route'] = {'kwargs': {'token': token}}
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            replies = []
            for frame in ('not json', '[1, 2]', json.dumps({'type': 'message', 'body': 5}),
                          json.dumps({'type': 'price_accepted', 'offer_id': 'abc'}),
                          json.dumps({'type': 'price_accepted', 'price': 'free'}),
                          json.dumps({'type': 'ping'})):
                await comm.send_to(text_data=frame)
                replies.append(await comm.receive_json_from(timeout=5))
            await comm.disconnect()
            return replies

        with self.assertLogs('komute.chat', 'INFO'):
            replies = async_to_sync(run)()
        self.assertEqual([r.get('code', r['type']) for r in replies],
                         ['bad_request', 'bad_request', 'bad_request', 'offer_stale', 'offer_stale', 'pong'])

    def test_rider_still_chats_before_the_driver_answers(self):
        """Unchanged behaviour: a rider messages the driver in a pre-request
        chat and while the request waits for the driver."""
        for status in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED):
            Message.objects.all().delete()
            RideBooking.objects.filter(pk=self.booking.pk).update(status=status)
            token = make_chat_token(self.booking.id)

            async def run():
                rider = WebsocketCommunicator(ChatConsumer.as_asgi(), f'/ws/chat/{token}/')
                rider.scope['user'] = self.rider
                rider.scope['url_route'] = {'kwargs': {'token': token}}
                driver = WebsocketCommunicator(ChatConsumer.as_asgi(), f'/ws/chat/{token}/')
                driver.scope['user'] = self.driver
                driver.scope['url_route'] = {'kwargs': {'token': token}}
                await rider.connect()
                await driver.connect()
                await rider.send_json_to({'type': 'message', 'body': 'Is the seat still free?'})
                seen_by_driver = await driver.receive_json_from(timeout=5)
                await rider.disconnect()
                await driver.disconnect()
                return seen_by_driver

            with self.assertLogs('komute.chat', 'INFO'):
                seen = async_to_sync(run)()
            self.assertEqual((seen['type'], seen['body']), ('message', 'Is the seat still free?'), status)
            self.assertEqual(Message.objects.get().sender, self.rider, status)


class ContactFormTests(TestCase):
    """The Contact Us form (contact_view): the behaviour the app's contact
    endpoint shares (komute/services/contact.py)."""
    FORM = {'name': 'Jordan Lee', 'email': 'jordan@example.com', 'role': 'Driver',
            'topic': 'Help with a booking', 'subject': 'Seat count', 'message': 'Hello there.'}

    def setUp(self):
        cache.clear()
        mail.outbox.clear()

    def post(self, **changes):
        return self.client.post(reverse('contact-us'), {**self.FORM, **changes})

    def test_page_renders(self):
        self.assertEqual(self.client.get(reverse('contact-us')).status_code, 200)

    def test_sends_to_the_contact_inbox(self):
        response = self.post()
        self.assertEqual((response.status_code, response.json()), (200, {'success': True}))
        sent = mail.outbox[-1]
        self.assertEqual(sent.subject, '[Komüte Contact] Help with a booking: Seat count')
        self.assertEqual(sent.to, [settings.CONTACT_EMAIL])
        self.assertEqual(sent.from_email, settings.DEFAULT_FROM_EMAIL)
        self.assertEqual(sent.reply_to, ['Jordan Lee <jordan@example.com>'])
        for line in ('Name:     Jordan Lee', 'Email:    jordan@example.com', 'Role:     Driver',
                     'Topic:    Help with a booking', 'Subject:  Seat count', 'Message:\nHello there.'):
            self.assertIn(line, sent.body)

    def test_role_is_optional(self):
        self.post(role='')
        self.assertIn('Role:     —', mail.outbox[-1].body)

    def test_required_fields(self):
        for field in ('name', 'email', 'topic', 'subject', 'message'):
            response = self.post(**{field: '   '})
            self.assertEqual((response.status_code, response.json()),
                             (400, {'error': 'Please fill in all required fields.'}), field)
        self.assertEqual(mail.outbox, [])

    def test_bad_email(self):
        response = self.post(email='not-an-email')
        self.assertEqual(response.json(), {'error': 'Please enter a valid email address.'})

    def test_name_is_cleaned(self):
        response = self.post(name='\x07\x08')
        self.assertEqual((response.status_code, response.json()), (400, {'error': 'Please enter your name.'}))
        self.post(name='Jo\x00rdan ' + 'x' * 200)
        self.assertEqual(mail.outbox[-1].reply_to[0].split(' <')[0], 'Jordan ' + 'x' * 92)

    def test_line_breaks_in_the_subject_still_send(self):
        """Were a 500 "Could not send message" (the Subject header refuses
        line breaks); now they become spaces."""
        response = self.post(subject='Seat\r\ncount\x0b please', topic='Help with\na booking')
        self.assertEqual(response.status_code, 200)
        sent = mail.outbox[-1]
        self.assertEqual(sent.subject, '[Komüte Contact] Help with a booking: Seat count please')
        self.assertIn('Subject:  Seat count please\n', sent.body)
        response = self.post(subject='\x07\x0b')
        self.assertEqual(response.json(), {'error': 'Please fill in all required fields.'})
        self.assertEqual(len(mail.outbox), 1)

    def test_mail_failure(self):
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('down')), \
                self.assertLogs('komute', 'ERROR'):
            response = self.post()
        self.assertEqual((response.status_code, response.json()),
                         (500, {'error': 'Could not send message. Please try again later.'}))

    @override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'contact': (1, 3600)})
    def test_rate_limit(self):
        self.post()
        response = self.post()
        self.assertEqual((response.status_code, response.json()),
                         (429, {'error': 'Too many messages sent. Please try again later.'}))
        self.assertEqual(len(mail.outbox), 1)
