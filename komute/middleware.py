from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
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


# Where pages may load scripts, styles, fonts, images and connections from
# (audit S-8). Every outside host here is one a template or static/js file
# uses: Leaflet from unpkg, OSM tiles, Unsplash photos on the
# about/how-it-works pages, Photon place search and OSRM routing. 'self'
# also covers the chat and notification websockets on the same host.
# 'unsafe-inline' stays until the inline <script> blocks and onclick=
# handlers are gone (patch 01, step 3). Add a host here before using it.
# Fonts are self-hosted from static/fonts (audit F-7), so Google is gone
# from everything but form-action (sign-in).
CONTENT_SECURITY_POLICY = '; '.join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://unpkg.com",
    "font-src 'self'",
    "img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org "
    "https://images.unsplash.com https://unpkg.com",
    "connect-src 'self' https://photon.komoot.io https://router.project-osrm.org",
    "media-src 'self' blob:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    # The Google/Apple sign-in buttons post here, then get redirected to
    # the provider; browsers check that redirect against form-action.
    "form-action 'self' https://accounts.google.com https://appleid.apple.com",
    "object-src 'none'",
    "report-uri /csp-report/",
])


class ContentSecurityPolicyMiddleware:
    """Adds the policy above to every response. Report-only (browsers send
    violations to /csp-report/ and block nothing) until CSP_ENFORCE is set."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        header = ('Content-Security-Policy' if settings.CSP_ENFORCE
                  else 'Content-Security-Policy-Report-Only')
        response.setdefault(header, CONTENT_SECURITY_POLICY)
        return response


def legal_versions_key():
    """The current Terms and Privacy Policy versions, as kept in the session
    once the signed-in user is known to have accepted them."""
    return f'{settings.TERMS_VERSION}|{settings.PRIVACY_VERSION}'


class LegalAcceptanceMiddleware:
    """Sends a signed-in user who hasn't accepted the current Terms and
    Privacy Policy (audit P-4) to the accept-terms page before anything else.
    That covers accounts created before acceptance was recorded, and every
    user after TERMS_VERSION or PRIVACY_VERSION changes.

    Left open: the legal pages themselves, logout, account deletion, contact,
    email confirmation, private media, robots.txt, the sitemap and the admin. Unnamed routes are
    either redirects to named ones or the old deactivate link. Websocket chat
    isn't covered, but the chat page that opens it is."""

    SESSION_KEY = 'legal_accepted_versions'
    EXEMPT_URL_NAMES = {
        'accept-terms', 'logout', 'terms-condition', 'privacy-policy',
        'community-guidelines', 'accessibility', 'contact-us', 'request-delete-account',
        'delete-account', 'confirm-delete-account', 'account_confirm_email', 'confirm-contact-email',
        'private-media', 'upload-too-large', 'csp-report', 'robots-txt', 'sitemap',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        match = request.resolver_match
        if (match is None or match.url_name is None
                or match.url_name in self.EXEMPT_URL_NAMES or 'admin' in match.namespaces):
            return None
        current = legal_versions_key()
        if request.session.get(self.SESSION_KEY) == current:
            return None
        from komute.models import LegalAcceptance
        if LegalAcceptance.objects.filter(
                user=user, terms_version=settings.TERMS_VERSION,
                privacy_version=settings.PRIVACY_VERSION).exists():
            request.session[self.SESSION_KEY] = current
            return None
        url = reverse('accept-terms')
        if request.method == 'GET':
            url += '?' + urlencode({'next': request.get_full_path()})
        return redirect(url)


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
