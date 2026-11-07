"""
Private storage for identity images (selfies, vehicle photos) — ISSUES.md C1.
Licence photos are no longer stored (audit P-5/P-13); KIND_LICENSE stays so
old files can still be found and deleted.

These used to live under MEDIA_ROOT at guessable paths
(licenses/license_<id>.jpg), which Nginx serves publicly at /media/.
They now live under PRIVATE_MEDIA_ROOT, which nothing serves directly, with
random filenames. The only way to fetch one is private_media_view, which
checks the requester is the owner (or staff) before handing the file off.

The *_url model fields keep their names and now hold
"<PRIVATE_MEDIA_URL><relative path>", e.g. "/private-media/licenses/12/<token>.jpg".
Legacy "<MEDIA_URL>..." values are still understood by the helpers below
until `manage.py move_private_media` has migrated them.
"""
import logging
import mimetypes
import posixpath
import secrets

from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage

logger = logging.getLogger(__name__)

KIND_LICENSE = "licenses"
KIND_SELFIE = "selfies"
KIND_VEHICLE = "vehicles"
KINDS = (KIND_LICENSE, KIND_SELFIE, KIND_VEHICLE)


def private_storage():
    # Built per call (cheap) rather than at import so override_settings works.
    return FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT, base_url=None)


def clean_rel_path(path):
    """Return a normalized storage-relative path, or None if it is unsafe
    (absolute, contains '..', backslashes, or isn't under a known kind)."""
    if not path or "\\" in path or "\x00" in path or path.startswith("/"):
        return None
    norm = posixpath.normpath(path)
    if norm != path or norm.startswith("..") or "/../" in f"/{norm}/":
        return None
    if norm.split("/", 1)[0] not in KINDS:
        return None
    return norm


def private_url(rel_path):
    return f"{settings.PRIVATE_MEDIA_URL}{rel_path}"


def resolve(url):
    """Map a stored *_url value to (storage, relative path), or (None, None)."""
    if not url:
        return None, None
    if url.startswith(settings.PRIVATE_MEDIA_URL):
        rel = clean_rel_path(url[len(settings.PRIVATE_MEDIA_URL):])
        return (private_storage(), rel) if rel else (None, None)
    if url.startswith(settings.MEDIA_URL):
        # Legacy public location, still readable until migrated.
        return default_storage, url[len(settings.MEDIA_URL):]
    return None, None


def open_stored_file(url):
    storage, rel = resolve(url)
    if storage is None:
        raise FileNotFoundError(url)
    return storage.open(rel, "rb")


def delete_stored_file(url):
    """Best-effort delete of the file a *_url field points at."""
    storage, rel = resolve(url)
    if storage is None:
        return
    try:
        if storage.exists(rel):
            storage.delete(rel)
    except Exception:
        logger.warning("could not delete stored file %r", rel, exc_info=True)


def save_private_image(kind, owner_id, content):
    """Save `content` under a fresh random name and return its private URL.
    A new name per upload also means browsers never show a stale cached copy."""
    if kind not in KINDS:
        raise ValueError(f"unknown private media kind: {kind}")
    name = f"{kind}/{owner_id}/{secrets.token_urlsafe(24)}.jpg"
    saved = private_storage().save(name, content)
    return private_url(saved)


def user_can_access(user, rel_path):
    """Owner of the image, or staff. Everyone else is denied."""
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    url = private_url(rel_path)
    if url in (user.selfie_url, user.avatar_url):
        return True
    return user.vehicles.filter(photo_url=url).exists()


def content_type_for(rel_path):
    return mimetypes.guess_type(rel_path)[0] or "application/octet-stream"


def file_response(rel_path):
    """The response that hands out a stored file the caller may see, or
    None if it doesn't exist. Used by the website's private_media_view and
    the API's media endpoint (same checks before either calls this)."""
    from django.http import FileResponse, HttpResponse

    storage = private_storage()
    if not storage.exists(rel_path):
        return None

    content_type = content_type_for(rel_path)
    if settings.PRIVATE_MEDIA_USE_ACCEL:
        # Nginx streams the file from its `internal` location.
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"{settings.PRIVATE_MEDIA_ACCEL_PREFIX}{rel_path}"
    else:
        response = FileResponse(storage.open(rel_path, "rb"), content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
