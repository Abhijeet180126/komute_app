import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from komute.models import Rides, RideBooking

logger = logging.getLogger(__name__)

# If the cron runs more than this long after a ride's estimated arrival,
# the delay is clearly a cron scheduling gap — don't overwrite arrive_at
# with a misleading timestamp.
MAX_CRON_LAG = timedelta(hours=2)


class Command(BaseCommand):
    help = 'Mark rides whose arrive_at has passed as completed and record actual arrival time'

    def handle(self, *args, **kwargs):
        now = timezone.now()

        try:
            qs = Rides.objects.filter(
                status__in=['posted', 'full'],
                depart_at__lte=now,
            ).filter(
                Q(arrive_at__lte=now) |
                Q(arrive_at__isnull=True, depart_at__lte=now - timedelta(hours=2)),
            )

            rides_updated = 0
            for ride in qs:
                ride.status = 'completed'

                # Record actual completion time so ride-summary can compare
                # real elapsed vs OSRM-expected duration.
                # Only overwrite arrive_at when the cron fired within MAX_CRON_LAG
                # of the estimated arrival — a longer gap means the cron was down
                # and "now" is not a meaningful arrival timestamp.
                estimated = ride.arrive_at or (ride.depart_at + timedelta(hours=2))
                if now - estimated <= MAX_CRON_LAG:
                    ride.arrive_at = now
                # else: keep the OSRM-estimated arrive_at as the best available value

                ride.save(update_fields=['status', 'arrive_at'])
                rides_updated += 1

            bookings_updated = RideBooking.objects.filter(
                ride__status='completed',
                status='confirmed',
            ).update(status='completed')

            msg = f"Rides completed: {rides_updated} | Bookings completed: {bookings_updated}"
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except Exception as e:
            logger.error(f"complete_rides failed: {e}", exc_info=True)
            raise SystemExit(1)
