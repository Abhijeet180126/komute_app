import logging

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils import timezone

from komute.models import (
    Vehicle, Rides, RideStop, RideBooking, Message, PriceOffer,
    RideSearch, VerificationLog, Review, RideInvite, UserReport, UserBlock,
    BiometricConsent, LegalAcceptance, EmailLog,
)
from komute.tasks import send_later

User = get_user_model()
logger = logging.getLogger(__name__)


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
    #
    # Review queue (audit P-10, Law 25 s.12.1): filter "Review status" =
    # "Waiting for review". The photos aren't kept, so the reviewer works
    # from the checks, reasons, the driver's comment, the profile and the
    # vehicle photo, and can email the driver or ask them to verify again.
    list_display = ('id', 'user', 'result', 'review_status', 'review_source', 'stage_results',
                    'reason_codes', 'reviewer', 'created_at')
    list_filter = ('review_status', 'review_source', 'result')
    search_fields = ('user__email',)
    list_select_related = ('user', 'reviewer')
    fields = ('user', 'created_at', 'result', 'stage_results', 'reason_codes', 'face_score',
              'review_status', 'review_source', 'review_requested_at', 'driver_comment',
              'reviewer', 'reviewed_at', 'notes')
    readonly_fields = ('user', 'created_at', 'result', 'stage_results', 'reason_codes', 'face_score',
                       'review_status', 'review_source', 'review_requested_at', 'driver_comment',
                       'reviewer', 'reviewed_at')
    actions = ('approve_after_review', 'reject_after_review')

    def has_add_permission(self, request):
        return False

    def _decide(self, request, queryset, approve):
        done = 0
        for log in queryset.select_related('user'):
            if log.review_status != VerificationLog.REVIEW_PENDING:
                self.message_user(request, f'#{log.id} is not waiting for review; skipped.', messages.WARNING)
                continue
            user = log.user
            if approve:
                # Already verified by a later attempt: keep its date.
                if not user.driver_verified:
                    user.driver_verified = True
                    user.driver_verified_at = timezone.now()
                    user.reverify_reminder_sent_at = None
                    user.save(update_fields=['driver_verified', 'driver_verified_at', 'reverify_reminder_sent_at'])
                # Like a service approval, this covers the vehicle the check used.
                vehicle = user.vehicles.filter(is_primary=True).first() or user.vehicles.first()
                if vehicle and not vehicle.vehicle_verified:
                    vehicle.vehicle_verified = True
                    vehicle.save(update_fields=['vehicle_verified'])
            elif user.driver_verified:
                user.driver_verified = False
                user.save(update_fields=['driver_verified'])
            stamp = f'{timezone.localdate():%Y-%m-%d} {request.user.email}: {"approved" if approve else "rejected"} after review'
            log.result = VerificationLog.RESULT_APPROVED if approve else VerificationLog.RESULT_REJECTED
            log.review_status = VerificationLog.REVIEW_DONE
            log.reviewer = request.user
            log.reviewed_at = timezone.now()
            log.notes = f'{log.notes or ""}\n{stamp}'.strip()
            log.save(update_fields=['result', 'review_status', 'reviewer', 'reviewed_at', 'notes'])
            _send_review_result_email(request, log, approve)
            done += 1
        if done:
            self.message_user(request, f'{done} review(s) closed; the driver(s) have been emailed.')

    @admin.action(description='Approve after review (verify the driver and their vehicle)')
    def approve_after_review(self, request, queryset):
        self._decide(request, queryset, approve=True)

    @admin.action(description='Reject after review (driver stays unverified)')
    def reject_after_review(self, request, queryset):
        self._decide(request, queryset, approve=False)


REVIEW_APPROVED_EMAIL_BODY = """Hi {name},

A member of our team has reviewed your driver verification and approved it.
You can now post rides on Komüte.

{profile_url}

The Komüte team
"""

REVIEW_REJECTED_EMAIL_BODY = """Hi {name},

A member of our team has reviewed your driver verification and could not
confirm your identity from it, so you are not verified as a driver.

You can check that your profile details match your licence exactly and
verify again from your profile:
{profile_url}

If you think this is wrong, reply to this email and tell us why.

The Komüte team
"""


def _send_review_result_email(request, log, approved):
    user = log.user
    body = REVIEW_APPROVED_EMAIL_BODY if approved else REVIEW_REJECTED_EMAIL_BODY
    send_later(EmailMultiAlternatives(
        subject='[Komüte] Your driver verification has been reviewed',
        body=body.format(name=user.full_name or 'there',
                         profile_url=request.build_absolute_uri(reverse('profile'))),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
        reply_to=[settings.REPLY_TO_EMAIL_SUPPORT_CONTACT],
    ), EmailLog.KIND_REVIEW_RESULT, user)


@admin.register(BiometricConsent)
class BiometricConsentAdmin(admin.ModelAdmin):
    # Evidence of consent: viewable, never edited or added by hand.
    list_display = ('id', 'user', 'notice_version', 'accepted_at', 'ip_address')
    list_filter = ('notice_version',)
    search_fields = ('user__email',)
    readonly_fields = ('user', 'notice_version', 'ip_address', 'user_agent', 'accepted_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LegalAcceptance)
class LegalAcceptanceAdmin(admin.ModelAdmin):
    # Evidence of acceptance: viewable, never edited or added by hand.
    list_display = ('id', 'user', 'terms_version', 'privacy_version', 'source', 'accepted_at', 'ip_address')
    list_filter = ('source', 'terms_version', 'privacy_version')
    search_fields = ('user__email',)
    readonly_fields = ('user', 'terms_version', 'privacy_version', 'source',
                       'ip_address', 'user_agent', 'accepted_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    # Did a queued email go out (audit F-6)? Written by komute.tasks only.
    # "Sent" means SES accepted it; delivery is in the SES console, found
    # by message_id.
    list_display = ('created_at', 'kind', 'to_masked', 'user', 'subject', 'status', 'attempts', 'sent_at')
    list_filter = ('status', 'kind', 'created_at')
    search_fields = ('user__email', 'user__full_name', 'to_masked', 'subject', 'message_id')
    date_hierarchy = 'created_at'
    list_select_related = ('user',)
    readonly_fields = ('kind', 'user', 'to_masked', 'subject', 'status', 'attempts',
                       'last_error', 'message_id', 'created_at', 'sent_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('id', 'reviewer', 'reviewee', 'booking', 'rating', 'is_anonymous', 'created_at')
    list_filter = ('rating', 'is_anonymous')


@admin.register(RideInvite)
class RideInviteAdmin(admin.ModelAdmin):
    list_display = ('id', 'driver', 'passenger', 'ride', 'status', 'sent_at')
    list_filter = ('status',)


WARNING_EMAIL_BODY = """Hi {name},

We received a report about your activity on Komüte and have reviewed it.

Please take a moment to read our Community Guidelines:
{guidelines_url}

Repeated or serious breaches can lead to your account being suspended or
removed. If you think this is a mistake, reply to this email and tell us
what happened.

The Komüte team
"""


@admin.register(UserReport)
class UserReportAdmin(admin.ModelAdmin):
    """Report queue (ISSUES.md A1). Filter by status "Open" to work through
    new reports. Each action closes the report and records who handled it."""
    list_display = ('id', 'created_at', 'reported', 'reporter', 'reason', 'status', 'handled_by')
    list_filter = ('status', 'reason')
    search_fields = ('reported__email', 'reporter__email', 'details')
    date_hierarchy = 'created_at'
    fields = ('reported', 'reported_account', 'reporter', 'booking', 'reason', 'details',
              'created_at', 'status', 'staff_note', 'handled_by', 'updated_at')
    readonly_fields = ('reported', 'reported_account', 'reporter', 'booking', 'reason', 'details',
                       'created_at', 'handled_by', 'updated_at')
    actions = ('mark_reviewing', 'dismiss', 'warn_by_email', 'deactivate_reported_account')

    @admin.display(description='Reported account')
    def reported_account(self, obj):
        user = obj.reported
        count = UserReport.objects.filter(reported=user).count()
        state = 'active' if user.is_active else 'deactivated'
        return f'{state}; {count} report(s) in total'

    def save_model(self, request, obj, form, change):
        if change and 'status' in form.changed_data:
            obj.handled_by = request.user
        super().save_model(request, obj, form, change)

    def _close(self, request, queryset, status, note):
        stamp = f'{timezone.localdate():%Y-%m-%d} {request.user.email}: {note}'
        for report in queryset:
            report.status = status
            report.handled_by = request.user
            report.staff_note = f'{report.staff_note}\n{stamp}'.strip()
            report.save(update_fields=['status', 'handled_by', 'staff_note', 'updated_at'])

    @admin.action(description='Mark as reviewing')
    def mark_reviewing(self, request, queryset):
        queryset.update(status=UserReport.STATUS_REVIEWING, handled_by=request.user, updated_at=timezone.now())

    @admin.action(description='Dismiss (no action needed)')
    def dismiss(self, request, queryset):
        self._close(request, queryset, UserReport.STATUS_DISMISSED, 'dismissed')

    @admin.action(description='Warn the reported user by email')
    def warn_by_email(self, request, queryset):
        guidelines_url = request.build_absolute_uri(reverse('community-guidelines'))
        warned = set()
        for report in queryset.select_related('reported'):
            user = report.reported
            if user.id not in warned:
                try:
                    EmailMultiAlternatives(
                        subject='[Komüte] A note about your account',
                        body=WARNING_EMAIL_BODY.format(name=user.full_name or 'there', guidelines_url=guidelines_url),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        to=[user.email],
                        reply_to=[settings.REPLY_TO_EMAIL_SUPPORT_CONTACT],
                    ).send(fail_silently=False)
                except Exception:
                    logger.exception('Could not send a warning email to user %s', user.id)
                    self.message_user(request, f'Could not email {user.email}; report #{report.id} left open.', messages.ERROR)
                    continue
                warned.add(user.id)
            self._close(request, [report], UserReport.STATUS_ACTIONED, 'warned by email')
        if warned:
            self.message_user(request, f'Warning sent to {len(warned)} user(s).')

    @admin.action(description='Deactivate the reported account')
    def deactivate_reported_account(self, request, queryset):
        # is_active=False signs them out and blocks login. deactivated_at is
        # left empty so purge_deactivated_accounts doesn't delete the account
        # while the report may still be needed.
        reported_ids = set(queryset.values_list('reported_id', flat=True))
        count = User.objects.filter(id__in=reported_ids, is_staff=False).update(is_active=False)
        self._close(request, queryset, UserReport.STATUS_ACTIONED, 'account deactivated')
        self.message_user(request, f'Deactivated {count} account(s). Staff accounts are never deactivated here.')


@admin.register(UserBlock)
class UserBlockAdmin(admin.ModelAdmin):
    list_display = ('id', 'blocker', 'blocked', 'created_at')
    search_fields = ('blocker__email', 'blocked__email')
