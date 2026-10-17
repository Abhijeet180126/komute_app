import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from komute.models import CustomUser, Message, RideSearch, UserBlock, Vehicle, VerificationLog
from komute.private_media import delete_stored_file as _delete_stored_file

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Fulfil confirmed account-deletion requests per privacy policy Section 8: '
        'ACCOUNT_DELETION_GRACE_DAYS after a user confirms deactivation (see '
        'confirm_delete_account_view), delete their ride search logs, '
        'verification records, chat messages and vehicles, and anonymize their '
        'profile. Rides/bookings/reviews involving other users are kept intact '
        '(the other party has a legitimate interest in that record — financial, '
        'dispute resolution) but display against an anonymized profile going '
        'forward, per the "unless legally required or permitted to retain" carve-out.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List matching accounts without changing anything',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        grace_days = settings.ACCOUNT_DELETION_GRACE_DAYS
        cutoff = timezone.now() - timedelta(days=grace_days)

        qs = CustomUser.objects.filter(
            is_active=False,
            deactivated_at__isnull=False,
            deactivated_at__lt=cutoff,
            data_purged_at__isnull=True,
        )

        if dry_run:
            count = qs.count()
            self.stdout.write(self.style.WARNING(
                f'[dry-run] {count} account(s) would be purged '
                f'(deactivated more than {grace_days} days ago, cutoff={cutoff.date()})'
            ))
            return

        purged = 0
        failed = 0
        purged_accounts = []  # (user_id, original_email) — for the admin notice below

        for user in qs:
            original_email = user.email
            try:
                with transaction.atomic():
                    RideSearch.objects.filter(user=user).delete()
                    VerificationLog.objects.filter(user=user).delete()
                    Message.objects.filter(Q(sender=user) | Q(receiver=user)).delete()
                    # Reports by or about them stay until purge_user_reports
                    # (safety records); blocks have no further use.
                    UserBlock.objects.filter(Q(blocker=user) | Q(blocked=user)).delete()

                    vehicles = Vehicle.objects.filter(owner=user)
                    for vehicle in vehicles:
                        _delete_stored_file(vehicle.photo_url)
                    vehicles.delete()

                    _delete_stored_file(user.selfie_url)
                    _delete_stored_file(user.license_front_url)
                    _delete_stored_file(user.license_back_url)
                    _delete_stored_file(user.avatar_url)

                    anon_email = f"deleted-user-{user.id}@komute.invalid"
                    user.full_name = "Deleted User"
                    user.email = anon_email
                    user.username = anon_email
                    user.pending_email = None
                    user.email_verified = False
                    user.phone = None
                    user.phone_verified = False
                    user.oauth_provider = None
                    user.oauth_subject = None
                    user.dob = None
                    user.gender = None
                    user.bio = None
                    user.avatar_url = None
                    user.driver_verified = False
                    user.license_number = None
                    user.license_province = None
                    user.license_expiry = None
                    user.license_front_url = None
                    user.license_back_url = None
                    user.selfie_url = None
                    user.face_match_score = None
                    user.set_unusable_password()
                    user.data_purged_at = timezone.now()
                    user.save()

                purged += 1
                purged_accounts.append((user.id, original_email))
            except Exception as e:
                failed += 1
                logger.error(
                    f'purge_deactivated_accounts failed for user {user.id}: {e}',
                    exc_info=True,
                )

        msg = (
            f'Purged {purged} account(s) deactivated more than {grace_days} days ago '
            f'(cutoff={cutoff.date()})' + (f'; {failed} failed, see log' if failed else '')
        )
        logger.info(msg)
        self.stdout.write(self.style.SUCCESS(msg) if not failed else self.style.WARNING(msg))

        if purged_accounts:
            self._notify_admin(purged_accounts, grace_days)

        if failed:
            raise SystemExit(1)

    def _notify_admin(self, purged_accounts, grace_days):
        lines = [
            f'{len(purged_accounts)} account(s) were permanently deleted/anonymized '
            f'({grace_days} days after deactivation confirmation):',
            '',
        ]
        lines += [f'- user id {uid}: {email}' for uid, email in purged_accounts]
        try:
            send_mail(
                subject=f'[Komüte] {len(purged_accounts)} account(s) purged',
                message='\n'.join(lines),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[settings.ADMIN_EMAIL],
            )
        except Exception:
            logger.warning(
                'purge_deactivated_accounts: could not send admin notification email',
                exc_info=True,
            )
