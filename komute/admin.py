from django.contrib import admin
from django.contrib.auth import get_user_model

from komute.models import (
    Vehicle, Rides, RideStop, RideBooking, Message, PriceOffer,
    RideSearch, VerificationLog, Review, RideInvite,
)

User = get_user_model()


@admin.register(User)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = ('email', 'full_name', 'role', 'is_staff', 'is_active',
                     'email_verified', 'driver_verified', 'date_joined')
    list_filter = ('role', 'is_staff', 'is_active', 'email_verified', 'driver_verified')
    search_fields = ('email', 'full_name', 'phone')


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ('plate', 'owner', 'make', 'model', 'year', 'is_primary', 'vehicle_verified')
    list_filter = ('vehicle_verified', 'is_primary')
    search_fields = ('plate', 'make', 'model', 'owner__email')


@admin.register(Rides)
class RidesAdmin(admin.ModelAdmin):
    list_display = ('id', 'driver', 'origin', 'destination', 'depart_at',
                     'status', 'seats_available', 'price_cents')
    list_filter = ('status', 'passenger_gender_pref')
    search_fields = ('origin', 'destination', 'driver__email')
    date_hierarchy = 'depart_at'


@admin.register(RideStop)
class RideStopAdmin(admin.ModelAdmin):
    list_display = ('ride', 'stop_order', 'location', 'scheduled_at')


@admin.register(RideBooking)
class RideBookingAdmin(admin.ModelAdmin):
    list_display = ('id', 'ride', 'passenger', 'status', 'seats',
                     'agreed_price_cents', 'booked_at')
    list_filter = ('status',)
    search_fields = ('passenger__email',)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'ride', 'sender', 'receiver', 'created_at', 'read_at')
    search_fields = ('sender__email', 'receiver__email')


@admin.register(PriceOffer)
class PriceOfferAdmin(admin.ModelAdmin):
    list_display = ('id', 'booking', 'proposed_by', 'price_cents', 'status', 'created_at')
    list_filter = ('status',)


@admin.register(RideSearch)
class RideSearchAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'origin', 'destination', 'search_date', 'created_at')
    list_filter = ('search_date',)


@admin.register(VerificationLog)
class VerificationLogAdmin(admin.ModelAdmin):
    # ocr_extracted_json / face_score deliberately left off the list view --
    # they're encrypted at rest (biometric data); the review form still shows
    # them for the actual manual-review workflow, just not in a scrolling list.
    # stage_results / reason_codes hold no licence values (C3), so support
    # can see why an attempt failed straight from the list.
    list_display = ('id', 'user', 'result', 'stage_results', 'reason_codes', 'reviewer', 'created_at')
    list_filter = ('result',)
    search_fields = ('user__email',)


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'reviewer', 'reviewee', 'booking', 'rating', 'is_anonymous', 'created_at')
    list_filter = ('rating', 'is_anonymous')


@admin.register(RideInvite)
class RideInviteAdmin(admin.ModelAdmin):
    list_display = ('id', 'driver', 'passenger', 'ride', 'status', 'sent_at')
    list_filter = ('status',)
