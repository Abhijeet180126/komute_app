"""The app's chat socket: the website's ChatConsumer (same rules, same room,
so a browser and a phone talk to each other) in the API's conventions:
hashids instead of database ids, cents instead of dollars.

Client to server: {"type": "ping"}, {"type": "message", "body": "..."},
{"type": "price_offer", "price_cents": 2000},
{"type": "price_accepted", "offer_id": "..."}.

Close codes: 4401 not signed in, 4403 bad token or not this booking's chat,
4410 blocked, 4412 the current Terms aren't accepted."""
import json

from channels.db import database_sync_to_async

from api.common.fields import decode_hashid
from komute.consumers import ChatConsumer
from komute.hashid_utils import encode_id
from komute.services.accounts import has_accepted_current_terms

CLOSE_TERMS_NOT_ACCEPTED = 4412


def _cents(dollars):
    return int(round(dollars * 100))


class AppChatConsumer(ChatConsumer):

    async def connect(self):
        # The website checks this on the page that opens the socket; the app
        # has no such page.
        user = self.scope['user']
        if user.is_authenticated and not await database_sync_to_async(has_accepted_current_terms)(user):
            await self.refuse(CLOSE_TERMS_NOT_ACCEPTED, 'terms not accepted')
            return
        await super().connect()

    async def _error(self, code, message):
        await self.send(text_data=json.dumps({'type': 'error', 'code': code, 'message': message}))

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            await self._error('bad_request', 'Send JSON text frames.')
            return
        if len(text_data) > self.MAX_FRAME_BYTES:
            await super().receive(text_data)  # answers "too long"
            return
        try:
            data = json.loads(text_data)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            await self._error('bad_request', 'Send a JSON object.')
            return

        msg_type = data.get('type')
        if msg_type == 'message' and not isinstance(data.get('body', ''), str):
            await self._error('bad_request', 'body must be text.')
            return
        if msg_type == 'price_offer':
            cents = data.get('price_cents')
            valid = isinstance(cents, int) and not isinstance(cents, bool)
            data = {'type': msg_type, 'price': cents / 100 if valid else None}
        elif msg_type == 'price_accepted':
            offer_pk = decode_hashid(data.get('offer_id'))
            if offer_pk is None:
                await self._error('offer_stale', self.ERROR_MESSAGES['offer_stale'])
                return
            data = {'type': msg_type, 'offer_id': offer_pk}
        await super().receive(json.dumps(data))

    # ── what the app receives ──

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type':      'message',
            'id':        encode_id(event['message_id']),
            'body':      event['body'],
            'sender_id': encode_id(event['sender_id']),
            'from_me':   event['sender_id'] == self.scope['user'].id,
            'sent_at':   event['sent_at'],
        }))

    async def price_offer(self, event):
        await self.send(text_data=json.dumps({
            'type':        'price_offer',
            'offer_id':    encode_id(event['offer_id']),
            'price_cents': _cents(event['price']),
            'sender_id':   encode_id(event['sender_id']),
            'from_me':     event['sender_id'] == self.scope['user'].id,
        }))

    async def price_accepted(self, event):
        await self.send(text_data=json.dumps({
            'type':        'price_accepted',
            'price_cents': _cents(event['price']),
        }))
