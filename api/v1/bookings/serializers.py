"""Module 4 request and response shapes."""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.common.fields import HashidField
from api.v1.rides.serializers import RideSerializer
from komute.chat_tokens import make_chat_token
from komute.models import RideBooking
from komute.services.common import display_name


class PersonSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    name = serializers.SerializerMethodField()
    verified = serializers.BooleanField(source='driver_verified')
    rating = serializers.SerializerMethodField()

    def get_name(self, user) -> str:
        return display_name(user)

    def get_rating(self, user) -> float:
        return round(user.avg_rating or 0.0, 1)


class BookingSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    status = serializers.CharField(help_text='draft (a pre-request chat), requested, confirmed, rejected, '
                                             'cancelled or completed.')
    seats = serializers.IntegerField()
    agreed_price_cents = serializers.IntegerField(help_text='The total for all seats, service fee included.')
    pickup_stop = HashidField(source='pickup_stop_id', allow_null=True, read_only=True)
    dropoff_stop = HashidField(source='dropoff_stop_id', allow_null=True, read_only=True)
    booked_at = serializers.DateTimeField()
    confirmed_at = serializers.DateTimeField(allow_null=True)
    cancelled_at = serializers.DateTimeField(allow_null=True)
    rejected_at = serializers.DateTimeField(allow_null=True)
    ride = RideSerializer()
    passenger = PersonSerializer()
    chat_token = serializers.SerializerMethodField(help_text="This booking's chat (module 5).")
    driver_phone = serializers.SerializerMethodField(
        help_text='For the rider, once the booking is confirmed (privacy policy s.10).')

    def get_chat_token(self, booking) -> str:
        return make_chat_token(booking.id)

    def get_driver_phone(self, booking) -> str | None:
        viewer = self.context['request'].user
        if booking.passenger_id == viewer.id and booking.status == RideBooking.STATUS_CONFIRMED:
            return booking.ride.driver.phone or ''
        return None


class BookRequestSerializer(serializers.Serializer):
    ride = serializers.CharField(help_text='The ride id.')
    seats = serializers.IntegerField(required=False, default=1, help_text='1 to 8.')
    intent = serializers.ChoiceField(choices=['book', 'chat'], required=False, default='book',
                                     help_text='"chat": open a chat with the driver first (no seat held, the '
                                               'driver is told only once you write). "book": request the seats.')
    pickup_stop = serializers.CharField(required=False, allow_blank=True, default='',
                                        help_text="One of the ride's stops (id), or empty for the start.")
    dropoff_stop = serializers.CharField(required=False, allow_blank=True, default='')


class OfferSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    price_cents = serializers.IntegerField(help_text='Per seat.')
    status = serializers.CharField(help_text='pending, accepted, declined or countered.')
    proposed_by_me = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    responded_at = serializers.DateTimeField(allow_null=True)

    def get_proposed_by_me(self, offer) -> bool:
        return offer.proposed_by_id == self.context['request'].user.id


class OfferRequestSerializer(serializers.Serializer):
    price_cents = serializers.IntegerField(help_text='Per seat, 1 to 100000.')


class RespondOfferSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['accept', 'decline', 'counter'])
    price_cents = serializers.IntegerField(required=False, help_text='For "counter": the new price per seat.')


class InboxRideSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    origin = serializers.CharField()
    destination = serializers.CharField()
    depart_at = serializers.DateTimeField()
    status = serializers.CharField()


class LastMessageSerializer(serializers.Serializer):
    body = serializers.CharField()
    sent_at = serializers.DateTimeField(source='created_at')
    from_me = serializers.BooleanField()


class InboxRowSerializer(serializers.Serializer):
    booking_id = HashidField()
    booking_status = serializers.CharField()
    chat_token = serializers.CharField()
    ride = InboxRideSerializer()
    other = PersonSerializer(help_text='The rider (in a driver\'s inbox) or the driver.')
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.IntegerField()
    nego_status = serializers.CharField(help_text='agreed, pending or none.')
    nego_cents = serializers.IntegerField(allow_null=True, help_text='Agreed total, or the pending offer per seat.')
    nego_by_other = serializers.BooleanField(help_text='The pending offer came from the other person.')
    status = serializers.CharField(help_text='upcoming, active or completed.')
    blocked_by_me = serializers.BooleanField()

    @extend_schema_field(LastMessageSerializer(allow_null=True))
    def get_last_message(self, row):
        msg = row['last_msg']
        if msg is None:
            return None
        return {'body': msg.body, 'sent_at': msg.created_at, 'from_me': msg.sender_id == row['viewer_id']}
