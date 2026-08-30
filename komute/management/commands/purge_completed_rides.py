import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from komute.models import Rides

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Delete completed rides (and cascading bookings/messages/stops/'
        'offers/invites) COMPLETED_RIDE_RETENTION_DAYS after trip '
        'completion. Anchored on arrive_at, falling back to updated_at '
        'when arrive_at was never recorded. Run after purge_old_messages '
        'so messages are purged on their own 1-year clock first.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Count matching rides without deleting them',
        )

    def handle(self, *args, **options):
        retention_days = settings.COMPLETED_RIDE_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=retention_days)

        qs = Rides.objects.filter(status='completed').filter(
            Q(arrive_at__isnull=False, arrive_at__lt=cutoff) |
            Q(arrive_at__isnull=True, updated_at__lt=cutoff)
        )

        if options['dry_run']:
            count = qs.count()
            self.stdout.write(
                self.style.WARNING(
                    f'[dry-run] {count} completed ride(s) would be deleted '
                    f'(completed more than {retention_days} days ago, '
                    f'cutoff={cutoff.date()})'
                )
            )
            return

        try:
            ride_count = qs.count()
            deleted_total, _ = qs.delete()
            msg = (
                f'Purged {ride_count} completed ride(s) older than '
                f'{retention_days} days (cutoff={cutoff.date()}), '
                f'{deleted_total} row(s) deleted total including cascades'
            )
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except Exception as e:
            logger.error(f'purge_completed_rides failed: {e}', exc_info=True)
            raise SystemExit(1)
