from rest_framework import serializers

from api.common.fields import HashidField
from api.common.media import media_url
from komute.services import vehicles


class VehicleSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    make = serializers.CharField(read_only=True)
    model = serializers.CharField(read_only=True)
    year = serializers.IntegerField(read_only=True)
    color = serializers.CharField(read_only=True)
    plate = serializers.CharField(read_only=True)
    seat_capacity = serializers.IntegerField(read_only=True)
    photo_url = serializers.SerializerMethodField(
        help_text='Load with the X-Session-Token header. Null if there is no photo.')
    is_primary = serializers.BooleanField(
        read_only=True, help_text='The vehicle driver verification checks.')
    vehicle_verified = serializers.BooleanField(
        read_only=True, help_text='Only verified vehicles can be used for rides.')
    created_at = serializers.DateTimeField(read_only=True)

    def get_photo_url(self, vehicle) -> str | None:
        return media_url(self.context['request'], vehicle.photo_url)


class VehicleWriteSerializer(serializers.Serializer):
    """Lengths and year range are komute.services.vehicles' (the database
    columns), as on the profile page."""
    make = serializers.CharField(max_length=vehicles.max_length('make'))
    model = serializers.CharField(max_length=vehicles.max_length('model'), required=False, allow_blank=True,
                                  default='')
    year = serializers.IntegerField(help_text=f'{vehicles.MIN_YEAR} to next year.')
    color = serializers.CharField(max_length=vehicles.max_length('color'), required=False, allow_blank=True,
                                  default='')
    plate = serializers.CharField(max_length=vehicles.max_length('plate'))

    def validate_year(self, value):
        if not vehicles.MIN_YEAR <= value <= vehicles.max_year():
            raise serializers.ValidationError(
                f'Year must be between {vehicles.MIN_YEAR} and {vehicles.max_year()}.')
        return value


class VehicleResultSerializer(serializers.Serializer):
    vehicle = VehicleSerializer()
    warnings = serializers.ListField(child=serializers.CharField(),
                                     help_text='Show these to the user, e.g. "needs to be verified".')
