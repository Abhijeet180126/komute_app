import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import dateformat, timezone

from komute.models import CustomUser

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Periodic driver identity re-verification. Run daily. Emails drivers '
        'DRIVER_REVERIFY_REMINDER_DAYS before their verification is '
        'DRIVER_REVERIFY_DAYS old, and clears driver_verified once it is, or '
        'once the driver\'s license expiry date has passed. '
        'Posting rides and accepting bookings also check the date directly '
        '(CustomUser.driver_verification_current), so a late run never lets '
        'an expired driver through; this command keeps the badge and the '
        'stored flag in step and sends the reminder.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List matching drivers without emailing or changing anything',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        reverify_days = settings.DRIVER_REVERIFY_DAYS
        reminder_days = settings.DRIVER_REVERIFY_REMINDER_DAYS
        now = timezone.now()
        expire_cutoff = now - timedelta(days=reverify_days)
        remind_cutoff = now - timedelta(days=reverify_days - reminder_days)

        verified = CustomUser.objects.filter(is_active=True, driver_verified=True)
        # Lapsed period, or the license it was checked against has expired (C3).
        to_expire = verified.filter(
            Q(driver_verified_at__lte=expire_cutoff) | Q(license_expiry__lt=timezone.localdate())
        )
        to_remind = verified.filter(
            driver_verified_at__gt=expire_cutoff,
            driver_verified_at__lte=remind_cutoff,
            reverify_reminder_sent_at__isnull=True,
        ).exclude(id__in=to_expire.values('id'))

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'[dry-run] {to_remind.count()} driver(s) would get a reminder, '
                f'{to_expire.count()} would lose verification '
                f'(every {reverify_days} days, reminder {reminder_days} days before)'
            ))
            return

        reminded = 0
        failed = 0
        for user in to_remind:
            if self._send_reminder(user, reverify_days):
                user.reverify_reminder_sent_at = now
                user.save(update_fields=['reverify_reminder_sent_at'])
                reminded += 1
            else:
                failed += 1  # retried on the next run

        expired = to_expire.update(driver_verified=False, face_match_score=None)

        msg = (
            f'Sent {reminded} re-verification reminder(s), expired {expired} '
            f'driver verification(s)' + (f'; {failed} reminder(s) failed, see log' if failed else '')
        )
        logger.info(msg)
        self.stdout.write(self.style.SUCCESS(msg) if not failed else self.style.WARNING(msg))

    def _send_reminder(self, user, reverify_days):
        ctx = {
            'driver_name':   (user.full_name or user.email).title(),
            'due_display':   dateformat.format(user.driver_reverify_due_at, 'F j, Y'),
            'reverify_days': reverify_days,
        }
        try:
            html = render_to_string('account/email/reverify_reminder_driver.html', ctx)
            msg = EmailMultiAlternatives(
                subject='Please re-verify your identity on Komüte',
                body=(
                    f"Hi {ctx['driver_name']}, your Komüte identity verification expires on "
                    f"{ctx['due_display']}. Log in, open your Profile and click "
                    f"\"Re-verify Identity\" to take a new selfie."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[user.mail_to],
                reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_VERIFICATION],
            )
            msg.attach_alternative(html, 'text/html')
            msg.send()
            return True
        except Exception:
            logger.warning(
                'expire_driver_verification: could not send reminder to user %s',
                user.id, exc_info=True,
            )
            return False
