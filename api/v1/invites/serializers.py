from rest_framework import serializers

from api.common.fields import HashidField
from api.v1.rides.serializers import RideSerializer
from komute.services import invites
from komute.services.common import display_name


class InviteSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    status = serializers.CharField(help_text='sent, viewed, accepted, declined or expired.')
    expired = serializers.SerializerMethodField(help_text='Past INVITE_EXPIRY_HOURS: it can no longer be accepted.')
    sent_at = serializers.DateTimeField()
    responded_at = serializers.DateTimeField(allow_null=True)
    driver_name = serializers.SerializerMethodField()
    rider_name = serializers.SerializerMethodField()
    ride = RideSerializer()

    def get_expired(self, invite) -> bool:
        return invites.invite_is_expired(invite)

    def get_driver_name(self, invite) -> str:
        return display_name(invite.driver)

    def get_rider_name(self, invite) -> str:
        return display_name(invite.passenger)


class SendInviteSerializer(serializers.Serializer):
    ride = serializers.CharField(help_text='One of your posted rides (id).')
    rider = serializers.CharField(help_text='A rider id from Route Demand (GET /rides/demand/).')


class AcceptedSerializer(serializers.Serializer):
    invite = InviteSerializer()
    booking_id = HashidField(help_text='The confirmed booking (module 4).')
    booking_status = serializers.CharField()
    seats = serializers.IntegerField()
    agreed_price_cents = serializers.IntegerField()
