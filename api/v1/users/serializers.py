from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.common.fields import HashidField
from api.common.media import media_url
from api.v1.vehicles.serializers import VehicleSerializer
from komute.services import profiles, vehicles


class DriverStatusSerializer(serializers.Serializer):
    driver_verified = serializers.BooleanField(help_text='Verified, and the verification is still current.')
    reverify_due_at = serializers.DateTimeField(allow_null=True)
    license_expired = serializers.BooleanField(help_text='Was verified against a licence that has since expired.')
    has_unverified_vehicle = serializers.BooleanField()
    reverify_soon = serializers.BooleanField()
    reverify_expired = serializers.BooleanField()


class ProfileSerializer(serializers.Serializer):
    """The signed-in user's own profile page."""
    id = HashidField(source='pk', read_only=True)
    email = serializers.EmailField()
    email_verified = serializers.BooleanField()
    pending_email = serializers.EmailField(allow_null=True)
    full_name = serializers.CharField()
    phone = serializers.CharField(allow_null=True, help_text='E.164, e.g. +14155552671.')
    country_code = serializers.SerializerMethodField()
    phone_number = serializers.SerializerMethodField()
    dob = serializers.DateField(allow_null=True)
    gender = serializers.CharField(allow_null=True)
    role = serializers.CharField()
    bio = serializers.CharField(allow_null=True)
    photo_url = serializers.SerializerMethodField(
        help_text='Load with the X-Session-Token header. Null if there is no photo.')
    uses_private_relay = serializers.BooleanField()
    contact_email = serializers.EmailField(allow_null=True)
    contact_email_confirmed = serializers.BooleanField()
    mail_to = serializers.EmailField(help_text='Where our emails go.')
    license_number = serializers.CharField(allow_null=True)
    license_province = serializers.CharField(allow_null=True)
    license_expiry = serializers.DateField(allow_null=True)
    driver_status = serializers.SerializerMethodField()
    vehicles = serializers.SerializerMethodField()

    def get_country_code(self, user) -> str:
        return profiles.split_phone(user.phone)[0]

    def get_phone_number(self, user) -> str:
        return profiles.split_phone(user.phone)[1]

    def get_photo_url(self, user) -> str | None:
        return media_url(self.context['request'], user.avatar_url)

    def _vehicles(self, user):
        if not hasattr(self, '_vehicle_list'):
            self._vehicle_list = list(vehicles.ordered(user))
        return self._vehicle_list

    @extend_schema_field(DriverStatusSerializer)
    def get_driver_status(self, user):
        return DriverStatusSerializer(
            profiles.driver_status(user, [v.vehicle_verified for v in self._vehicles(user)])).data

    @extend_schema_field(VehicleSerializer(many=True))
    def get_vehicles(self, user):
        return VehicleSerializer(self._vehicles(user), many=True, context=self.context).data


def _text(**kwargs):
    return serializers.CharField(required=False, allow_blank=True, **kwargs)


class ProfileUpdateSerializer(serializers.Serializer):
    """Send only what changes. The checks are the profile page's, run on the
    whole profile as it would be after the change."""
    full_name = _text()
    country_code = _text(help_text='Send with phone_number. Both empty removes the phone.')
    phone_number = _text()
    dob = _text(help_text='YYYY-MM-DD.')
    gender = serializers.ChoiceField(choices=list(profiles.GENDERS), required=False, allow_blank=True)
    role = _text(help_text='rider or driver. Driver needs at least one vehicle (POST /vehicles/).')
    bio = _text()
    license_number = _text(help_text='Saved for drivers only.')
    license_province = _text()
    license_expiry = _text(help_text='YYYY-MM-DD, not in the past.')


class ProfileResultSerializer(serializers.Serializer):
    profile = ProfileSerializer()
    warnings = serializers.ListField(child=serializers.CharField())


class ContactEmailSerializer(serializers.Serializer):
    contact_email = _text(default='', help_text='Empty removes it.')
