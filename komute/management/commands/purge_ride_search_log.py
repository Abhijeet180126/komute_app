import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from komute.models import RideSearch

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Delete ride search log entries older than RIDE_SEARCH_RETENTION_DAYS'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Count matching search log entries without deleting them',
        )

    def handle(self, *args, **options):
        retention_days = settings.RIDE_SEARCH_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=retention_days)

        qs = RideSearch.objects.filter(created_at__lt=cutoff)

        if options['dry_run']:
            count = qs.count()
            self.stdout.write(
                self.style.WARNING(
                    f'[dry-run] {count} ride search log entr{"y" if count == 1 else "ies"} would be deleted '
                    f'(older than {retention_days} days, cutoff={cutoff.date()})'
                )
            )
            return

        try:
            deleted, _ = qs.delete()
            msg = (
                f'Purged {deleted} ride search log entr{"y" if deleted == 1 else "ies"} older than '
                f'{retention_days} days (cutoff={cutoff.date()})'
            )
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except Exception as e:
            logger.error(f'purge_ride_search_log failed: {e}', exc_info=True)
            raise SystemExit(1)
