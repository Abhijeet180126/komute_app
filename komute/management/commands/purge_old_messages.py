import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from komute.models import Message

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Delete chat messages MESSAGE_RETENTION_DAYS after their ride was '
        'completed or cancelled. Messages on rides still in progress '
        '(posted/full) are never purged by this job.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Count matching messages without deleting them',
        )

    def handle(self, *args, **options):
        retention_days = settings.MESSAGE_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=retention_days)

        # Completed rides: anchor on arrive_at, falling back to updated_at
        # when arrive_at was never recorded (mirrors the completed-ride
        # purge job's anchor logic).
        qs = Message.objects.filter(
            Q(
                ride__status='completed',
                ride__arrive_at__isnull=False,
                ride__arrive_at__lt=cutoff,
            ) | Q(
                ride__status='completed',
                ride__arrive_at__isnull=True,
                ride__updated_at__lt=cutoff,
            ) | Q(
                ride__status='cancelled',
                ride__cancelled_at__lt=cutoff,
            )
        )

        if options['dry_run']:
            count = qs.count()
            self.stdout.write(
                self.style.WARNING(
                    f'[dry-run] {count} message(s) would be deleted '
                    f'(ride completed/cancelled more than {retention_days} '
                    f'days ago, cutoff={cutoff.date()})'
                )
            )
            return

        try:
            deleted, _ = qs.delete()
            msg = (
                f'Purged {deleted} message(s) whose ride completed/cancelled '
                f'more than {retention_days} days ago (cutoff={cutoff.date()})'
            )
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except Exception as e:
            logger.error(f'purge_old_messages failed: {e}', exc_info=True)
            raise SystemExit(1)
