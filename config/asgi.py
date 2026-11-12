import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
import komute.routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

http_application = get_asgi_application()

# Only pages served from ALLOWED_HOSTS may open a socket. The session
# cookie is sent cross-site too, so without this another website could
# open a user's chat or notify socket (ISSUES.md L1).
website_sockets = AllowedHostsOriginValidator(
    AuthMiddlewareStack(
        URLRouter(komute.routing.websocket_urlpatterns)
    )
)


def build_websocket_application():
    """The website's sockets, plus the mobile app's under /ws/app/ only when
    API_ENABLED=True (docs/api-modular-breakup.md, module 5)."""
    from django.conf import settings
    if not settings.API_ENABLED:
        return website_sockets
    from api.routing import with_app_sockets
    return with_app_sockets(website_sockets)


application = ProtocolTypeRouter({
    'http':      http_application,
    'websocket': build_websocket_application(),
})
