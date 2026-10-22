import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from django_q.models import Task

from komute.models import EmailLog

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Delete background job records and email log rows older than TASK_LOG_RETENTION_DAYS. '
        'A failed email job keeps the whole unsent email, address included (audit F-6).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Count matching job records without deleting them',
        )

    def handle(self, *args, **options):
        retention_days = settings.TASK_LOG_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=retention_days)

        qs = Task.objects.filter(stopped__lt=cutoff)
        # Retries end within half an hour, so a row this old is final even
        # if it still says queued (its job was lost).
        logs = EmailLog.objects.filter(created_at__lt=cutoff)

        if options['dry_run']:
            count, log_count = qs.count(), logs.count()
            self.stdout.write(
                self.style.WARNING(
                    f'[dry-run] {count} job record{"" if count == 1 else "s"} and '
                    f'{log_count} email log row{"" if log_count == 1 else "s"} would be deleted '
                    f'(older than {retention_days} days, cutoff={cutoff.date()})'
                )
            )
            return

        try:
            deleted, _ = qs.delete()
            log_deleted, _ = logs.delete()
            msg = (
                f'Purged {deleted} job record{"" if deleted == 1 else "s"} and '
                f'{log_deleted} email log row{"" if log_deleted == 1 else "s"} older than '
                f'{retention_days} days (cutoff={cutoff.date()})'
            )
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except Exception as e:
            logger.error(f'purge_task_log failed: {e}', exc_info=True)
            raise SystemExit(1)
