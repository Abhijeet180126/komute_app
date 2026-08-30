import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        token = self.scope['url_route']['kwargs']['token']
        user  = self.scope['user']

        if not user.is_authenticated:
            await self.close()
            return

        booking_id = await self.resolve_token(token)
        if booking_id is None:
            await self.close()
            return

        booking_info = await self.get_booking_info(user, booking_id)
        if booking_info is None:
            await self.close()
            return

        self.booking_id  = booking_id
        self.ride_id     = booking_info['ride_id']
        self.receiver_id = booking_info['receiver_id']
        self.room_group  = f'chat_ride_{self.ride_id}'

        # Accept before any channel-layer work so the browser sees a proper
        # WebSocket handshake instead of an HTTP 403 that triggers onclose(1006).
        await self.accept()
        try:
            await self.channel_layer.group_add(self.room_group, self.channel_name)
        except Exception:
            await self.close(code=1011)

    async def disconnect(self, code):
        if hasattr(self, 'room_group'):
            await self.channel_layer.group_discard(self.room_group, self.channel_name)

    MAX_FRAME_BYTES = 8_192   # 8 KB raw WebSocket frame limit
    MAX_BODY_CHARS  = 2_000   # visible message character limit

    async def receive(self, text_data):
        if len(text_data) > self.MAX_FRAME_BYTES:
            await self.send(text_data=json.dumps({'type': 'error', 'message': 'Message too long (max 2000 characters).'}))
            return

        data = json.loads(text_data)
        msg_type = data.get('type')

        if msg_type == 'message':
            body = data.get('body', '').strip()
            if not body:
                return
            if len(body) > self.MAX_BODY_CHARS:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Message too long (max 2000 characters).'}))
                return
            user = self.scope['user']
            msg = await self.save_message(user, body)
            if msg is None:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'Messaging is disabled — this ride is no longer active.'}))
                return
            initials = ''.join(p[0].upper() for p in user.full_name.split()[:2])

            await self.channel_layer.group_send(self.room_group, {
                'type':      'chat_message',
                'body':      body,
                'sender_id': user.id,
                'initials':  initials,
                'time':      msg.created_at.strftime('%I:%M %p').lstrip('0'),
            })

        elif msg_type == 'price_offer':
            user = self.scope['user']
            offer = await self.save_price_offer(user, self.booking_id, data.get('price'))
            if offer is None:
                await self.send(text_data=json.dumps({'type': 'error', 'message': 'This booking is no longer active.'}))
                return
            await self.channel_layer.group_send(self.room_group, {
                'type':      'price_offer',
                # Broadcast the server-recorded price, not the client's raw
                # input — the latter is attacker-controlled and gets rendered
                # into the DOM (and an inline onclick handler) on both peers'
                # screens.
                'price':     offer.price_cents / 100,
                'offer_id':  offer.id,
                'sender_id': user.id,
            })

        elif msg_type == 'price_accepted':
            price = data.get('price')
            offer_id = data.get('offer_id')
            result, agreed_price = await self.accept_price_offer(self.booking_id, offer_id, price)
            if result != 'ok':
                messages_by_code = {
                    'booking_inactive': 'This booking is no longer active — it may have been cancelled or rejected.',
                    'offer_stale':      'That price is no longer available — the other side may have sent a new offer. Check the latest price and try again.',
                    'no_seats':         'Price agreed, but this ride no longer has an open seat to confirm your booking. Please contact the driver.',
                }
                message = messages_by_code.get(result, 'Something went wrong.')
                await self.send(text_data=json.dumps({'type': 'error', 'code': result, 'message': message}))
                return
            await self.channel_layer.group_send(self.room_group, {
                'type':  'price_accepted',
                # Same rationale as price_offer above — broadcast the price
                # resolved from the accepted offer, never the raw client value.
                'price': agreed_price,
            })

    # ── handlers (called by group_send) ──

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type':      'message',
            'body':      event['body'],
            'sender_id': event['sender_id'],
            'initials':  event['initials'],
            'time':      event['time'],
        }))

    async def price_offer(self, event):
        await self.send(text_data=json.dumps({
            'type':      'price_offer',
            'price':     event['price'],
            'offer_id':  event.get('offer_id'),
            'sender_id': event['sender_id'],
        }))

    async def price_accepted(self, event):
        await self.send(text_data=json.dumps({
            'type':  'price_accepted',
            'price': event['price'],
        }))

    # ── DB helpers ──

    @database_sync_to_async
    def resolve_token(self, token):
        from .chat_tokens import resolve_chat_token
        return resolve_chat_token(token)

    @database_sync_to_async
    def get_booking_info(self, user, booking_id):
        from .models import RideBooking
        try:
            booking = RideBooking.objects.select_related('ride__driver', 'passenger').get(id=booking_id)
        except RideBooking.DoesNotExist:
            return None
        if user not in (booking.ride.driver, booking.passenger):
            return None
        receiver = booking.passenger if user == booking.ride.driver else booking.ride.driver
        return {
            'ride_id':     booking.ride_id,
            'receiver_id': receiver.id,
        }

    @database_sync_to_async
    def save_message(self, user, body):
        from .models import Message, Rides, RideBooking
        from django.contrib.auth import get_user_model
        User = get_user_model()
        try:
            booking = RideBooking.objects.get(id=self.booking_id)
        except RideBooking.DoesNotExist:
            return None
        if booking.status not in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
            return None
        ride     = Rides.objects.get(id=self.ride_id)
        receiver = User.objects.get(id=self.receiver_id)
        return Message.objects.create(
            ride=ride,
            sender=user,
            receiver=receiver,
            body=body,
        )

    @database_sync_to_async
    def save_price_offer(self, user, booking_id, price):
        from .models import RideBooking, PriceOffer
        booking = RideBooking.objects.get(id=booking_id)
        if booking.status not in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
            return None
        PriceOffer.objects.filter(
            booking=booking,
            proposed_by=user,
            status=PriceOffer.STATUS_PENDING,
        ).update(status=PriceOffer.STATUS_DECLINED, responded_at=timezone.now())
        return PriceOffer.objects.create(
            booking=booking,
            proposed_by=user,
            price_cents=int(round(float(price) * 100)),
        )

    @database_sync_to_async
    def accept_price_offer(self, booking_id, offer_id, price):
        from .models import RideBooking, PriceOffer, Rides
        from django.conf import settings
        from django.db import transaction
        from django.db.models import F

        booking = RideBooking.objects.select_related('ride', 'passenger').filter(id=booking_id).first()
        if booking is None or booking.status not in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED, RideBooking.STATUS_CONFIRMED):
            return 'booking_inactive', None

        # Prefer the specific offer the client had on screen — matching by price
        # alone is ambiguous once a proposer has superseded their own pending
        # offer with a newer one (same price could belong to a stale offer).
        if offer_id is not None:
            offer = PriceOffer.objects.filter(id=offer_id, booking_id=booking_id).first()
        else:
            price_cents = int(round(float(price) * 100))
            offer = (
                PriceOffer.objects
                .filter(booking_id=booking_id, price_cents=price_cents, status=PriceOffer.STATUS_PENDING)
                .order_by('-created_at')
                .first()
            )
        if offer is None or offer.status != PriceOffer.STATUS_PENDING:
            return 'offer_stale', None

        agreed_price_cents = offer.price_cents * booking.seats + settings.SERVICE_FEE_CENTS
        was_unconfirmed = booking.status in (RideBooking.STATUS_DRAFT, RideBooking.STATUS_REQUESTED)

        with transaction.atomic():
            if booking.status == RideBooking.STATUS_DRAFT:
                # A pre-request chat ("Chat with driver") never went through
                # "Request to book", so no seat was ever reserved — agreeing on
                # a price here IS the booking, so reserve the seat now.
                reserved = Rides.objects.filter(
                    id=booking.ride_id, seats_available__gte=booking.seats,
                ).update(seats_available=F('seats_available') - booking.seats)
                if not reserved:
                    return 'no_seats', None
                Rides.objects.filter(id=booking.ride_id, seats_available=0).update(status='full')
                booking.ride.seats_available -= booking.seats

            offer.status = PriceOffer.STATUS_ACCEPTED
            offer.responded_at = timezone.now()
            offer.save(update_fields=['status', 'responded_at'])

            update_fields = {'agreed_price_cents': agreed_price_cents}
            if was_unconfirmed:
                update_fields['status'] = RideBooking.STATUS_CONFIRMED
                update_fields['confirmed_at'] = timezone.now()
            RideBooking.objects.filter(id=booking_id).update(**update_fields)

        if was_unconfirmed:
            booking.agreed_price_cents = agreed_price_cents
            booking.status = RideBooking.STATUS_CONFIRMED
            import threading
            from .views import _send_booking_confirmation_emails
            threading.Thread(target=_send_booking_confirmation_emails, args=(booking,), daemon=True).start()

        return 'ok', offer.price_cents / 100
