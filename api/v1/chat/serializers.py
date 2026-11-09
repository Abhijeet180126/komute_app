from rest_framework import serializers

from api.common.fields import HashidField
from api.v1.bookings.serializers import PersonSerializer


class ChatMessageSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    body = serializers.CharField()
    sender_id = HashidField(read_only=True)
    from_me = serializers.SerializerMethodField()
    sent_at = serializers.DateTimeField(source='created_at')
    read_at = serializers.DateTimeField(allow_null=True)

    def get_from_me(self, message) -> bool:
        return message.sender_id == self.context['request'].user.id


class PendingOfferSerializer(serializers.Serializer):
    id = HashidField(source='pk', read_only=True)
    price_cents = serializers.IntegerField(help_text='Per seat.')


class ChatStateSerializer(serializers.Serializer):
    booking_id = HashidField()
    booking_status = serializers.CharField()
    chat_token = serializers.CharField(help_text='Fresh on every call. Use it in socket_path.')
    socket_path = serializers.CharField(help_text='Open with wss:// on the API host, sending X-Session-Token.')
    is_driver = serializers.BooleanField()
    other = PersonSerializer()
    blocked_by_me = serializers.BooleanField()
    label = serializers.CharField(help_text='Badge text, e.g. Confirmed.')
    status_class = serializers.CharField(help_text='active, pending, completed or cancelled.')
    chat_locked = serializers.BooleanField(help_text='True: no more messages.')
    nego_locked = serializers.BooleanField(help_text='True: no price offers until the driver answers the request.')
    closed_message = serializers.CharField(allow_blank=True)
    ride_price_cents = serializers.IntegerField(help_text="The driver's price per seat.")
    agreed_price_cents = serializers.IntegerField(allow_null=True, help_text='Total for all seats, once agreed.')
    pending_incoming = PendingOfferSerializer(allow_null=True, help_text='Waiting for your answer.')
    pending_outgoing = PendingOfferSerializer(allow_null=True, help_text='Waiting for theirs.')
