from django.utils.cache import add_never_cache_headers


# Browser features the site uses, for its own pages only: the camera for
# the verification selfie (profile) and geolocation for "Locate me"
# (search, post ride). Everything else is off, including for any embedded
# third-party content. clipboard-write is allowed by default in browsers.
PERMISSIONS_POLICY = ', '.join([
    'camera=(self)',
    'geolocation=(self)',
    'microphone=()',
    'payment=()',
    'usb=()',
    'serial=()',
    'bluetooth=()',
    'hid=()',
    'midi=()',
    'magnetometer=()',
    'gyroscope=()',
    'accelerometer=()',
    'display-capture=()',
    'browsing-topics=()',
])


class PermissionsPolicyMiddleware:
    """Adds the Permissions-Policy header above to every response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault('Permissions-Policy', PERMISSIONS_POLICY)
        return response


class NoStoreForSignedInUsersMiddleware:
    """Stop browsers from caching pages rendered for a signed-in user
    (ISSUES.md M3).

    Inboxes, my-rides, chat, the profile and booking details all show state
    that another user can change (a booking accepted, a ride cancelled).
    Without these headers the back button can show a cached copy with stale
    buttons, and after logout it can show the previous user's pages.

    Responses that already set Cache-Control (e.g. private media) keep it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, 'user', None)
        if (user is not None and user.is_authenticated
                and not response.has_header('Cache-Control')):
            add_never_cache_headers(response)
        return response
