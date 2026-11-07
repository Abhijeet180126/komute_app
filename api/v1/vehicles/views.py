"""Module 2: the signed-in user's vehicles, /api/v1/vehicles/.

Rules from komute.services.vehicles, shared with the profile page. One
difference in shape, not in rules: the website edits every vehicle in one
form post and only as a driver; here each vehicle is added or changed on its
own, by any user, so the app can add a vehicle before switching to driver.
The rules that matter still hold: switching to driver needs a vehicle
(PATCH /users/me/), a driver can't remove their last one, and only verified
vehicles can be used for rides."""
from django.db import IntegrityError
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from api.common.exceptions import FieldError
from api.common.fields import decode_hashid
from api.common.permissions import HasAcceptedTerms
from api.v1.vehicles import serializers as s
from komute.services import profiles, vehicles
from rest_framework.permissions import IsAuthenticated

TAG = ['vehicles']
SIGNED_IN = [IsAuthenticated, HasAcceptedTerms]


def needs_verification_msg(plate):
    return (f"Vehicle {plate} needs to be verified before you can use it for rides. "
            "Make it your primary vehicle and verify your identity.")


def _vehicle_for(request, vehicle_id):
    """The user's own vehicle, else 404 (someone else's looks the same as none)."""
    pk = decode_hashid(vehicle_id)
    vehicle = request.user.vehicles.filter(pk=pk).first() if pk else None
    if vehicle is None:
        raise NotFound()
    return vehicle


def _result(request, vehicle, warnings, status_code=status.HTTP_200_OK):
    return Response(s.VehicleResultSerializer(
        {'vehicle': vehicle, 'warnings': warnings}, context={'request': request}).data, status=status_code)


def _check_plate(request, plate, vehicle=None):
    message = vehicles.plate_error(request.user, plate, vehicle)
    if message:
        code = 'duplicate_plate' if message == vehicles.DUPLICATE_PLATE_MSG else 'plate_taken'
        raise FieldError('plate', message, code=code)


class VehicleListView(APIView):
    permission_classes = SIGNED_IN

    @extend_schema(tags=TAG, summary='My vehicles', responses={200: s.VehicleSerializer(many=True)},
                   description='Primary first.')
    def get(self, request):
        return Response(s.VehicleSerializer(vehicles.ordered(request.user), many=True,
                                            context={'request': request}).data)

    @extend_schema(tags=TAG, summary='Add a vehicle', request=s.VehicleWriteSerializer,
                   responses={201: s.VehicleResultSerializer},
                   description='Starts unverified. The first vehicle becomes the primary. Error codes: '
                               'plate_taken, duplicate_plate (400).')
    def post(self, request):
        serializer = s.VehicleWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        _check_plate(request, data['plate'])
        try:
            vehicle = vehicles.create_vehicle(request.user, **data)
        except IntegrityError:  # registered by someone else a moment ago
            raise FieldError('plate', vehicles.plate_taken_msg(data['plate']), code='plate_taken')
        warnings = [needs_verification_msg(vehicle.plate)] if request.user.driver_verified else []
        return _result(request, vehicle, warnings, status.HTTP_201_CREATED)


class VehicleDetailView(APIView):
    permission_classes = SIGNED_IN

    @extend_schema(tags=TAG, summary='One vehicle', responses={200: s.VehicleSerializer})
    def get(self, request, vehicle_id):
        return Response(s.VehicleSerializer(_vehicle_for(request, vehicle_id), context={'request': request}).data)

    @extend_schema(tags=TAG, summary='Change a vehicle', request=s.VehicleWriteSerializer(partial=True),
                   responses={200: s.VehicleResultSerializer},
                   description='Send only the fields that change. Changing the make, model, year, colour '
                               'or plate of a verified vehicle makes it unverified again.')
    def patch(self, request, vehicle_id):
        vehicle = _vehicle_for(request, vehicle_id)
        current = {'make': vehicle.make, 'model': vehicle.model or '', 'year': vehicle.year,
                   'color': vehicle.color or '', 'plate': vehicle.plate}
        serializer = s.VehicleWriteSerializer(data={**current, **request.data})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data['plate'] != vehicle.plate:
            _check_plate(request, data['plate'], vehicle)
        try:
            unverified = vehicles.update_vehicle(vehicle, **data)
        except IntegrityError:
            raise FieldError('plate', vehicles.plate_taken_msg(data['plate']), code='plate_taken')
        return _result(request, vehicle, [needs_verification_msg(vehicle.plate)] if unverified else [])

    @extend_schema(tags=TAG, summary='Remove a vehicle', responses={204: None},
                   description='Also deletes its photo. Rides already posted with it keep running. Error code '
                               'last_vehicle (400): a driver must keep one vehicle.')
    def delete(self, request, vehicle_id):
        vehicle = _vehicle_for(request, vehicle_id)
        if request.user.role == 'driver' and request.user.vehicles.count() == 1:
            raise FieldError(None, profiles.DRIVER_NEEDS_VEHICLE_MSG, code='last_vehicle')
        vehicles.delete_vehicle(vehicle)
        return Response(status=status.HTTP_204_NO_CONTENT)


class VehiclePhotoView(APIView):
    permission_classes = SIGNED_IN

    @extend_schema(tags=TAG, summary='Upload a vehicle photo',
                   request={'multipart/form-data': {'type': 'object', 'properties': {
                       'photo': {'type': 'string', 'format': 'binary'}}, 'required': ['photo']}},
                   responses={200: s.VehicleResultSerializer},
                   description='multipart/form-data with a "photo" file. Replaces the old photo; a verified '
                               'vehicle becomes unverified.')
    def post(self, request, vehicle_id):
        vehicle = _vehicle_for(request, vehicle_id)
        photo = request.FILES.get('photo')
        if not photo or not photo.size:
            raise FieldError('photo', 'Choose a photo to upload.', code='required')
        try:
            unverified = vehicles.set_vehicle_photo(vehicle, photo)
        except DjangoValidationError as e:
            raise FieldError('photo', e.messages[0], code='invalid_image')
        return _result(request, vehicle, [needs_verification_msg(vehicle.plate)] if unverified else [])


class VehiclePrimaryView(APIView):
    permission_classes = SIGNED_IN

    @extend_schema(tags=TAG, summary='Make this the primary vehicle', request=None,
                   responses={200: s.VehicleSerializer(many=True)},
                   description='Driver verification checks the primary vehicle. Returns all vehicles.')
    def post(self, request, vehicle_id):
        vehicles.set_primary(_vehicle_for(request, vehicle_id))
        return Response(s.VehicleSerializer(vehicles.ordered(request.user), many=True,
                                            context={'request': request}).data)
