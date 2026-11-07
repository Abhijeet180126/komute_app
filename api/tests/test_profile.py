"""Module 2 (profile, vehicles, photos) tests.

    manage.py test api --settings=config.settings_regression

The rules are the profile page's (komute.services.profiles / vehicles,
covered by komute.tests_regression); these check the API applies them the
same way and keeps photos private."""
import io
import shutil
import tempfile
from datetime import date, timedelta

from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from api.tests.base import ApiTestCase, auth_client, make_user
from komute import private_media
from komute.hashid_utils import encode_id
from komute.models import Vehicle

ME = '/api/v1/users/me/'
V = '/api/v1/vehicles/'
RELAY = 'abc123@privaterelay.appleid.com'


def jpeg(name='photo.jpg', size=(40, 30)):
    buf = io.BytesIO()
    Image.new('RGB', size, 'red').save(buf, format='JPEG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


def error(response):
    return response.json()['error']


def media_path(url):
    """The stored file behind an API photo URL."""
    return url.split('/api/v1/media/', 1)[1]


class MediaRootMixin:
    """Each test gets its own private media folder."""

    def setUp(self):
        super().setUp()
        root = tempfile.mkdtemp(prefix='komute_api_media_')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(PRIVATE_MEDIA_ROOT=root, PRIVATE_MEDIA_USE_ACCEL=False)
        override.enable()
        self.addCleanup(override.disable)

    def stored(self, rel_path):
        return private_media.private_storage().exists(rel_path)


def verified_driver(email='driver@example.com', **extra):
    user = make_user(email, role='driver', driver_verified=True, driver_verified_at=timezone.now(),
                     face_match_score=0.93, license_number='D1234-56789', license_province='QC',
                     license_expiry=date.today() + timedelta(days=400), **extra)
    Vehicle.objects.create(owner=user, make='Toyota', model='Corolla', year=2020, color='Red',
                           plate='ABC123', is_primary=True, vehicle_verified=True)
    return user


class ProfileReadTests(ApiTestCase):
    def test_my_profile(self):
        user = make_user('rider@example.com', phone='+14155552671', gender='female', bio='Hi')
        body = auth_client(user).get(ME).json()
        self.assertEqual(body['id'], encode_id(user.pk))
        self.assertEqual((body['phone'], body['country_code'], body['phone_number']),
                         ('+14155552671', '+1', '4155552671'))
        self.assertEqual((body['gender'], body['bio'], body['role']), ('female', 'Hi', 'rider'))
        self.assertIsNone(body['photo_url'])
        self.assertEqual(body['vehicles'], [])
        self.assertFalse(body['driver_status']['driver_verified'])
        self.assertEqual(body['mail_to'], 'rider@example.com')

    def test_driver_status(self):
        body = auth_client(verified_driver()).get(ME).json()
        status = body['driver_status']
        self.assertTrue(status['driver_verified'])
        self.assertFalse(status['has_unverified_vehicle'])
        self.assertEqual(body['vehicles'][0]['plate'], 'ABC123')
        self.assertEqual(body['license_number'], 'D1234-56789')

    def test_needs_token_and_terms(self):
        self.assertEqual(APIClient().get(ME).status_code, 401)
        response = auth_client(make_user('new@example.com', accepted_terms=False)).get(ME)
        self.assertEqual(error(response)['code'], 'terms_not_accepted')


class ProfileUpdateTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com', phone='+14155552671')
        self.client = auth_client(self.user)

    def test_change_only_what_is_sent(self):
        response = self.client.patch(ME, {'bio': 'Commuting to Laval', 'gender': 'other'})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['warnings'], [])
        self.assertEqual((body['profile']['bio'], body['profile']['gender']), ('Commuting to Laval', 'other'))
        self.user.refresh_from_db()
        self.assertEqual((self.user.phone, self.user.full_name), ('+14155552671', 'Test rider@example.com'))

    def test_profile_page_messages(self):
        cases = [
            ({'full_name': ''}, 'full_name', 'Full name is required.'),
            ({'dob': 'not-a-date'}, 'dob', 'Invalid date of birth.'),
            ({'dob': ''}, 'dob', 'Date of birth is required.'),
            ({'country_code': '+1', 'phone_number': ''}, 'phone_number', 'Phone number is required.'),
            ({'country_code': '+999'}, 'country_code', 'Please select a valid country code.'),
            ({'role': 'admin'}, 'role', 'Invalid role selected.'),
            ({'license_expiry': 'soon'}, 'license_expiry', 'Invalid license expiry date.'),
        ]
        for body, field, message in cases:
            response = self.client.patch(ME, body)
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(error(response)['fields'][field], [message], body)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, 'rider')

    def test_gender_must_be_one_of_the_choices(self):
        self.assertEqual(self.client.patch(ME, {'gender': 'robot'}).status_code, 400)
        self.assertEqual(self.client.patch(ME, {'gender': ''}).status_code, 200)

    def test_bio_limit(self):
        response = self.client.patch(ME, {'bio': 'b' * 281})
        self.assertEqual(error(response)['fields']['bio'], ['Self description can be at most 280 characters.'])
        self.assertEqual(self.client.patch(ME, {'bio': 'b' * 280}).status_code, 200)

    def test_old_invalid_gender_does_not_block_other_changes(self):
        """As on the page, whose dropdown can't show it and so sends it back blank."""
        type(self.user).objects.filter(pk=self.user.pk).update(gender='robot')
        response = self.client.patch(ME, {'bio': 'Hi'})
        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.gender)

    def test_vehicle_year_message_matches_the_page(self):
        response = self.client.post(V, {'make': 'Honda', 'year': 1800, 'plate': 'OLD001'})
        next_year = timezone.localdate().year + 1
        self.assertEqual(error(response)['fields']['year'], [f'Year must be between 1900 and {next_year}.'])

    def test_remove_phone(self):
        self.client.patch(ME, {'country_code': '', 'phone_number': ''})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.phone)

    def test_driver_needs_a_vehicle(self):
        response = self.client.patch(ME, {'role': 'driver'})
        self.assertEqual(error(response)['fields']['role'], ['You must add at least one vehicle to be a driver.'])
        self.client.post(V, {'make': 'Honda', 'year': 2019, 'plate': 'XYZ789'})
        response = self.client.patch(ME, {'role': 'driver', 'license_number': 'H123',
                                          'license_expiry': (date.today() + timedelta(days=30)).isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()
        self.assertEqual((self.user.role, self.user.license_number), ('driver', 'H123'))

    def test_riders_licence_is_not_saved(self):
        self.client.patch(ME, {'license_number': 'R999'})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.license_number)

    def test_expired_licence_refused_for_drivers(self):
        driver = verified_driver()
        response = auth_client(driver).patch(ME, {'license_expiry': (date.today() - timedelta(days=1)).isoformat()})
        self.assertEqual(error(response)['fields']['license_expiry'],
                         ['Your license has expired. Please enter the expiry date of a valid license.'])

    def test_identity_change_ends_verification(self):
        driver = verified_driver()
        client = auth_client(driver)
        # Case and hyphens don't count as a change (as DL-Verification compares).
        response = client.patch(ME, {'full_name': driver.full_name.upper(), 'license_number': 'd123456789'})
        self.assertEqual(response.json()['warnings'], [])
        driver.refresh_from_db()
        self.assertTrue(driver.driver_verified)
        response = client.patch(ME, {'full_name': 'Someone Else'})
        self.assertEqual(response.json()['warnings'], [
            'Your name, date of birth or license details changed. Please verify your identity again before '
            'posting rides.'])
        driver.refresh_from_db()
        self.assertFalse(driver.driver_verified)
        self.assertIsNone(driver.face_match_score)

    def test_bio_change_keeps_verification(self):
        driver = verified_driver()
        auth_client(driver).patch(ME, {'bio': 'Hello'})
        driver.refresh_from_db()
        self.assertTrue(driver.driver_verified)


class ProfilePhotoTests(MediaRootMixin, ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com')
        self.client = auth_client(self.user)

    def upload(self, photo):
        return self.client.post(ME + 'photo/', {'photo': photo}, format='multipart')

    def test_upload_and_download(self):
        response = self.upload(jpeg())
        self.assertEqual(response.status_code, 200, response.content)
        url = response.json()['photo_url']
        rel = media_path(url)
        self.assertTrue(rel.startswith(f'selfies/{self.user.pk}/'))
        self.assertTrue(self.stored(rel))
        self.user.refresh_from_db()
        self.assertEqual(self.user.avatar_url, f'/private-media/{rel}')

        # The owner gets it with the token...
        got = self.client.get(url)
        self.assertEqual((got.status_code, got['Content-Type']), (200, 'image/jpeg'))
        self.assertEqual(got['Cache-Control'], 'private, no-store')
        self.assertTrue(b''.join(got.streaming_content).startswith(b'\xff\xd8'))
        # ...nobody else does, and it doesn't say whether the file exists.
        self.assertEqual(auth_client(make_user('other@example.com')).get(url).status_code, 404)
        self.assertEqual(APIClient().get(url).status_code, 401)
        # The website's link still works for the owner.
        web = Client()
        web.force_login(self.user)
        self.assertEqual(web.get(self.user.avatar_url).status_code, 200)

    def test_new_photo_deletes_the_old_file(self):
        first = media_path(self.upload(jpeg()).json()['photo_url'])
        second = media_path(self.upload(jpeg()).json()['photo_url'])
        self.assertNotEqual(first, second)
        self.assertFalse(self.stored(first))
        self.assertTrue(self.stored(second))

    def test_not_an_image(self):
        response = self.upload(SimpleUploadedFile('x.jpg', b'not an image', content_type='image/jpeg'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(error(response), {'code': 'invalid_image', 'message': "That file isn't a valid image.",
                                           'fields': {'photo': ["That file isn't a valid image."]}})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.avatar_url)

    @override_settings(IMAGE_UPLOAD_MAX_MB=0)
    def test_too_large(self):
        response = self.upload(jpeg())
        self.assertEqual(error(response)['code'], 'invalid_image')
        self.assertIn('too large', error(response)['message'])

    def test_no_file(self):
        self.assertEqual(error(self.client.post(ME + 'photo/', {}, format='multipart'))['code'], 'required')

    def test_media_paths_are_checked(self):
        for bad in ('../settings.py', 'other/1/x.jpg', 'selfies/../../x.jpg'):
            self.assertEqual(self.client.get('/api/v1/media/' + bad).status_code, 404, bad)

    def test_staff_can_see_photos(self):
        url = self.upload(jpeg()).json()['photo_url']
        self.assertEqual(auth_client(make_user('staff@example.com', is_staff=True)).get(url).status_code, 200)

    def test_media_open_without_the_new_terms(self):
        url = self.upload(jpeg()).json()['photo_url']
        self.user.legal_acceptances.all().delete()
        self.assertEqual(self.client.get(url).status_code, 200)


class ContactEmailTests(ApiTestCase):
    URL = ME + 'contact-email/'

    def test_only_for_apple_relay_accounts(self):
        response = auth_client(make_user('rider@example.com')).post(self.URL, {'contact_email': 'me@x.com'})
        self.assertEqual(error(response)['code'], 'not_private_relay')

    def test_set_confirm_flow(self):
        user = make_user(RELAY)
        client = auth_client(user)
        response = client.post(self.URL, {'contact_email': 'Me@RealMail.com'})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['warnings'], ['We sent a confirmation link to me@realmail.com. We will start using '
                                            'it once you click the link.'])
        self.assertEqual((body['profile']['contact_email'], body['profile']['contact_email_confirmed']),
                         ('me@realmail.com', False))
        self.assertEqual(mail.outbox[-1].to, ['me@realmail.com'])
        user.refresh_from_db()
        user.contact_email_confirmed = True
        user.save()
        again = client.post(self.URL, {'contact_email': 'me@realmail.com'})
        self.assertEqual(error(again)['fields']['contact_email'], ['This is already your communication email.'])
        removed = client.post(self.URL, {'contact_email': ''}).json()
        self.assertIsNone(removed['profile']['contact_email'])

    def test_rules(self):
        client = auth_client(make_user(RELAY))
        self.assertEqual(error(client.post(self.URL, {'contact_email': 'nope'}))['fields']['contact_email'],
                         ['Enter a valid email address.'])
        self.assertEqual(error(client.post(self.URL, {'contact_email': 'x@privaterelay.appleid.com'}))['fields']
                         ['contact_email'], ["Enter an email address you read, not an Apple relay address."])

    @override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'change-email': (1, 3600)})
    def test_rate_limit_shared_with_the_profile_page(self):
        user = make_user(RELAY)
        web = Client()
        web.force_login(user)
        web.post('/profile/contact-email/', {'contact_email': 'one@realmail.com'})
        response = auth_client(user).post(self.URL, {'contact_email': 'two@realmail.com'})
        self.assertEqual(response.status_code, 429)


class VehicleTests(MediaRootMixin, ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('rider@example.com')
        self.client = auth_client(self.user)

    def add(self, client=None, **fields):
        body = {'make': 'Honda', 'model': 'Civic', 'year': 2019, 'color': 'Blue', 'plate': 'XYZ789', **fields}
        return (client or self.client).post(V, body)

    def test_add_and_list(self):
        response = self.add()
        self.assertEqual(response.status_code, 201, response.content)
        first = response.json()['vehicle']
        self.assertEqual((first['is_primary'], first['vehicle_verified'], first['seat_capacity']), (True, False, 4))
        self.assertEqual(response.json()['warnings'], [])
        second = self.add(plate='SEC456').json()['vehicle']
        self.assertFalse(second['is_primary'])
        listed = self.client.get(V).json()
        self.assertEqual([v['plate'] for v in listed], ['XYZ789', 'SEC456'])
        self.assertEqual(self.client.get(f"{V}{second['id']}/").json()['plate'], 'SEC456')

    def test_plate_rules(self):
        Vehicle.objects.create(owner=make_user('other@example.com'), make='Kia', model='Rio', year=2018,
                               color='Grey', plate='TAKEN1')
        response = self.add(plate='TAKEN1')
        self.assertEqual(error(response)['code'], 'plate_taken')
        self.assertEqual(error(response)['fields']['plate'], ['Plate "TAKEN1" is already registered to another vehicle.'])
        self.add()
        response = self.add(plate='xyz789')
        self.assertEqual(error(response)['code'], 'duplicate_plate')
        self.assertEqual(error(response)['message'], 'Duplicate plate numbers are not allowed.')

    def test_field_checks(self):
        self.assertIn('make', error(self.add(make=''))['fields'])
        self.assertIn('year', error(self.add(year=timezone.localdate().year + 2))['fields'])
        self.assertIn('year', error(self.add(year=1800))['fields'])
        self.assertIn('plate', error(self.add(plate='P' * 21))['fields'])
        self.assertFalse(Vehicle.objects.exists())

    def test_verified_driver_is_told_to_verify_a_new_vehicle(self):
        client = auth_client(verified_driver())
        response = self.add(client=client, plate='NEW001')
        self.assertEqual(response.json()['warnings'], [
            'Vehicle NEW001 needs to be verified before you can use it for rides. Make it your primary vehicle '
            'and verify your identity.'])

    def test_change_details_ends_verification(self):
        driver = verified_driver()
        client = auth_client(driver)
        vehicle = driver.vehicles.get()
        url = f'{V}{encode_id(vehicle.pk)}/'
        same = client.patch(url, {'color': 'red'})  # case only: no real change
        self.assertEqual(same.json()['warnings'], [])
        vehicle.refresh_from_db()
        self.assertTrue(vehicle.vehicle_verified)
        changed = client.patch(url, {'color': 'Black'})
        self.assertFalse(changed.json()['vehicle']['vehicle_verified'])
        self.assertEqual(len(changed.json()['warnings']), 1)
        vehicle.refresh_from_db()
        self.assertEqual((vehicle.color, vehicle.make), ('Black', 'Toyota'))

    def test_change_plate_rules(self):
        url = f"{V}{self.add().json()['vehicle']['id']}/"
        other = self.add(plate='SEC456').json()['vehicle']
        self.assertEqual(error(self.client.patch(f"{V}{other['id']}/", {'plate': 'xyz789'}))['code'],
                         'duplicate_plate')
        self.assertEqual(self.client.patch(url, {'plate': 'XYZ789'}).status_code, 200)  # its own plate

    def test_someone_elses_vehicle_is_404(self):
        theirs = verified_driver().vehicles.get()
        url = f'{V}{encode_id(theirs.pk)}/'
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {'color': 'Pink'}).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertEqual(self.client.get(f'{V}junk/').status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.color, 'Red')

    def test_photo(self):
        driver = verified_driver()
        client = auth_client(driver)
        vehicle = driver.vehicles.get()
        url = f'{V}{encode_id(vehicle.pk)}/photo/'
        response = client.post(url, {'photo': jpeg()}, format='multipart')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertFalse(body['vehicle']['vehicle_verified'])
        self.assertEqual(len(body['warnings']), 1)
        first = media_path(body['vehicle']['photo_url'])
        self.assertTrue(first.startswith(f'vehicles/{driver.pk}/'))
        got = client.get(body['vehicle']['photo_url'])
        self.assertEqual(got.status_code, 200)
        got.close()  # releases the file (Windows won't delete an open file)
        self.assertEqual(self.client.get(body['vehicle']['photo_url']).status_code, 404)  # not the owner
        second = media_path(client.post(url, {'photo': jpeg()}, format='multipart').json()['vehicle']['photo_url'])
        self.assertFalse(self.stored(first))
        self.assertTrue(self.stored(second))
        bad = client.post(url, {'photo': SimpleUploadedFile('x.jpg', b'nope')}, format='multipart')
        self.assertEqual(error(bad)['code'], 'invalid_image')

    def test_primary(self):
        self.add()
        second = self.add(plate='SEC456').json()['vehicle']
        listed = self.client.post(f"{V}{second['id']}/primary/").json()
        self.assertEqual([(v['plate'], v['is_primary']) for v in listed], [('SEC456', True), ('XYZ789', False)])
        self.assertEqual(Vehicle.objects.filter(owner=self.user, is_primary=True).count(), 1)

    def test_delete(self):
        first = self.add().json()['vehicle']
        second = self.add(plate='SEC456').json()['vehicle']
        photo = media_path(self.client.post(f"{V}{first['id']}/photo/", {'photo': jpeg()},
                                            format='multipart').json()['vehicle']['photo_url'])
        self.assertEqual(self.client.delete(f"{V}{first['id']}/").status_code, 204)
        self.assertFalse(self.stored(photo))
        # The other vehicle takes over as primary.
        self.assertEqual([(v['id'], v['is_primary']) for v in self.client.get(V).json()],
                         [(second['id'], True)])
        # A rider may remove their last vehicle.
        self.assertEqual(self.client.delete(f"{V}{second['id']}/").status_code, 204)

    def test_driver_keeps_one_vehicle(self):
        driver = verified_driver()
        response = auth_client(driver).delete(f'{V}{encode_id(driver.vehicles.get().pk)}/')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(error(response)['code'], 'last_vehicle')
        self.assertTrue(driver.vehicles.exists())

    def test_profile_page_sees_api_vehicles(self):
        """A plate added in the app is taken on the website too."""
        self.add(plate='APP001')
        driver = verified_driver('web@example.com')
        web = Client()
        web.force_login(driver)
        web.post('/profile/', {
            'full_name': driver.full_name, 'dob': driver.dob.isoformat(), 'role': 'driver',
            'license_number': driver.license_number, 'license_province': driver.license_province,
            'license_expiry': driver.license_expiry.isoformat(),
            'vehicle_make': ['Toyota'], 'vehicle_model': ['Corolla'], 'vehicle_year': ['2020'],
            'vehicle_color': ['Red'], 'vehicle_plate': ['APP001'], 'vehicle_id': [''],
        })
        self.assertFalse(Vehicle.objects.filter(owner=driver, plate='APP001').exists())
