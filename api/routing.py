"""The mobile app's WebSockets, under /ws/app/ (config/asgi.py adds them only
when API_ENABLED=True).

They sign in only by the X-Session-Token header and read no cookie. The
website's sockets check the Origin header because a browser attaches the
session cookie to a socket opened by any site; a browser can't set this
header on a WebSocket, so that attack doesn't apply here, and the app sends
no Origin at all."""
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from django.contrib.auth.models import AnonymousUser
from django.urls import re_path

from api.common.authentication import user_for_token
from api.v1.chat.consumers import AppChatConsumer

APP_SOCKET_PREFIX = '/ws/app/'
TOKEN_HEADER = b'x-session-token'

app_websocket_urlpatterns = [
    re_path(r'^ws/app/chat/(?P<token>[A-Za-z0-9_:\-\.]+)/$', AppChatConsumer.as_asgi()),
]


@database_sync_to_async
def _user_for(token):
    result = user_for_token(token) if token else None
    return result[0] if result else AnonymousUser()


class SessionTokenSocketAuth:
    """Sets scope['user'] from the X-Session-Token header. A missing or
    invalid token leaves it anonymous, and the consumer closes with 4401."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        token = dict(scope.get('headers', [])).get(TOKEN_HEADER, b'').decode('latin1')
        return await self.inner({**scope, 'user': await _user_for(token)}, receive, send)


app_sockets = SessionTokenSocketAuth(URLRouter(app_websocket_urlpatterns))


def with_app_sockets(website_sockets):
    """Sends /ws/app/ to the app's stack and everything else to the website's."""

    async def dispatch(scope, receive, send):
        target = app_sockets if scope['path'].startswith(APP_SOCKET_PREFIX) else website_sockets
        return await target(scope, receive, send)

    return dispatch
