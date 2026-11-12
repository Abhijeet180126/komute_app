from rest_framework import serializers

from api.common.fields import HashidField
from api.v1.bookings.serializers import PersonSerializer
from komute.models import UserReport
from komute.services import safety


class PendingReviewSerializer(serializers.Serializer):
    booking_id = HashidField(source='pk', read_only=True)
    ride_id = HashidField(read_only=True)
    origin = serializers.CharField(source='ride.origin')
    destination = serializers.CharField(source='ride.destination')
    depart_at = serializers.DateTimeField(source='ride.depart_at')
    reviewee = serializers.SerializerMethodField()
    reviewee_role = serializers.SerializerMethodField(help_text='driver (you rate your driver) or passenger.')

    def _reviewee(self, booking):
        return booking.passenger if booking.ride.driver_id == self.context['request'].user.id else booking.ride.driver

    def get_reviewee(self, booking) -> dict:
        return PersonSerializer(self._reviewee(booking)).data

    def get_reviewee_role(self, booking) -> str:
        return 'passenger' if booking.ride.driver_id == self.context['request'].user.id else 'driver'


class ReviewRequestSerializer(serializers.Serializer):
    booking = serializers.CharField(help_text='A booking from GET /safety/reviews/pending/.')
    rating = serializers.IntegerField(min_value=1, max_value=5,
                                      error_messages={'min_value': 'Rating must be between 1 and 5.',
                                                      'max_value': 'Rating must be between 1 and 5.'})
    comment = serializers.CharField(required=False, allow_blank=True, default='')
    anonymous = serializers.BooleanField(required=False, default=False)
    tags = serializers.ListField(child=serializers.ChoiceField(choices=safety.REVIEW_TAGS), required=False,
                                 default=list, help_text='Shown publicly on profiles.')
    subratings = serializers.DictField(child=serializers.IntegerField(min_value=1, max_value=5), required=False,
                                       default=dict,
                                       help_text=f'Optional 1-5 scores for: {", ".join(safety.SUBRATING_CATEGORIES)}.')

    def validate_subratings(self, value):
        unknown = set(value) - set(safety.SUBRATING_CATEGORIES)
        if unknown:
            raise serializers.ValidationError(f'Unknown categories: {", ".join(sorted(unknown))}.')
        return value


class TargetSerializer(serializers.Serializer):
    """Who it's about: a booking you're on (the other person), or a ride you
    can see (its driver). Never a user id."""
    booking = serializers.CharField(required=False, allow_blank=True, default='')
    ride = serializers.CharField(required=False, allow_blank=True, default='')


class ReportRequestSerializer(TargetSerializer):
    reason = serializers.ChoiceField(choices=[c for c, _ in UserReport.REASON_CHOICES],
                                     error_messages={'invalid_choice': 'Please choose a reason.'})
    details = serializers.CharField(required=False, allow_blank=True, default='',
                                    help_text=f'Up to {safety.REPORT_DETAILS_MAX_CHARS} characters; required for '
                                              '"other".')


class BlockedUserSerializer(serializers.Serializer):
    id = HashidField(source='blocked_id', read_only=True, help_text='Use to unblock.')
    name = serializers.SerializerMethodField()
    since = serializers.DateTimeField(source='created_at')

    def get_name(self, block) -> str:
        return (block.blocked.full_name or '').title() or 'Komüte user'
