"""Module 3: rides (/api/v1/rides/).

Every rule is komute.services.rides (shared with Post Ride, Edit Ride,
Cancel Ride, Search, the summary page, the receipt and Route Demand), with
the website's messages. Drivers post, edit and cancel their own rides;
riders search and view rides they could book."""
from datetime import date, timezone as dt_timezone

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import exceptions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.common.exceptions import FieldError
from api.common.fields import decode_hashid
from api.common.pagination import StandardPagination
from api.common.permissions import HasAcceptedTerms
from api.v1.rides import serializers as s
from komute.hashid_utils import encode_id
from komute.models import Rides, UserBlock
from komute.services import route_demand
from komute.services import rides as ride_rules
from komute.services.common import display_name
from komute.services.geo import zone_or_utc
from komute.services.receipts import build_receipt_pdf

TAG = ['rides']
SIGNED_IN = [IsAuthenticated, HasAcceptedTerms]


class NotAllowed(exceptions.PermissionDenied):
    """A rule of the website that refuses this user (message is the website's)."""

    def __init__(self, message, code):
        super().__init__(message, code=code)


def _ride_qs():
    return Rides.objects.select_related('driver', 'vehicle').prefetch_related('stops')


def _ride_id(ride_id):
    pk = decode_hashid(ride_id)
    if pk is None:
        raise exceptions.NotFound()
    return pk


def _own_ride(request, ride_id):
    return get_object_or_404(_ride_qs(), id=_ride_id(ride_id), driver=request.user)


def _tz(name):
    """(tz name to store or None, ZoneInfo). An unknown name is refused
    (the website quietly falls back to UTC)."""
    if not name:
        return None, zone_or_utc(None)
    zone = zone_or_utc(name)
    if zone.key != name and name != 'UTC':
        raise exceptions.ValidationError({'tz': ['Unknown time zone.']})
    return name, zone


def _aware(dt):
    """A value without an offset is UTC, as on the website."""
    return dt.replace(tzinfo=dt_timezone.utc) if timezone.is_naive(dt) else dt


def _stops_for_service(stops):
    """API stops → the (locations, lats, lngs) lists clean_stops reads."""
    return ([st['name'] for st in stops], [st['lat'] for st in stops], [st['lng'] for st in stops])


def _check_ride_fields(data, *, price_zero_msg, too_far_msg):
    """The checks Post Ride and Edit Ride share, in their order. Returns
    (errors, cleaned values)."""
    errors = {}
    origin, destination = data['origin'], data['destination']
    if error := ride_rules.endpoint_error(origin['name'].strip(), origin['lat'], origin['lng'], 'pickup',
                                          'Starting point is required.'):
        errors['origin'] = error
    if error := ride_rules.endpoint_error(destination['name'].strip(), destination['lat'], destination['lng'],
                                          'drop-off', 'Final destination is required.'):
        errors['destination'] = error
    clean_stops, stops_error = ride_rules.clean_stops(*_stops_for_service(data['stops']))
    if stops_error:
        errors['stops'] = stops_error

    seats = data.get('seats')
    seats, error = ride_rules.seats_error('' if seats is None else str(seats))
    if error:
        errors['seats'] = error

    price_cents = data.get('price_cents')
    if price_cents is None:
        errors['price_cents'] = 'Price per seat is required.'
    elif error := ride_rules.price_cents_error(price_cents, price_zero_msg):
        errors['price_cents'] = error

    depart_at = data.get('depart_at')
    if depart_at is None:
        errors['depart_at'] = 'Departure date and time are required.'
    else:
        depart_at = _aware(depart_at)
        if bad := ride_rules.departure_error(depart_at, too_far_msg):
            errors['depart_at'] = bad[1]
    return errors, {'clean_stops': clean_stops, 'seats': seats, 'price_cents': price_cents, 'depart_at': depart_at}


def _raise(errors):
    if errors:
        raise exceptions.ValidationError({field: [message] for field, message in errors.items()})


def _duration(data):
    return ride_rules.parse_duration_seconds(data.get('duration_seconds'))


class RideListView(APIView):
    permission_classes = SIGNED_IN

    @extend_schema(tags=TAG, summary='Post a ride (or a recurring ride)', request=s.PostRideSerializer,
                   responses={201: s.PostRideResultSerializer},
                   description='Same rules as Post Ride: a verified driver with a verified email, date of birth '
                               'and a verified vehicle; pick-up, drop-off and stops in the US/Canada outside '
                               'Quebec; departure 30 minutes to 4 months ahead. Error code cannot_post (403) '
                               'with the reason.')
    def post(self, request):
        user = request.user
        reason = ride_rules.post_ride_block_reason(user)
        if reason:
            raise NotAllowed(reason, 'cannot_post')

        serializer = s.PostRideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = {'origin': {'name': '', 'lat': 0.0, 'lng': 0.0},
                'destination': {'name': '', 'lat': 0.0, 'lng': 0.0}, 'stops': [],
                **serializer.validated_data}
        tz_name, driver_tz = _tz(data.get('tz'))

        errors, clean = _check_ride_fields(data, price_zero_msg=ride_rules.POST_PRICE_ZERO_MSG,
                                           too_far_msg=ride_rules.POST_TOO_FAR_MSG)
        vehicle = None
        vehicle_key = data.get('vehicle', '')
        if not vehicle_key:
            errors['vehicle'] = 'Please select a vehicle.'
        else:
            pk = decode_hashid(vehicle_key)
            vehicle = user.vehicles.filter(pk=pk).first() if pk else None
            if vehicle is None:
                errors['vehicle'] = 'Invalid vehicle selected.'
            elif not vehicle.vehicle_verified:
                errors['vehicle'] = "This vehicle isn't verified yet. Choose a verified vehicle."
                vehicle = None

        recurring = data.get('recurring')
        recurring_end, recurring_days = None, []
        if recurring is not None:
            recurring_errs, recurring_end, recurring_days = ride_rules.recurring_errors(
                recurring['end_date'], ','.join(str(d) for d in recurring['days']),
                clean['depart_at'] if 'depart_at' not in errors else None, driver_tz)
            errors.update({f'recurring.{k.replace("recurring_", "")}': v for k, v in recurring_errs.items()})
        _raise(errors)

        origin, destination = data['origin'], data['destination']
        first = ride_rules.create_rides(
            user, vehicle=vehicle, origin=origin['name'].strip(), origin_lat=origin['lat'], origin_lng=origin['lng'],
            destination=destination['name'].strip(), dest_lat=destination['lat'], dest_lng=destination['lng'],
            depart_at=clean['depart_at'], seats=clean['seats'], price_cents=clean['price_cents'],
            passenger_gender_pref=data.get('passenger_gender_pref', 'any'), notes=data.get('notes', '').strip(),
            preferences_json=data.get('preferences') or None, tz_name=tz_name, driver_tz=driver_tz,
            duration_seconds=_duration(data), stops=clean['clean_stops'],
            recurring_end=recurring_end if recurring is not None else None, recurring_days=recurring_days)
        count = (Rides.objects.filter(recurring_group_id=first.recurring_group_id).count()
                 if first.recurring_group_id else 1)
        first = _ride_qs().get(pk=first.pk)
        return Response(s.PostRideResultSerializer({'ride': first, 'rides_created': count},
                                                   context={'request': request}).data,
                        status=status.HTTP_201_CREATED)


@extend_schema(tags=TAG, summary='My posted rides', responses={200: s.RideSerializer(many=True)},
               parameters=[OpenApiParameter('status', str, description='posted, full, completed or cancelled. '
                                                                        'Default: upcoming (posted and full).')],
               description='Soonest first. Bookings on them are module 4.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def my_rides(request):
    qs = _ride_qs().filter(driver=request.user)
    wanted = request.query_params.get('status')
    if wanted in ('posted', 'full', 'completed', 'cancelled'):
        qs = qs.filter(status=wanted).order_by('-depart_at' if wanted in ('completed', 'cancelled') else 'depart_at')
    else:
        qs = qs.filter(status__in=('posted', 'full')).order_by('depart_at')
    paginator = StandardPagination()
    page = paginator.paginate_queryset(qs, request)
    return paginator.get_paginated_response(s.RideSerializer(page, many=True, context={'request': request}).data)


class RideDetailView(APIView):
    permission_classes = SIGNED_IN

    @extend_schema(tags=TAG, summary='One ride', responses={200: s.RideDetailSerializer},
                   description="Its driver sees it in any state. A rider sees it as on the booking page "
                               "(the rules for booking it are checked when booking). Error code riders_only "
                               "(403) for another driver; 404 when either has blocked the other.")
    def get(self, request, ride_id):
        ride = get_object_or_404(_ride_qs(), id=_ride_id(ride_id))
        if ride.driver_id != request.user.id:
            if request.user.role != 'rider':
                raise NotAllowed('Only riders can book rides.', 'riders_only')
            # Hidden from search when either has blocked the other (ISSUES.md A1).
            if UserBlock.objects.between(ride.driver_id, request.user.id):
                raise exceptions.NotFound()
        return Response(s.RideDetailSerializer(ride, context={'request': request}).data)

    @extend_schema(tags=TAG, summary='Edit my ride', request=s.RideWriteSerializer,
                   responses={200: s.RideSerializer},
                   description='Send only what changes (stops replace all stops). Same rules as Edit Ride: only '
                               'a posted ride with no requested or confirmed bookings. Error code cannot_edit (400).')
    def patch(self, request, ride_id):
        ride = _own_ride(request, ride_id)
        reason = ride_rules.edit_block_reason(ride)
        if reason:
            raise FieldError(None, reason, code='cannot_edit')

        serializer = s.RideWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sent = serializer.validated_data
        current = {
            'origin': {'name': ride.origin, 'lat': ride.origin_lat, 'lng': ride.origin_lng},
            'destination': {'name': ride.destination, 'lat': ride.dest_lat, 'lng': ride.dest_lng},
            'stops': [{'name': st.location, 'lat': st.lat, 'lng': st.lng}
                      for st in sorted(ride.stops.all(), key=lambda st: st.stop_order)],
            'depart_at': ride.depart_at, 'seats': ride.seats_total, 'price_cents': ride.price_cents,
            'notes': ride.notes or '', 'preferences': ride.preferences_json,
            'passenger_gender_pref': ride.passenger_gender_pref, 'tz': '', 'duration_seconds': 0,
        }
        data = {**current, **{k: v for k, v in sent.items() if k in self.request.data}}
        tz_name, _ = _tz(data.get('tz'))

        errors, clean = _check_ride_fields(data, price_zero_msg=ride_rules.EDIT_PRICE_ZERO_MSG,
                                           too_far_msg=ride_rules.EDIT_TOO_FAR_MSG)
        _raise(errors)
        origin, destination = data['origin'], data['destination']
        try:
            ride_rules.update_ride(
                ride, origin=origin['name'].strip(), origin_lat=origin['lat'], origin_lng=origin['lng'],
                destination=destination['name'].strip(), dest_lat=destination['lat'], dest_lng=destination['lng'],
                depart_at=clean['depart_at'], tz_name=tz_name, duration_seconds=_duration(data),
                seats=clean['seats'], price_cents=clean['price_cents'], notes=(data.get('notes') or '').strip(),
                preferences_json=data.get('preferences') or None,
                passenger_gender_pref=data.get('passenger_gender_pref') or 'any', stops=clean['clean_stops'])
        except ride_rules.EditBlocked as e:
            raise FieldError(None, e.reason, code='cannot_edit')
        return Response(s.RideSerializer(_ride_qs().get(pk=ride.pk), context={'request': request}).data)



@extend_schema(tags=TAG, summary='Cancel my ride', request=None, responses={200: s.RideSerializer},
               description='Every requested or confirmed booking is cancelled and its rider emailed, as on '
                           'the website. Error code cannot_cancel (400): already completed or cancelled, or departed.')
@api_view(['POST'])
@permission_classes(SIGNED_IN)
def cancel_ride(request, ride_id):
    ride = _own_ride(request, ride_id)
    error = ride_rules.cancel_error(ride)
    if error:
        raise FieldError(None, error, code='cannot_cancel')
    ride_rules.cancel_ride(ride)
    return Response(s.RideSerializer(_ride_qs().get(pk=ride.pk), context={'request': request}).data)


@extend_schema(tags=TAG, summary='Search rides', parameters=[s.SearchSerializer],
               responses={200: s.RideSerializer(many=True)},
               description='Riders only. Same matching as the website: within SEARCH_RADIUS_KM of the pick-up '
                           'and drop-off (or one of the ride\'s stops) when coordinates are sent, else by name; '
                           'blocked drivers and rides closed to the rider\'s gender are left out. A search '
                           'with a date is remembered for 90 days and shown to drivers on that route (Route '
                           'Demand). Paginated.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def search(request):
    if request.user.role != 'rider':
        raise NotAllowed('Only riders can search rides.', 'riders_only')
    params = s.SearchSerializer(data=request.query_params)
    params.is_valid(raise_exception=True)
    q = params.validated_data
    rider_tz = zone_or_utc(q['tz'])
    rider_today = timezone.localdate(timezone=rider_tz)

    errors = {}
    parsed_date = None
    if q['date']:
        try:
            parsed_date = date.fromisoformat(q['date'])
        except ValueError:
            errors['date'] = 'Invalid date.'
        else:
            if date_error := ride_rules.search_date_error(parsed_date, rider_today):
                errors['date'] = date_error
    errors.update(ride_rules.search_area_errors(q['from'], q['to'], q['origin_lat'], q['origin_lng'],
                                                q['dest_lat'], q['dest_lng']))
    _raise(errors)

    rides = []
    if q['from'] or q['to'] or parsed_date:
        # Clamped as on the website: no ride has more than 8 seats.
        seats = q.get('seats')
        seats_needed = min(max(seats, 1), 8) if seats else None
        max_cents = q.get('max_price_cents')
        rides = ride_rules.search_rides(
            request.user, search_from=q['from'], search_to=q['to'], parsed_date=parsed_date, rider_tz=rider_tz,
            seats_needed=seats_needed, max_price_str=f'{max_cents / 100:.2f}' if max_cents is not None else '',
            time_slot=q.get('time_slot') or '', preference=q.get('preference') or '',
            origin_lat=q['origin_lat'], origin_lng=q['origin_lng'], dest_lat=q['dest_lat'], dest_lng=q['dest_lng'])
    paginator = StandardPagination()
    page = paginator.paginate_queryset(rides, request)
    return paginator.get_paginated_response(s.RideSerializer(page, many=True, context={'request': request}).data)


@extend_schema(tags=TAG, summary='Completed ride summary', responses={200: s.SummarySerializer},
               description='Its driver, or a rider with a confirmed or completed booking. Error code '
                           'not_completed (400) until the ride is completed.')
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def summary(request, ride_id):
    ride = get_object_or_404(
        _ride_qs().prefetch_related('bookings__passenger', 'bookings__pickup_stop'), id=_ride_id(ride_id))
    is_driver, is_passenger = ride_rules.summary_viewer(ride, request.user)
    if not is_driver and not is_passenger:
        raise exceptions.NotFound()
    if ride.status != 'completed':
        raise FieldError(None, 'Ride summary is only available for completed rides.', code='not_completed')
    stats = ride_rules.summary_stats(ride)
    passengers = [{
        'name': display_name(b.passenger),
        'seats': b.seats,
        'pickup': b.pickup_stop.location.split(',')[0].strip() if b.pickup_stop else stats['origin_city'],
        'contribution_cents': b.agreed_price_cents,
    } for b in stats['active_bookings']]
    return Response(s.SummarySerializer({
        **stats, 'ride': ride, 'reference': stats['ride_ref'], 'passengers': passengers,
        'total_contributions_cents': stats['total_cents'], 'viewer_is_driver': is_driver,
    }, context={'request': request}).data)


@extend_schema(tags=TAG, summary='Ride receipt (PDF)',
               responses={(200, 'application/pdf'): OpenApiResponse(OpenApiTypes.BINARY)},
               description="The website's receipt. Its driver, or a rider with a confirmed or completed booking.")
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def receipt(request, ride_id):
    ride = get_object_or_404(
        ride_rules.receipt_rides(request.user).prefetch_related('stops', 'bookings__passenger',
                                                                  'bookings__pickup_stop'),
        id=_ride_id(ride_id))
    pdf, ride_ref = build_receipt_pdf(ride)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="Komute-Receipt-{ride_ref}.pdf"'
    return response


@extend_schema(tags=TAG, summary='Route demand', responses={200: s.DemandSerializer},
               description="Drivers only. Riders who searched near each of the driver's posted rides in the "
                           "last 7 days (not blocked, allowed by the ride's gender preference), most demand first.")
@api_view(['GET'])
@permission_classes(SIGNED_IN)
def demand(request):
    if request.user.role != 'driver':
        raise NotAllowed('Only drivers can see route demand.', 'drivers_only')
    built = route_demand.build(request.user)
    routes = []
    for d in built['route_demands']:
        routes.append({**d, 'riders': [{**r, 'id': encode_id(r['id'])} for r in d['riders']]})
    return Response(s.DemandSerializer({'routes': routes, 'stats': built['stats']}).data)
