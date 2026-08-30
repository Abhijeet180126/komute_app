import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from komute.models import Rides

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Delete cancelled rides (and cascading bookings/messages/stops/'
        'offers/invites) CANCELLED_RIDE_RETENTION_DAYS after cancellation. '
        'Anchored on cancelled_at; rides cancelled before this field '
        'existed have no cancelled_at and are left untouched by this job. '
        'Run after purge_old_messages so messages are purged on their own '
        '1-year clock first.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Count matching rides without deleting them',
        )

    def handle(self, *args, **options):
        retention_days = settings.CANCELLED_RIDE_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=retention_days)

        qs = Rides.objects.filter(
            status='cancelled',
            cancelled_at__isnull=False,
            cancelled_at__lt=cutoff,
        )

        if options['dry_run']:
            count = qs.count()
            self.stdout.write(
                self.style.WARNING(
                    f'[dry-run] {count} cancelled ride(s) would be deleted '
                    f'(cancelled more than {retention_days} days ago, '
                    f'cutoff={cutoff.date()})'
                )
            )
            return

        try:
            ride_count = qs.count()
            deleted_total, _ = qs.delete()
            msg = (
                f'Purged {ride_count} cancelled ride(s) older than '
                f'{retention_days} days (cutoff={cutoff.date()}), '
                f'{deleted_total} row(s) deleted total including cascades'
            )
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except Exception as e:
            logger.error(f'purge_cancelled_rides failed: {e}', exc_info=True)
            raise SystemExit(1)
