"""Module 8 (driver identity verification) tests.

    manage.py test api --settings=config.settings_regression

The rules are the website's (komute.services.verification, covered by
komute.tests_regression). The outbound call to the verification node and the
vehicle photo read are faked, as in those tests. The main point here:
photos stay in memory and nothing is stored or sent without consent."""
import base64
import io
import shutil
import tempfile
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, mock_open, patch

import requests
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import InMemoryUploadedFile, SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone
from PIL import Image

from api.tests.base import ApiTestCase, auth_client, make_user
from komute.models import BiometricConsent, Vehicle, VerificationLog
from komute.services import verification as verification_service

V = '/api/v1/verification/'
APPROVED = {"decision": "APPROVED",
            "stages": {"license_ocr": {"passed": True},
                       "face_biometrics": {"passed": True, "vlm_confidence": 0.9},
                       "vehicle_verification": {"passed": True}},
            "rejection_reasons": []}
REJECTED = {"decision": "REJECTED",
            "stages": {"license_ocr": {"passed": True},
                       "face_biometrics": {"passed": False, "vlm_confidence": 0.2},
                       "vehicle_verification": {"passed": True}},
            "rejection_reasons": ["Facial biometrics mismatch for JOHN SMITH"]}


def jpeg(name):
    buf = io.BytesIO()
    Image.new('RGB', (60, 40), 'blue').save(buf, format='JPEG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


def service_reply(body):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = body
    return resp


def error(response):
    return response.json()['error']


class VerificationTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        root = tempfile.mkdtemp(prefix='komute_api_verify_')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(PRIVATE_MEDIA_ROOT=root, PRIVATE_MEDIA_USE_ACCEL=False)
        override.enable()
        self.addCleanup(override.disable)
        self.media_root = root
        self.driver = make_user('driver@example.com', role='driver', license_number='D1234-56789',
                                license_province='ON', license_expiry=date.today() + timedelta(days=400))
        self.vehicle = Vehicle.objects.create(owner=self.driver, make='Honda', model='Civic', year=2019,
                                              color='Black', plate='VER001', photo_url='/private-media/vehicles/1/v.jpg',
                                              is_primary=True, vehicle_verified=False)
        self.client = auth_client(self.driver)
        self.client.credentials(HTTP_X_SESSION_TOKEN=self.client._credentials['HTTP_X_SESSION_TOKEN'],
                                HTTP_USER_AGENT='KomuteApp/1.0')

    def attempt(self, reply=APPROVED, side_effect=None, **fields):
        body = {'license_image': jpeg('licence.jpg'), 'license_back_image': jpeg('licence_back.jpg'),
                'selfie': jpeg('selfie.jpg'), 'biometric_consent': 'true', 'notice_version': settings.BIOMETRIC_NOTICE_VERSION, **fields}
        body = {k: v for k, v in body.items() if v is not None}
        with patch('komute.services.verification.open_stored_file', mock_open(read_data=b'vehicle-bytes')), \
                patch('requests.post', return_value=service_reply(reply), side_effect=side_effect) as post:
            response = self.client.post(V + 'attempt/', body, format='multipart')
        return response, post

    def stored_files(self):
        import os
        return [f for _, _, files in os.walk(self.media_root) for f in files]


class AttemptTests(VerificationTestCase):
    def test_approved(self):
        response, post = self.attempt()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body['verified'], body['similarity_percent'], body['decision'], body['reasons']),
                         (True, 90.0, 'APPROVED', []))
        self.assertTrue(all(body['checks'].values()))
        self.driver.refresh_from_db()
        self.vehicle.refresh_from_db()
        self.assertTrue(self.driver.driver_verified)
        self.assertTrue(self.vehicle.vehicle_verified)
        # What went to the service: the profile's licence details and the four images.
        sent = post.call_args.kwargs['json']
        self.assertEqual(sent['license_details']['license_number'], 'D1234-56789')
        self.assertEqual(base64.b64decode(sent['images']['vehicle_photo_base64']), b'vehicle-bytes')
        self.assertTrue(base64.b64decode(sent['images']['license_image_base64']).startswith(b'\xff\xd8'))
        self.assertTrue(base64.b64decode(sent['images']['license_back_base64']).startswith(b'\xff\xd8'))
        # The node needs when and under which notice the driver agreed.
        self.assertEqual(sent['consent_policy_version'], settings.BIOMETRIC_NOTICE_VERSION)
        consent_at = datetime.fromisoformat(sent['consent_at'])
        self.assertIsNotNone(consent_at.tzinfo)
        self.assertLess(abs(timezone.now() - consent_at), timedelta(minutes=1))
        log = VerificationLog.objects.get()
        self.assertEqual((log.result, log.reason_codes), (VerificationLog.RESULT_APPROVED, None))

    def test_consent_is_recorded_before_sending(self):
        self.attempt()
        consent = BiometricConsent.objects.get()
        self.assertEqual((consent.user, consent.notice_version, consent.user_agent),
                         (self.driver, settings.BIOMETRIC_NOTICE_VERSION, 'KomuteApp/1.0'))

    def test_no_consent_no_call(self):
        for fields, code in (({'biometric_consent': 'false'}, 'consent_required'),
                             ({'notice_version': '2000-01-01'}, 'notice_changed')):
            response, post = self.attempt(**fields)
            self.assertEqual((response.status_code, error(response)['code']), (400, code), fields)
            post.assert_not_called()
        self.assertFalse(BiometricConsent.objects.exists())
        self.assertFalse(VerificationLog.objects.exists())

    def test_photos_are_never_stored(self):
        self.attempt()
        self.assertEqual(self.stored_files(), [])

    @override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=10)
    def test_photos_stay_in_memory_even_when_large(self):
        """With a 10-byte limit Django would spill every upload to a temp file
        on disk; the attempt endpoint keeps them in memory."""
        seen, max_dims = [], []
        real = verification_service.resize_image

        def spy(file_obj, *args, **kwargs):
            seen.append(type(file_obj))
            max_dims.append(kwargs.get('max_dim'))
            return real(file_obj, *args, **kwargs)
        with patch('komute.services.verification.resize_image', side_effect=spy):
            response, _ = self.attempt()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(seen, [InMemoryUploadedFile] * 3)
        # Licence front, licence back, selfie. The back keeps 1600 px so the barcode stays readable.
        self.assertEqual(max_dims, [None, 1600, None])

    def test_rejected_then_review(self):
        response, _ = self.attempt(reply=REJECTED)
        body = response.json()
        self.assertEqual((body['verified'], body['can_request_review']), (False, True))
        self.assertEqual(body['reasons'], ['Facial biometrics mismatch for JOHN SMITH'])
        log = VerificationLog.objects.get()
        # Only the category is kept, never the text (it can quote the licence).
        self.assertEqual(log.reason_codes, [VerificationLog.REASON_FACE_MISMATCH])
        self.assertEqual(self.client.get(V).json()['review'], 'available')

        mail.outbox.clear()
        response = self.client.post(V + 'review/', {'comment': 'It is me, the light was bad.'}, format='json')
        self.assertEqual(response.json(), {'review': 'pending'})
        log.refresh_from_db()
        self.assertEqual((log.review_status, log.driver_comment),
                         (VerificationLog.REVIEW_PENDING, 'It is me, the light was bad.'))
        self.assertEqual(mail.outbox[-1].to, [settings.ADMIN_EMAIL])
        again = self.client.post(V + 'review/', {}, format='json')
        self.assertEqual((again.status_code, error(again)['code']), (409, 'review_pending'))

    def test_nothing_to_review(self):
        self.assertEqual(error(self.client.post(V + 'review/', {}, format='json'))['code'], 'nothing_to_review')

    def test_undecided_goes_to_a_person(self):
        response, _ = self.attempt(reply={**APPROVED, 'decision': 'NEEDS_REVIEW'})
        body = response.json()
        self.assertEqual((body['verified'], body['review_pending']), (False, True))
        self.assertEqual(VerificationLog.objects.get().review_status, VerificationLog.REVIEW_PENDING)

    def test_missing_pieces(self):
        for fields, code in (({'license_image': None}, 'license_required'),
                             ({'license_back_image': None}, 'license_back_required'),
                             ({'license_back_image': SimpleUploadedFile('b.jpg', b'not an image')},
                              'invalid_license_back_image'),
                             ({'selfie': None}, 'selfie_required'),
                             ({'selfie': SimpleUploadedFile('s.jpg', b'not an image')}, 'invalid_selfie')):
            response, post = self.attempt(**fields)
            self.assertEqual(error(response)['code'], code, fields)
            post.assert_not_called()
        cache.clear()  # the cases above used up the attempts allowed per hour
        Vehicle.objects.update(photo_url=None)
        self.assertEqual(error(self.attempt()[0])['code'], 'vehicle_photo_required')

    def test_rider_is_refused(self):
        """Only drivers verify. A rider is stopped before any consent record, photo read or call."""
        self.client = auth_client(make_user('rider@example.com', role='rider'))
        response, post = self.attempt()
        self.assertEqual((response.status_code, error(response)['code']), (403, 'not_a_driver'))
        post.assert_not_called()
        self.assertFalse(BiometricConsent.objects.exists())
        self.assertFalse(VerificationLog.objects.exists())

    def test_service_down(self):
        # Offline is told to the driver, not logged as an error (as on the website).
        response, _ = self.attempt(side_effect=requests.exceptions.ConnectionError())
        self.assertEqual((response.status_code, error(response)['code']), (503, 'service_offline'))
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.driver_verified)

    @override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'verify-driver': (1, 3600)})
    def test_rate_limit(self):
        self.attempt()
        response, post = self.attempt()
        self.assertEqual(response.status_code, 429)
        post.assert_not_called()

    def test_needs_terms(self):
        self.driver.legal_acceptances.all().delete()
        response, post = self.attempt()
        self.assertEqual(error(response)['code'], 'terms_not_accepted')
        post.assert_not_called()


class StatusAndNoticeTests(VerificationTestCase):
    def test_status(self):
        body = self.client.get(V).json()
        self.assertEqual((body['driver_verified'], body['review'], body['notice_version']),
                         (False, '', settings.BIOMETRIC_NOTICE_VERSION))

    def test_notice_is_the_website_text(self):
        body = self.client.get(V + 'notice/').json()
        self.assertEqual(body['version'], settings.BIOMETRIC_NOTICE_VERSION)
        self.assertIn('http://testserver/privacy-policy/', body['html'])
        web = Client()
        web.force_login(self.driver)
        page = web.get('/profile/').content.decode()
        normalise = ' '.join
        self.assertIn(normalise(body['html'].replace('http://testserver/privacy-policy/', '/privacy-policy/').split()),
                      normalise(page.split()))
        self.assertIn(body['agreement'], page)
