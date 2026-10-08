import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from komute.models import CustomUser, Vehicle
from komute.private_media import (
    KIND_LICENSE, KIND_SELFIE, KIND_VEHICLE, KINDS, delete_stored_file, save_private_image,
)

logger = logging.getLogger(__name__)

USER_FIELDS = (
    ("license_front_url", KIND_LICENSE),
    ("license_back_url", KIND_LICENSE),
    ("selfie_url", KIND_SELFIE),
    ("avatar_url", KIND_SELFIE),
)


def _legacy_rel(url):
    """Storage-relative path of a legacy public /media/ identity image, else None."""
    if not url or not url.startswith(settings.MEDIA_URL):
        return None
    rel = url[len(settings.MEDIA_URL):]
    return rel if rel.split("/", 1)[0] in KINDS else None


def _walk(storage, top):
    try:
        dirs, files = storage.listdir(top)
    except FileNotFoundError:
        return
    for name in files:
        yield f"{top}/{name}"
    for d in dirs:
        yield from _walk(storage, f"{top}/{d}")


class Command(BaseCommand):
    help = (
        'One-off migration for ISSUES.md C1: move license, selfie and vehicle '
        'images from the public MEDIA_ROOT (/media/licenses|selfies|vehicles, '
        'guessable names) into PRIVATE_MEDIA_ROOT under random names, update '
        'the database URLs, and delete the public copies. Safe to re-run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be moved/deleted without changing anything',
        )
        parser.add_argument(
            '--delete-orphans',
            action='store_true',
            help='Also delete files left in media/licenses|selfies|vehicles that no '
                 'database row references (e.g. from vehicles deleted earlier)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        moved = missing = failed = 0
        referenced = set()

        def move(rel, kind, owner_id):
            """Copy one legacy file into private storage; return its new URL or None."""
            nonlocal moved, missing
            if not default_storage.exists(rel):
                missing += 1
                self.stdout.write(self.style.WARNING(f'missing on disk, left as is: {rel}'))
                return None
            moved += 1
            if dry_run:
                self.stdout.write(f'[dry-run] would move {rel}')
                return None
            with default_storage.open(rel, 'rb') as f:
                return save_private_image(kind, owner_id, ContentFile(f.read()))

        users = CustomUser.objects.all().only('id', *[f for f, _ in USER_FIELDS])
        for user in users:
            new_urls = {}  # legacy url -> new url (selfie and avatar share one file)
            changed = []
            try:
                for field, kind in USER_FIELDS:
                    url = getattr(user, field)
                    rel = _legacy_rel(url)
                    if rel is None:
                        continue
                    referenced.add(rel)
                    if url not in new_urls:
                        new_urls[url] = move(rel, kind, user.id)
                    if new_urls[url]:
                        setattr(user, field, new_urls[url])
                        changed.append(field)
                if changed:
                    user.save(update_fields=changed)
                    for old_url, new_url in new_urls.items():
                        if new_url:
                            delete_stored_file(old_url)
            except Exception:
                failed += 1
                logger.exception('move_private_media failed for user %s', user.id)
                self.stdout.write(self.style.ERROR(f'failed for user {user.id}, see log'))

        for vehicle in Vehicle.objects.exclude(photo_url__isnull=True).only('id', 'owner_id', 'photo_url'):
            rel = _legacy_rel(vehicle.photo_url)
            if rel is None:
                continue
            referenced.add(rel)
            try:
                new_url = move(rel, KIND_VEHICLE, vehicle.owner_id)
                if new_url:
                    old_url = vehicle.photo_url
                    vehicle.photo_url = new_url
                    vehicle.save(update_fields=['photo_url'])
                    delete_stored_file(old_url)
            except Exception:
                failed += 1
                logger.exception('move_private_media failed for vehicle %s', vehicle.id)
                self.stdout.write(self.style.ERROR(f'failed for vehicle {vehicle.id}, see log'))

        orphans = [
            rel for kind in KINDS for rel in _walk(default_storage, kind)
            if rel not in referenced
        ]
        deleted_orphans = 0
        for rel in orphans:
            if options['delete_orphans'] and not dry_run:
                default_storage.delete(rel)
                deleted_orphans += 1
            else:
                self.stdout.write(f'orphan (unreferenced) public file: {rel}')

        prefix = '[dry-run] ' if dry_run else ''
        verb = 'would be moved' if dry_run else 'moved'
        msg = (
            f'{prefix}{moved} file(s) {verb} to private storage; {missing} referenced '
            f'file(s) missing on disk; {len(orphans)} orphan public file(s) found'
            + (f', {deleted_orphans} deleted' if deleted_orphans else '')
            + (f'; {failed} failed, see log' if failed else '')
        )
        logger.info(msg)
        self.stdout.write(self.style.SUCCESS(msg) if not failed else self.style.WARNING(msg))
        if orphans and not options['delete_orphans']:
            self.stdout.write(self.style.WARNING(
                'Orphan files are still publicly reachable under /media/. '
                'Re-run with --delete-orphans to remove them.'
            ))
        if failed:
            raise SystemExit(1)
