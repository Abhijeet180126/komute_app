from django.utils.cache import add_never_cache_headers


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
