"""Module 3 request and response shapes."""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.common.fields import HashidField
from komute.models import RideBooking
from komute.services import rides as ride_rules
from komute.services.common import display_name


# ── Responses ─────────────────────────────────────────────────────

class PlaceSerializer(serializers.Serializer):
    name = serializers.CharField()
    lat = serializers.FloatField()
    lng = serializers.FloatField()


class StopSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True, help_text='Pick-up / drop-off choice when booking (module 4).')
    name = serializers.CharField(source='location')
    lat = serializers.FloatField()
    lng = serializers.FloatField()
    order = serializers.IntegerField(source='stop_order')


class RideVehicleSerializer(serializers.Serializer):
    make = serializers.CharField()
    model = serializers.CharField()
    year = serializers.IntegerField()
    color = serializers.CharField()
    plate = serializers.CharField()


class RideDriverSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    name = serializers.SerializerMethodField()
    verified = serializers.BooleanField(source='driver_verified')
    rating = serializers.SerializerMethodField()
    rating_count = serializers.SerializerMethodField()
    gender = serializers.SerializerMethodField()

    def get_name(self, driver) -> str:
        return display_name(driver)

    def get_rating(self, driver) -> float:
        return round(driver.avg_rating or 0.0, 1)

    def get_rating_count(self, driver) -> int:
        return driver.total_rides or 0

    def get_gender(self, driver) -> str:
        return driver.get_gender_display() if driver.gender else ''


class RideSerializer(serializers.Serializer):
    """A ride as its driver, or a rider who may book it, sees it."""
    id = HashidField(source='pk', read_only=True)
    status = serializers.CharField(help_text='posted, full, completed or cancelled.')
    origin = serializers.SerializerMethodField()
    destination = serializers.SerializerMethodField()
    stops = serializers.SerializerMethodField()
    depart_at = serializers.DateTimeField()
    arrive_at = serializers.DateTimeField(allow_null=True)
    tz = serializers.CharField(allow_null=True, help_text="The driver's time zone, for showing local times.")
    seats_total = serializers.IntegerField()
    seats_available = serializers.IntegerField()
    price_cents = serializers.IntegerField(help_text='Per seat.')
    currency = serializers.CharField()
    passenger_gender_pref = serializers.CharField(help_text='any, women_only or men_only.')
    preferences = serializers.SerializerMethodField()
    notes = serializers.CharField(allow_null=True)
    recurring_group_id = serializers.CharField(allow_null=True)
    vehicle = serializers.SerializerMethodField()
    driver = RideDriverSerializer()
    is_mine = serializers.SerializerMethodField()

    @extend_schema_field(PlaceSerializer)
    def get_origin(self, ride):
        return {'name': ride.origin, 'lat': ride.origin_lat, 'lng': ride.origin_lng}

    @extend_schema_field(PlaceSerializer)
    def get_destination(self, ride):
        return {'name': ride.destination, 'lat': ride.dest_lat, 'lng': ride.dest_lng}

    @extend_schema_field(StopSerializer(many=True))
    def get_stops(self, ride):
        return StopSerializer(sorted(ride.stops.all(), key=lambda st: st.stop_order), many=True).data

    def get_preferences(self, ride) -> list[str]:
        return ride.preferences_json if isinstance(ride.preferences_json, list) else []

    @extend_schema_field(RideVehicleSerializer(allow_null=True))
    def get_vehicle(self, ride):
        # A ride keeps running after its vehicle is removed.
        return RideVehicleSerializer(ride.vehicle).data if ride.vehicle else None

    def get_is_mine(self, ride) -> bool:
        return ride.driver_id == self.context['request'].user.id


class RideDetailSerializer(RideSerializer):
    my_booking_status = serializers.SerializerMethodField(
        help_text="The rider's own booking on this ride: draft, requested, confirmed, or null.")
    driver_phone = serializers.SerializerMethodField(
        help_text='Only once the rider has a confirmed booking (privacy policy s.10).')

    def _my_booking(self, ride):
        user = self.context['request'].user
        if not hasattr(self, '_booking_cache'):
            self._booking_cache = RideBooking.objects.filter(
                ride=ride, passenger=user,
                status__in=[RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED],
            ).first()
        return self._booking_cache

    def get_my_booking_status(self, ride) -> str | None:
        booking = self._my_booking(ride)
        return booking.status if booking else None

    def get_driver_phone(self, ride) -> str | None:
        booking = self._my_booking(ride)
        if booking and booking.status == RideBooking.STATUS_CONFIRMED:
            return ride.driver.phone or ''
        return None


class PostRideResultSerializer(serializers.Serializer):
    ride = RideSerializer(help_text='The first ride (for a recurring ride, the earliest).')
    rides_created = serializers.IntegerField()


# ── Requests ──────────────────────────────────────────────────────

class PlaceInput(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True, default='')
    lat = serializers.FloatField(required=False, default=0.0)
    lng = serializers.FloatField(required=False, default=0.0)


class RecurringInput(serializers.Serializer):
    end_date = serializers.CharField(required=False, allow_blank=True, default='',
                                     help_text='YYYY-MM-DD, after the first departure, at most 4 months ahead.')
    days = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=6), required=False,
                                 default=list, help_text='Weekdays, 0 = Monday … 6 = Sunday, local to tz.')


class RideWriteSerializer(serializers.Serializer):
    """Fields are checked by komute.services.rides with the website's
    messages; this only parses them."""
    origin = PlaceInput(required=False)
    destination = PlaceInput(required=False)
    stops = PlaceInput(many=True, required=False)
    depart_at = serializers.DateTimeField(required=False, help_text='ISO 8601 with offset; no offset means UTC.')
    tz = serializers.CharField(required=False, allow_blank=True, default='',
                               help_text="IANA name, e.g. America/Toronto. Local weekdays and times use it.")
    seats = serializers.IntegerField(required=False)
    price_cents = serializers.IntegerField(required=False, help_text='Per seat, 1 to 100000.')
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    preferences = serializers.ListField(child=serializers.ChoiceField(choices=ride_rules.RIDE_PREFERENCE_OPTIONS),
                                        required=False)
    passenger_gender_pref = serializers.ChoiceField(choices=list(ride_rules.GENDER_PREFS), required=False)
    duration_seconds = serializers.IntegerField(required=False, default=0,
                                                help_text='Route duration from the map, for the arrival time.')


class PostRideSerializer(RideWriteSerializer):
    vehicle = serializers.CharField(required=False, allow_blank=True, default='',
                                    help_text='One of your verified vehicles (id).')
    recurring = RecurringInput(required=False, help_text='Leave out for a single ride.')


class SearchSerializer(serializers.Serializer):
    """Query parameters of GET /rides/search/. "from" is added in
    get_fields(): it can't be a class attribute name."""
    to = serializers.CharField(required=False, allow_blank=True, default='')
    origin_lat = serializers.FloatField(required=False, default=0.0)
    origin_lng = serializers.FloatField(required=False, default=0.0)
    dest_lat = serializers.FloatField(required=False, default=0.0)
    dest_lng = serializers.FloatField(required=False, default=0.0)
    date = serializers.CharField(required=False, allow_blank=True, default='', help_text='YYYY-MM-DD, local to tz.')
    tz = serializers.CharField(required=False, allow_blank=True, default='')
    seats = serializers.IntegerField(required=False)
    max_price_cents = serializers.IntegerField(required=False)
    time_slot = serializers.ChoiceField(choices=list(ride_rules.TIME_SLOT_HOURS), required=False, allow_blank=True)
    preference = serializers.ChoiceField(choices=ride_rules.RIDE_PREFERENCE_OPTIONS, required=False,
                                         allow_blank=True)

    def get_fields(self):
        fields = super().get_fields()
        fields = {'from': serializers.CharField(required=False, allow_blank=True, default='',
                                                help_text='Pick-up place name.'), **fields}
        return fields


class SummaryPassengerSerializer(serializers.Serializer):
    name = serializers.CharField()
    seats = serializers.IntegerField()
    pickup = serializers.CharField()
    contribution_cents = serializers.IntegerField()


class SummarySerializer(serializers.Serializer):
    ride = RideSerializer()
    reference = serializers.CharField()
    distance_km = serializers.IntegerField()
    duration = serializers.CharField()
    passenger_count = serializers.IntegerField()
    co2_saved_kg = serializers.FloatField()
    trees_equiv = serializers.FloatField()
    km_avoided = serializers.IntegerField()
    arrival_note = serializers.CharField()
    passengers = SummaryPassengerSerializer(many=True)
    total_contributions_cents = serializers.IntegerField()
    viewer_is_driver = serializers.BooleanField()


class DemandRiderSerializer(serializers.Serializer):
    id = serializers.CharField(help_text='Send as rider when inviting (POST /invites/).')
    name = serializers.CharField()
    initials = serializers.CharField()
    searching_since = serializers.CharField()
    rating_label = serializers.CharField()
    seats_needed = serializers.IntegerField()


class DemandRouteSerializer(serializers.Serializer):
    ride_id = serializers.CharField()
    from_location = serializers.CharField()
    to_location = serializers.CharField()
    heat = serializers.CharField()
    is_new = serializers.BooleanField()
    search_count = serializers.IntegerField()
    demand_label = serializers.CharField()
    demand_percent = serializers.IntegerField()
    most_searched_days = serializers.CharField(allow_null=True)
    avg_seats = serializers.CharField(allow_null=True)
    last_updated = serializers.CharField(allow_null=True)
    ride_date = serializers.CharField(allow_null=True)
    booked_seats = serializers.IntegerField()
    open_seats = serializers.IntegerField()
    riders = DemandRiderSerializer(many=True)


class DemandSerializer(serializers.Serializer):
    routes = DemandRouteSerializer(many=True)
    stats = serializers.DictField()
