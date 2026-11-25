"""The map link: /map/suggest/, /map/details/, /map/route/, /map/token/, the page config tag, the two route-map
templates, the CSP and the privacy-policy wording. The map node is faked by patching requests.get/post inside
komute.services.map_client, so no network or Apple call happens."""
from datetime import date
from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from komute.hashid_utils import encode_id
from komute.models import RideSearch
from komute.services import map_client
from komute.tests_regression import make_driver_with_vehicle, make_ride, make_user

REF = 'A' * 40
GET = 'komute.services.map_client.http_client.get'
POST = 'komute.services.map_client.http_client.post'
ROWS = {'results': [
    {'ref': REF, 'type': 'address', 'title': '100 Queen St W', 'subtitle': 'Toronto, ON, Canada', 'lat': 43.65, 'lon': -79.38, 'internal': 'SECRET'},
    {'ref': 'B' * 40, 'type': 'poi', 'title': 'Tim Hortons', 'subtitle': 'Toronto, ON', 'lat': None, 'lon': None},
    {'ref': 'not a valid ref!', 'title': 'dropped'},
    {'title': 'no ref'},
]}


def reply(status=200, body=None, bad_json=False):
    m = MagicMock()
    m.status_code = status
    if bad_json:
        m.json.side_effect = ValueError('no json')
    else:
        m.json.return_value = {} if body is None else body
    return m


class MapEndpointBase(TestCase):
    def setUp(self):
        cache.clear()
        self.rider = make_user('map_rider@example.com')
        self.client.force_login(self.rider)


class SuggestTests(MapEndpointBase):
    url = '/map/suggest/'

    def test_needs_a_signed_in_user(self):
        self.client.logout()
        r = self.client.get(self.url, {'q': 'queen'})
        self.assertEqual((r.status_code, r.json()['code']), (401, 'auth'))

    def test_get_only(self):
        self.assertEqual(self.client.post(self.url, {'q': 'queen'}).status_code, 405)

    @patch(GET)
    def test_returns_only_the_fields_the_page_uses_and_drops_unusable_rows(self, get):
        get.return_value = reply(body=ROWS)
        r = self.client.get(self.url, {'q': '100 queen', 'lat': '43.65', 'lon': '-79.38'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Cache-Control'], 'no-store')
        rows = r.json()['results']
        self.assertEqual([x['title'] for x in rows], ['100 Queen St W', 'Tim Hortons'])
        self.assertEqual(set(rows[0]), {'ref', 'type', 'title', 'subtitle', 'lat', 'lon'})
        self.assertNotIn('SECRET', r.content.decode())
        self.assertIsNone(rows[1]['lat'])

    @patch(GET)
    def test_sends_the_question_and_the_shared_key_to_the_node(self, get):
        get.return_value = reply(body=ROWS)
        with override_settings(MAP_API_URL='http://node.test:8081/', MAP_API_KEY='shared-secret'):
            self.client.get(self.url, {'q': ' 100 queen ', 'lat': '43.65', 'lon': '-79.38'})
        args, kwargs = get.call_args
        self.assertEqual(args[0], 'http://node.test:8081/v1/places/suggest')
        self.assertEqual(kwargs['params'], {'q': '100 queen', 'limit': 5, 'lat': 43.65, 'lon': -79.38})
        self.assertEqual(kwargs['headers'], {'X-API-Key': 'shared-secret'})
        self.assertLessEqual(max(kwargs['timeout']), 10)

    @patch(GET)
    def test_no_location_is_fine_the_node_has_a_default(self, get):
        get.return_value = reply(body=ROWS)
        self.assertEqual(self.client.get(self.url, {'q': 'queen'}).status_code, 200)
        self.assertNotIn('lat', get.call_args.kwargs['params'])

    @patch(GET)
    def test_bad_input_never_reaches_the_node(self, get):
        for params in ({}, {'q': ''}, {'q': '   '}, {'q': 'x' * 201}):
            r = self.client.get(self.url, params)
            self.assertEqual((r.status_code, r.json()['code']), (400, 'invalid'), params)
        get.assert_not_called()

    @patch(GET)
    def test_an_unusable_location_hint_is_dropped_not_refused(self, get):
        # A rider whose device is abroad (here India) or reports junk must still be able to search.
        get.return_value = reply(body=ROWS)
        for hint in ({'lat': '17.769653', 'lon': '83.21521'}, {'lat': '51.5', 'lon': '-0.12'}, {'lat': '43.6'},
                     {'lon': '-79'}, {'lat': 'abc', 'lon': '-79'}, {'lat': 'nan', 'lon': '-79'}):
            r = self.client.get(self.url, {'q': 'vancouver', **hint})
            self.assertEqual(r.status_code, 200, hint)
            params = get.call_args.kwargs['params']
            self.assertEqual(params['q'], 'vancouver', hint)
            self.assertNotIn('lat', params, hint)
            self.assertNotIn('lon', params, hint)

    @patch(GET)
    def test_us_points_are_accepted(self, get):
        get.return_value = reply(body=ROWS)
        for lat, lon in (('40.71', '-74.0'), ('21.3', '-157.86'), ('61.2', '-149.9')):
            self.assertEqual(self.client.get(self.url, {'q': 'main', 'lat': lat, 'lon': lon}).status_code, 200)

    @patch(GET)
    def test_node_failures_become_clean_messages(self, get):
        cases = [
            (requests.exceptions.ConnectionError('http://secret-host/?key=SECRET'), 503, 'unavailable'),
            (requests.exceptions.Timeout(), 504, 'timeout'),
            (reply(503, {'error': {'message': 'SECRET'}}), 503, 'busy'),
            (reply(422, {'error': {'code': 'OUTSIDE_SERVICE_AREA', 'message': 'SECRET'}}), 422, 'outside_area'),
            (reply(422, {'error': {'code': 'INVALID_FIELD', 'message': 'SECRET'}}), 400, 'invalid'),
            (reply(503, {'error': {'code': 'NOT_CONFIGURED', 'message': 'SECRET'}}), 503, 'config'),
            (reply(504, {'error': {'code': 'UPSTREAM_TIMEOUT'}}), 504, 'timeout'),
            (reply(401), 502, 'config'),
            (reply(500, {'x': 'SECRET'}), 502, 'error'),
            (reply(bad_json=True), 502, 'bad_answer'),
            (reply(body=[1, 2]), 502, 'bad_answer'),
        ]
        for effect, status, code in cases:
            get.side_effect = effect if isinstance(effect, Exception) else None
            get.return_value = None if isinstance(effect, Exception) else effect
            with self.assertLogs('komute.views', level='ERROR') if code in ('config', 'error', 'invalid') else self.assertNoLogs('komute.views', level='ERROR'):
                r = self.client.get(self.url, {'q': 'queen'})
            self.assertEqual((r.status_code, r.json()['code']), (status, code), code)
            self.assertNotIn('SECRET', r.content.decode())
            self.assertNotIn('secret-host', r.content.decode())

    @patch(GET)
    def test_rate_limit_is_per_user(self, get):
        get.return_value = reply(body=ROWS)
        with override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'map-suggest': (2, 60)}):
            self.assertEqual([self.client.get(self.url, {'q': 'queen'}).status_code for _ in range(3)], [200, 200, 429])
            other = make_user('map_other@example.com')
            self.client.force_login(other)
            self.assertEqual(self.client.get(self.url, {'q': 'queen'}).status_code, 200)  # another person has their own allowance


class DetailsTests(MapEndpointBase):
    url = '/map/details/'

    def test_needs_a_signed_in_user(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url, {'ref': REF}).status_code, 401)

    @patch(GET)
    def test_returns_the_place(self, get):
        get.return_value = reply(body={'label': '100 Queen St W, Toronto ON', 'lat': 43.65, 'lon': -79.38, 'title': 'x',
                                       'address': {'house_number': '100', 'street': 'Queen St W'}, 'secret': 'SECRET'})
        r = self.client.get(self.url, {'ref': REF})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'label': '100 Queen St W, Toronto ON', 'lat': 43.65, 'lon': -79.38, 'address': {'house_number': '100', 'street': 'Queen St W'}})
        self.assertEqual(get.call_args.args[0].rsplit('/', 3)[-3:], ['v1', 'places', 'details'])

    @patch(GET)
    def test_bad_refs_never_reach_the_node(self, get):
        for ref in ('', 'short', 'A' * 2201, REF + '/../x', 'https://evil.example/' + 'a' * 20, REF + ' x'):
            self.assertEqual(self.client.get(self.url, {'ref': ref}).status_code, 400, ref)
        get.assert_not_called()

    @patch(GET)
    def test_unknown_place_and_missing_coordinates(self, get):
        get.return_value = reply(404)
        self.assertEqual(self.client.get(self.url, {'ref': REF}).json()['code'], 'not_found')
        get.return_value = reply(body={'label': 'x'})
        self.assertEqual(self.client.get(self.url, {'ref': REF}).status_code, 404)

    @patch(GET)
    def test_a_place_outside_the_service_area_is_refused(self, get):
        get.return_value = reply(body={'label': 'Paris', 'lat': 48.85, 'lon': 2.35})
        r = self.client.get(self.url, {'ref': REF})
        self.assertEqual((r.status_code, r.json()['code']), (422, 'outside_area'))

    @patch(GET)
    def test_rate_limit(self, get):
        get.return_value = reply(body={'label': 'x', 'lat': 43.6, 'lon': -79.4})
        with override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'map-details': (1, 60)}):
            self.assertEqual([self.client.get(self.url, {'ref': REF}).status_code for _ in range(2)], [200, 429])


class BiasTests(TestCase):
    def test_a_rider_is_biased_to_the_origin_of_their_latest_search(self):
        rider = make_user('bias_rider@example.com')
        for i, lat in enumerate((43.65, 49.28)):
            RideSearch.objects.create(user=rider, origin='o', origin_lat=lat, origin_lng=-79.0 - i, destination='d', dest_lat=45.4, dest_lng=-75.7, search_date=date.today())
        self.assertEqual(map_client.bias_for(rider), (49.28, -80.0))

    def test_a_driver_is_biased_to_the_origin_of_their_latest_ride(self):
        driver = make_driver_with_vehicle('bias_driver@example.com')
        make_ride(driver, origin_lat=51.04, origin_lng=-114.07)
        self.assertEqual(map_client.bias_for(driver), (51.04, -114.07))

    def test_nothing_usable_gives_none(self):
        self.assertIsNone(map_client.bias_for(make_user('bias_new@example.com')))
        rider = make_user('bias_zero@example.com')
        RideSearch.objects.create(user=rider, origin='o', origin_lat=0, origin_lng=0, destination='d', dest_lat=45.4, dest_lng=-75.7, search_date=date.today())
        self.assertIsNone(map_client.bias_for(rider))
        far = make_user('bias_far@example.com')
        RideSearch.objects.create(user=far, origin='o', origin_lat=48.85, origin_lng=2.35, destination='d', dest_lat=45.4, dest_lng=-75.7, search_date=date.today())
        self.assertIsNone(map_client.bias_for(far))


class PageConfigTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('cfg_driver@example.com')
        self.rider = make_user('cfg_rider@example.com')
        self.ride = make_ride(self.driver, origin_lat=45.42, origin_lng=-75.69)
        RideSearch.objects.create(user=self.rider, origin='o', origin_lat=44.23, origin_lng=-76.48, destination='d', dest_lat=45.4, dest_lng=-75.7, search_date=date.today())

    def pages(self):
        return [(self.driver, reverse('post-ride')), (self.driver, reverse('edit-ride', args=[encode_id(self.ride.id)])), (self.rider, reverse('search-rides'))]

    def test_switch_off_by_default_so_nothing_changes(self):
        self.assertEqual(settings.MAP_AUTOCOMPLETE, 'photon')
        for user, url in self.pages():
            self.client.force_login(user)
            html = self.client.get(url).content.decode()
            self.assertIn('<div id="map-config" hidden data-autocomplete="photon" data-routes="osrm"></div>', html, url)
            self.assertNotIn('data-bias-lat', html, url)
            self.assertLess(html.index('id="map-config"'), html.index('js/place-autocomplete.js'), url)  # config is read when the script runs

    @override_settings(MAP_AUTOCOMPLETE='apple')
    def test_apple_switch_carries_the_riders_own_starting_point(self):
        for user, url, lat, lon in ((self.driver, reverse('post-ride'), '45.42', '-75.69'), (self.rider, reverse('search-rides'), '44.23', '-76.48')):
            self.client.force_login(user)
            html = self.client.get(url).content.decode()
            self.assertIn(f'data-autocomplete="apple" data-routes="osrm" data-bias-lat="{lat}" data-bias-lon="{lon}"', html, url)

    @override_settings(MAP_AUTOCOMPLETE='apple')
    def test_apple_switch_without_history_sends_no_hint(self):
        self.client.force_login(make_driver_with_vehicle('zzz_new@example.com'))
        html = self.client.get(reverse('post-ride')).content.decode()
        self.assertIn('<div id="map-config" hidden data-autocomplete="apple" data-routes="osrm"></div>', html)


class PrivacyWordingTests(TestCase):
    """The policy must say what the site really does, in both states of the switch."""

    def html(self):
        return self.client.get(reverse('privacy-policy')).content.decode()

    def test_photon_state_is_unchanged(self):
        html = self.html()
        self.assertIn("the text is sent to Komoot's Photon service", html)
        self.assertIn('Komoot, Photon address search', html)
        self.assertNotIn('Apple Maps', html)

    @override_settings(MAP_AUTOCOMPLETE='apple')
    def test_apple_state_names_apple_and_keeps_photon_for_locate_me(self):
        html = self.html()
        self.assertIn('Apple Maps', html)
        self.assertIn('which pass it to Apple Maps', html)
        self.assertNotIn("the text is sent to Komoot's Photon service", html)
        self.assertNotIn('Komoot, Photon address search', html)
        self.assertIn('your device\'s position if you use "Locate Me"', html)  # Photon still gets Locate Me
        self.assertIn("your device's GPS coordinates are sent to Komoot's Photon geocoding service", html)


# ---- Release 2: routes ----------------------------------------------------------------------------------------------------
NODE_ROUTE = {'distance_m': 450600, 'duration_s': 16200, 'traffic_delay_s': None, 'legs': [],
              'geometry': [[-79.383612345, 43.652812345], [-79.383612345, 43.652812345], [-75.7009, 45.4236]]}
T, K, O = '43.6528,-79.3836', '44.2312,-76.486', '45.4236,-75.7009'   # Toronto, Kingston, Ottawa


class RouteTests(MapEndpointBase):
    url = '/map/route/'

    def test_needs_a_signed_in_user_and_get(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url, {'o': T, 'd': O}).status_code, 401)
        self.client.force_login(self.rider)
        self.assertEqual(self.client.post(self.url, {'o': T, 'd': O}).status_code, 405)

    @patch(POST)
    def test_returns_distance_and_duration_only(self, post):
        post.return_value = reply(body=NODE_ROUTE)
        with override_settings(MAP_API_URL='http://node.test:8081', MAP_API_KEY='shared-secret'):
            r = self.client.get(self.url, {'o': T, 'd': O})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Cache-Control'], 'no-store')
        self.assertEqual(r.json(), {'distance_m': 450600, 'duration_s': 16200})
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'http://node.test:8081/v1/route')
        self.assertEqual(kwargs['json'], {'origin': {'lat': 43.6528, 'lon': -79.3836}, 'destination': {'lat': 45.4236, 'lon': -75.7009}, 'waypoints': []})
        self.assertEqual(kwargs['headers'], {'X-API-Key': 'shared-secret'})
        self.assertEqual(kwargs['timeout'], map_client.ROUTE_TIMEOUT)

    @patch(POST)
    def test_stops_keep_their_order(self, post):
        post.return_value = reply(body=NODE_ROUTE)
        self.client.get(self.url, {'o': T, 'd': O, 's': K + ';44.5,-76.0'})
        self.assertEqual(post.call_args.kwargs['json']['waypoints'], [{'lat': 44.2312, 'lon': -76.486}, {'lat': 44.5, 'lon': -76.0}])

    @patch(POST)
    def test_direct_asks_for_the_start_to_end_route_too(self, post):
        def node(url, json, **kw):
            return reply(body={**NODE_ROUTE, 'distance_m': 404000 if not json['waypoints'] else 460000})
        post.side_effect = node
        r = self.client.get(self.url, {'o': T, 'd': O, 's': K, 'direct': '1'})
        self.assertEqual(r.json(), {'distance_m': 460000, 'duration_s': 16200, 'direct_distance_m': 404000})
        self.assertEqual(sorted(len(c.kwargs['json']['waypoints']) for c in post.call_args_list), [0, 1])

    @patch(POST)
    def test_direct_without_stops_is_a_single_route(self, post):
        post.return_value = reply(body=NODE_ROUTE)
        r = self.client.get(self.url, {'o': T, 'd': O, 'direct': '1'})
        self.assertNotIn('direct_distance_m', r.json())
        self.assertEqual(post.call_count, 1)

    @patch(POST)
    def test_a_failed_direct_route_only_drops_the_hint(self, post):
        def node(url, json, **kw):
            return reply(body=NODE_ROUTE) if json['waypoints'] else reply(422, {'error': {'code': 'NO_ROUTE'}})
        post.side_effect = node
        r = self.client.get(self.url, {'o': T, 'd': O, 's': K, 'direct': '1'})
        self.assertEqual((r.status_code, r.json()['direct_distance_m']), (200, None))

    @patch(POST)
    def test_line_is_rounded_and_deduplicated_and_only_sent_when_asked(self, post):
        post.return_value = reply(body=NODE_ROUTE)
        r = self.client.get(self.url, {'o': T, 'd': O, 'line': '1'})
        self.assertEqual(r.json()['line'], [[-79.38361, 43.65281], [-75.7009, 45.4236]])

    @patch(POST)
    def test_bad_input_never_reaches_the_node(self, post):
        for params in ({}, {'o': T}, {'d': O}, {'o': '43.6', 'd': O}, {'o': '43.6,-79.3,1', 'd': O}, {'o': 'nan,-79', 'd': O},
                       {'o': 'a,b', 'd': O}, {'o': '51.5,-0.12', 'd': O}, {'o': T, 'd': O, 's': '51.5,-0.12'},
                       {'o': T, 'd': O, 's': K + ';'}, {'o': T, 'd': O, 's': ';'.join([K] * 6)}):
            r = self.client.get(self.url, params)
            self.assertEqual((r.status_code, r.json()['code']), (400, 'invalid'), params)
        post.assert_not_called()

    @patch(POST)
    def test_five_stops_is_the_limit(self, post):
        post.return_value = reply(body=NODE_ROUTE)
        self.assertEqual(self.client.get(self.url, {'o': T, 'd': O, 's': ';'.join([K] * 5)}).status_code, 200)

    @patch(POST)
    def test_us_trips_are_accepted(self, post):
        post.return_value = reply(body=NODE_ROUTE)
        self.assertEqual(self.client.get(self.url, {'o': '40.71,-74.0', 'd': '42.36,-71.06'}).status_code, 200)

    @patch(POST)
    def test_node_failures_become_clean_messages(self, post):
        cases = [
            (reply(422, {'error': {'code': 'NO_ROUTE', 'message': 'SECRET'}}), 422, 'no_route'),
            (requests.exceptions.ConnectionError('http://secret-host/?key=SECRET'), 503, 'unavailable'),
            (requests.exceptions.Timeout(), 504, 'timeout'),
            (reply(body={'distance_m': 'SECRET'}), 502, 'bad_answer'),
            (reply(503, {'error': {'code': 'UPSTREAM_BUSY'}}), 503, 'busy'),
        ]
        for effect, status, code in cases:
            cache.clear()
            post.side_effect = effect if isinstance(effect, Exception) else None
            post.return_value = None if isinstance(effect, Exception) else effect
            r = self.client.get(self.url, {'o': T, 'd': O})
            self.assertEqual((r.status_code, r.json()['code']), (status, code), code)
            self.assertNotIn('SECRET', r.content.decode())

    @patch(POST)
    def test_answers_are_cached_per_trip_and_options(self, post):
        post.return_value = reply(body=NODE_ROUTE)
        for _ in range(3):
            self.client.get(self.url, {'o': T, 'd': O})
        self.assertEqual(post.call_count, 1)
        self.client.get(self.url, {'o': T, 'd': O, 'line': '1'})     # different options, different answer
        self.client.get(self.url, {'o': O, 'd': T})                  # the other direction is another trip
        self.assertEqual(post.call_count, 3)

    @patch(POST)
    def test_failures_are_not_cached(self, post):
        post.return_value = reply(422, {'error': {'code': 'NO_ROUTE'}})
        self.client.get(self.url, {'o': T, 'd': O})
        post.return_value = reply(body=NODE_ROUTE)
        self.assertEqual(self.client.get(self.url, {'o': T, 'd': O}).status_code, 200)

    @patch(POST)
    def test_rate_limit(self, post):
        post.return_value = reply(body=NODE_ROUTE)
        with override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'map-route': (1, 60)}):
            self.assertEqual([self.client.get(self.url, {'o': T, 'd': O}).status_code for _ in range(2)], [200, 429])


# ---- Release 3: the browser map's token -------------------------------------------------------------------------------------
class TokenTests(MapEndpointBase):
    url = '/map/token/'

    @patch(GET)
    def test_not_served_while_the_apple_map_is_off(self, get):
        self.assertEqual(settings.MAP_DISPLAY, 'leaflet')
        self.assertEqual(self.client.get(self.url).status_code, 404)
        get.assert_not_called()

    @override_settings(MAP_DISPLAY='mapkit')
    @patch(GET)
    def test_signed_in_users_get_a_token(self, get):
        get.return_value = reply(body={'token': 'header.payload.signature', 'expires_in': 1800, 'extra': 'SECRET'})
        r = self.client.get(self.url)
        self.assertEqual((r.status_code, r.json()), (200, {'token': 'header.payload.signature', 'expires_in': 1800}))
        self.assertEqual(r['Cache-Control'], 'no-store')
        self.assertTrue(get.call_args.args[0].endswith('/v1/mapkit-token'))
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)

    @override_settings(MAP_DISPLAY='mapkit')
    @patch(GET)
    def test_node_problems(self, get):
        for effect, status, code in ((reply(503, {'error': {'code': 'NOT_CONFIGURED'}}), 503, 'config'),
                                     (reply(body={'expires_in': 1800}), 502, 'bad_answer'),
                                     (reply(body={'token': '', 'expires_in': 1800}), 502, 'bad_answer')):
            get.return_value = effect
            r = self.client.get(self.url)
            self.assertEqual((r.status_code, r.json()['code']), (status, code), code)

    @override_settings(MAP_DISPLAY='mapkit')
    @patch(GET)
    def test_rate_limit(self, get):
        get.return_value = reply(body={'token': 't', 'expires_in': 1800})
        with override_settings(RATE_LIMITS={**settings.RATE_LIMITS, 'map-token': (1, 60)}):
            self.assertEqual([self.client.get(self.url).status_code for _ in range(2)], [200, 429])


# ---- the pages ---------------------------------------------------------------------------------------------------------------
class RouteFormPageTests(TestCase):
    def setUp(self):
        self.driver = make_driver_with_vehicle('rf_driver@example.com')
        self.ride = make_ride(self.driver)
        self.client.force_login(self.driver)

    def pages(self):
        return [reverse('post-ride'), reverse('edit-ride', args=[encode_id(self.ride.id)])]

    def test_forms_load_the_route_script_after_their_config(self):
        from django.contrib.staticfiles import finders
        self.assertIsNotNone(finders.find('js/map-routes.js'))
        for url in self.pages():
            html = self.client.get(url).content.decode()
            self.assertIn('data-routes="osrm"', html, url)
            self.assertLess(html.index('id="map-config"'), html.index('js/map-routes.js'), url)
            self.assertIn('KomuteRoutes.summary(', html, url)
            self.assertNotIn('router.project-osrm.org', html, url)  # the OSRM call now lives in map-routes.js only

    @override_settings(MAP_ROUTES='apple')
    def test_apple_switch_reaches_the_forms(self):
        for url in self.pages():
            self.assertIn('data-routes="apple"', self.client.get(url).content.decode(), url)


class RouteMapPageTests(TestCase):
    """ride-summary (driver, completed ride) and booking-details (rider, open ride) in both states of MAP_DISPLAY."""

    def setUp(self):
        from komute.models import RideStop
        self.driver = make_driver_with_vehicle('rm_driver@example.com')
        self.rider = make_user('rm_rider@example.com')
        evil = 'Union </script><script>alert(1)</script> Station'   # a place name typed by a driver
        self.done = make_ride(self.driver, status='completed', origin=evil)
        self.open = make_ride(self.driver, origin=evil)
        for ride in (self.done, self.open):
            RideStop.objects.create(ride=ride, stop_order=1, location='Kingston', lat=44.2312, lng=-76.486)

    def html(self):
        self.client.force_login(self.driver)
        summary = self.client.get(reverse('ride-summary', args=[encode_id(self.done.id)])).content.decode()
        self.client.force_login(self.rider)
        booking = self.client.get(reverse('create-ride-booking', args=[encode_id(self.open.id)])).content.decode()
        return {'ride-summary': summary, 'booking-details': booking}

    def test_leaflet_by_default(self):
        for page, html in self.html().items():
            self.assertIn('unpkg.com/leaflet@1.9.4/dist/leaflet.js', html, page)
            self.assertIn("L.map('routeMap'", html, page)
            self.assertIn('router.project-osrm.org', html, page)
            self.assertNotIn('apple-mapkit', html, page)
            self.assertNotIn('/map/route/', html, page)

    @override_settings(MAP_DISPLAY='mapkit')
    def test_mapkit_replaces_leaflet_and_osrm(self):
        for page, html in self.html().items():
            self.assertIn('<script src="https://cdn.apple-mapkit.com/mk/5.x.x/mapkit.js"></script>', html, page)
            self.assertIn("new mapkit.Map('routeMap'", html, page)
            self.assertIn("fetch('/map/token/'", html, page)
            self.assertIn("fetch('/map/route/?'", html, page)
            self.assertIn('lat: 44.2312, lng: -76.486', html, page)          # the stop is on the map
            self.assertNotIn('unpkg.com/leaflet', html, page)
            self.assertNotIn('L.map(', html, page)
            self.assertNotIn('router.project-osrm.org', html, page)          # Apple's map never shows an OSRM route
            self.assertEqual(html.count('mapkit.init('), 1, page)

    @override_settings(MAP_DISPLAY='mapkit')
    def test_place_names_cannot_break_out_of_the_script(self):
        for page, html in self.html().items():
            self.assertNotIn('</script><script>alert(1)', html, page)
            self.assertIn('Union \\u003C/script\\u003E\\u003Cscript\\u003Ealert(1)', html, page)


class CspTests(TestCase):
    def test_apple_map_host_is_allowed_where_mapkit_needs_it(self):
        policy = self.client.get(reverse('home'))['Content-Security-Policy-Report-Only']
        parts = {p.split()[0]: p for p in policy.split('; ')}
        for directive in ('script-src', 'img-src'):
            self.assertIn('https://cdn.apple-mapkit.com', parts[directive], directive)
        self.assertIn('https://*.apple-mapkit.com', parts['connect-src'])   # MapKit talks to several of its hosts
        self.assertEqual(parts['worker-src'], "worker-src 'self' blob:")    # and runs its workers from blob: URLs


class PrivacyMapWordingTests(TestCase):
    """Every switch combination: the policy names exactly the services the site really uses."""

    def html(self, **flags):
        with override_settings(**flags):
            return self.client.get(reverse('privacy-policy')).content.decode()

    def test_default_is_unchanged(self):
        html = self.html()
        self.assertIn("your browser sends the trip's origin, stop and destination coordinates to the Open Source Routing Machine", html)
        self.assertIn('<strong>OpenStreetMap Foundation</strong>', html)
        self.assertIn('<strong>unpkg</strong>', html)
        self.assertNotIn('Apple Maps', html)

    def test_apple_routes_only(self):
        html = self.html(MAP_ROUTES='apple')
        self.assertIn('<strong>Apple Maps</strong> (United States): receives the start, stop and end points of trips', html)
        self.assertIn('Apple does not receive your IP address or browser information. (see Section 11)', html)
        self.assertIn("they are sent to KOMUTE's servers, which ask Apple Maps for the route", html)
        self.assertIn('<strong>Open Source Routing Machine (OSRM) public routing service</strong>', html)   # the trip pages still use it
        self.assertIn('loads the map images from the OpenStreetMap Foundation', html)

    def test_apple_map_only(self):
        html = self.html(MAP_DISPLAY='mapkit')
        self.assertIn('The route maps on trip pages are loaded from Apple, so Apple also receives your IP address', html)
        self.assertIn('your browser loads the map itself from Apple', html)
        self.assertIn('<strong>Open Source Routing Machine (OSRM) public routing service</strong>', html)   # the forms still use it
        self.assertNotIn('<strong>OpenStreetMap Foundation</strong>', html)
        self.assertNotIn('<strong>unpkg</strong>', html)

    def test_everything_on_apple(self):
        html = self.html(MAP_AUTOCOMPLETE='apple', MAP_ROUTES='apple', MAP_DISPLAY='mapkit')
        self.assertIn('receives the addresses you type into place search, and the start, stop and end points of trips', html)
        self.assertNotIn('Open Source Routing Machine', html)
        self.assertNotIn('OpenStreetMap Foundation', html)
        self.assertIn('Komoot, Photon</strong> (Germany): receives your device', html)   # Locate Me stays on Photon
