import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from komute.models import UserReport

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Delete user reports (ISSUES.md A1) closed (actioned or dismissed) more '
        'than REPORT_RETENTION_DAYS ago. Open and reviewing reports are kept '
        'until staff close them.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Count matching reports without deleting them',
        )

    def handle(self, *args, **options):
        retention_days = settings.REPORT_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=retention_days)

        qs = UserReport.objects.filter(status__in=UserReport.CLOSED_STATUSES, updated_at__lt=cutoff)

        if options['dry_run']:
            count = qs.count()
            self.stdout.write(self.style.WARNING(
                f'[dry-run] {count} closed report(s) would be deleted '
                f'(closed more than {retention_days} days ago, cutoff={cutoff.date()})'
            ))
            return

        try:
            deleted, _ = qs.delete()
            msg = (
                f'Purged {deleted} report(s) closed more than {retention_days} days ago '
                f'(cutoff={cutoff.date()})'
            )
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))
        except Exception as e:
            logger.error(f'purge_user_reports failed: {e}', exc_info=True)
            raise SystemExit(1)
