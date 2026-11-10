"""The app's live notice socket: the website's NotifyConsumer (the same
per-user group, so the website's booking flows reach it) behind the app's
token sign-in (api/routing.py).

While the app is open it gets {"type": "bookings_changed", "message": ...}
whenever a booking of theirs is requested, accepted, rejected or cancelled,
a ride of theirs is cancelled, an invite is accepted or a price is agreed,
and refreshes what it shows. On connect it gets {"type": "state",
"fingerprint": ...}: if that differs from the last one seen, refresh too.
Send {"type": "ping"} to keep the socket open; the answer is {"type": "pong"}.

Push notifications to a closed app are a separate, later part of module 6.

Close codes: 4401 not signed in, 4412 the current Terms aren't accepted."""
from channels.db import database_sync_to_async

from api.v1.chat.consumers import CLOSE_TERMS_NOT_ACCEPTED
from komute.consumers import NotifyConsumer, logger
from komute.services.accounts import has_accepted_current_terms


class AppNotifyConsumer(NotifyConsumer):

    async def connect(self):
        user = self.scope['user']
        if user.is_authenticated and not await database_sync_to_async(has_accepted_current_terms)(user):
            logger.warning('notify refused (%s): terms not accepted, user=%s', CLOSE_TERMS_NOT_ACCEPTED, user.id)
            await self.accept()
            await self.close(code=CLOSE_TERMS_NOT_ACCEPTED)
            return
        await super().connect()
