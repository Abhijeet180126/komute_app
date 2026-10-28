import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from komute.models import Review, Rides

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Delete completed rides (and cascading bookings/messages/stops/'
        'offers/invites) COMPLETED_RIDE_RETENTION_DAYS after trip '
        'completion. Anchored on arrive_at, falling back to updated_at '
        'when arrive_at was never recorded. Run after purge_old_messages '
        'so messages are purged on their own 1-year clock first. Reviews '
        'of those rides go with them; reviews no longer linked to a booking '
        'are deleted the same time after they were written (audit P-18).'
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
        # Review.booking is SET_NULL, so a review would outlive its ride;
        # select reviews before the rides go.
        reviews = Review.objects.filter(
            Q(booking__ride__in=qs) | Q(booking__isnull=True, created_at__lt=cutoff)
        )

        if options['dry_run']:
            count = qs.count()
            self.stdout.write(
                self.style.WARNING(
                    f'[dry-run] {count} completed ride(s) and {reviews.count()} '
                    f'review(s) would be deleted '
                    f'(completed more than {retention_days} days ago, '
                    f'cutoff={cutoff.date()})'
                )
            )
            return

        try:
            review_count, _ = reviews.delete()
            ride_count = qs.count()
            deleted_total, _ = qs.delete()
            msg = (
                f'Purged {review_count} review(s) and {ride_count} completed ride(s) older than '
                f'{retention_days} days (cutoff={cutoff.date()}), '
                f'{deleted_total} row(s) deleted total including cascades'
            )
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except Exception as e:
            logger.error(f'purge_completed_rides failed: {e}', exc_info=True)
            raise SystemExit(1)
