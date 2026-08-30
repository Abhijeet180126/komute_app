import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from komute.models import CustomUser, VerificationLog

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Scrub biometric/OCR verification data per retention policy: null '
        'VerificationLog.face_score and CustomUser.face_match_score after '
        'FACE_SCORE_RETENTION_DAYS, null VerificationLog.ocr_extracted_json '
        'unconditionally, and delete VerificationLog rows after '
        'VERIFICATION_RECORD_RETENTION_DAYS.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Count affected rows/fields without changing anything',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        now = timezone.now()

        face_score_days = settings.FACE_SCORE_RETENTION_DAYS
        record_days = settings.VERIFICATION_RECORD_RETENTION_DAYS

        face_score_cutoff = now - timedelta(days=face_score_days)
        record_cutoff = now - timedelta(days=record_days)

        # Item 1: VerificationLog.face_score, anchored on each row's own created_at.
        face_score_qs = VerificationLog.objects.filter(
            created_at__lt=face_score_cutoff,
            face_score__isnull=False,
        )

        # Item 1: CustomUser.face_match_score, anchored on the user's most recent
        # VerificationLog.created_at (not CustomUser.updated_at, which bumps on
        # unrelated profile edits).
        stale_user_ids = list(
            VerificationLog.objects.values('user_id')
            .annotate(last_decision=Max('created_at'))
            .filter(last_decision__lt=face_score_cutoff)
            .values_list('user_id', flat=True)
        )
        face_match_qs = CustomUser.objects.filter(
            id__in=stale_user_ids,
            face_match_score__isnull=False,
        )

        # Item 2: VerificationLog.ocr_extracted_json, no grace period.
        ocr_qs = VerificationLog.objects.filter(ocr_extracted_json__isnull=False)

        # Item 3: full VerificationLog row delete once the outcome-only retention
        # window has passed. Runs last so items 1-2 have already nulled the
        # sensitive fields on any row that isn't old enough to be deleted outright.
        expired_qs = VerificationLog.objects.filter(created_at__lt=record_cutoff)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'[dry-run] {face_score_qs.count()} VerificationLog.face_score '
                f'would be nulled (older than {face_score_days} days, '
                f'cutoff={face_score_cutoff.date()})'
            ))
            self.stdout.write(self.style.WARNING(
                f'[dry-run] {face_match_qs.count()} CustomUser.face_match_score '
                f'would be nulled (last verification decision older than '
                f'{face_score_days} days)'
            ))
            self.stdout.write(self.style.WARNING(
                f'[dry-run] {ocr_qs.count()} VerificationLog.ocr_extracted_json '
                f'would be nulled (unconditional)'
            ))
            self.stdout.write(self.style.WARNING(
                f'[dry-run] {expired_qs.count()} VerificationLog row(s) would be '
                f'deleted (older than {record_days} days, '
                f'cutoff={record_cutoff.date()})'
            ))
            return

        try:
            with transaction.atomic():
                face_score_updated = face_score_qs.update(face_score=None)
                face_match_updated = face_match_qs.update(face_match_score=None)
                ocr_updated = ocr_qs.update(ocr_extracted_json=None)
                deleted, _ = expired_qs.delete()

            msg = (
                f'Scrubbed verification data: {face_score_updated} '
                f'VerificationLog.face_score field(s) nulled, '
                f'{face_match_updated} CustomUser.face_match_score field(s) '
                f'nulled, {ocr_updated} VerificationLog.ocr_extracted_json '
                f'field(s) nulled, {deleted} VerificationLog row(s) deleted '
                f'(older than {record_days} days).'
            )
            logger.info(msg)
            self.stdout.write(self.style.SUCCESS(msg))

        except Exception as e:
            logger.error(f'scrub_verification_data failed: {e}', exc_info=True)
            raise SystemExit(1)
