"""Photo links for the app.

Stored photos (profile, vehicles) are private: the website's /private-media/
checks the website cookie. The app signs in by token, so it gets the same
files from /api/v1/media/<path>, with the same rule (owner or staff only)."""
from django.conf import settings
from django.urls import reverse


def media_url(request, stored_url):
    """An absolute URL the app can load `stored_url` from (with its token),
    or None if there is no photo."""
    if not stored_url:
        return None
    if stored_url.startswith(settings.PRIVATE_MEDIA_URL):
        rel_path = stored_url[len(settings.PRIVATE_MEDIA_URL):]
        return request.build_absolute_uri(reverse('api:v1:media', args=[rel_path]))
    # Legacy public files under MEDIA_URL (until move_private_media runs).
    return request.build_absolute_uri(stored_url)
