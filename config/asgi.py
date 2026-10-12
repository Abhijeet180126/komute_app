import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
import komute.routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = ProtocolTypeRouter({
    'http':      get_asgi_application(),
    # Only pages served from ALLOWED_HOSTS may open a socket. The session
    # cookie is sent cross-site too, so without this another website could
    # open a user's chat or notify socket (ISSUES.md L1).
    'websocket': AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(komute.routing.websocket_urlpatterns)
        )
    ),
})
