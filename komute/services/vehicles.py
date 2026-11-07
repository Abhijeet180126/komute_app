"""A driver's vehicles (API module 2).

The website edits all vehicles at once as rows of the profile form
(komute/views.py profile_view, which uses the checks here); the API edits
one vehicle at a time with the functions below. Same rules either way:

- a plate registered to someone else is refused; one person's plates must
  differ (ignoring case);
- text fits its database column and the year is 1900 to next year;
- a new vehicle starts unverified, and a verified one goes back to
  unverified when its details or photo change (C3);
- verification checks the primary vehicle;
- removing a vehicle deletes its photo; its rides keep running without it."""
from django.db import transaction
from django.utils import timezone

from komute import private_media
from komute.models import Vehicle
from komute.private_media import delete_stored_file, save_private_image
from komute.services.common import norm_code
from komute.services.images import resize_image

DUPLICATE_PLATE_MSG = "Duplicate plate numbers are not allowed."

# Field limits are the database columns' (komute.models.Vehicle): a longer
# value or an out-of-range year used to end in a server error.
MIN_YEAR = 1900
_LABELS = (('make', 'Make'), ('model', 'Model'), ('color', 'Color'), ('plate', 'Plate'))


def max_length(field):
    return Vehicle._meta.get_field(field).max_length


def max_year():
    """Next year's models go on sale this year."""
    return timezone.localdate().year + 1


def fields_error(make, model, year, color, plate):
    """The message for the first value that doesn't fit, or None."""
    values = {'make': make, 'model': model, 'color': color, 'plate': plate}
    for field, label in _LABELS:
        limit = max_length(field)
        if len(values[field] or '') > limit:
            return f"{label} can be at most {limit} characters."
    if not MIN_YEAR <= year <= max_year():
        return f"Year must be between {MIN_YEAR} and {max_year()}."
    return None


def plate_taken_msg(plate):
    return f"Plate \"{plate}\" is already registered to another vehicle."


def plate_registered_elsewhere(user, plate):
    """Exact match, as the website has always checked (and the database's
    unique constraint enforces)."""
    return Vehicle.objects.filter(plate=plate).exclude(owner=user).exists()


def plate_error(user, plate, vehicle=None):
    """The website's message if `plate` can't be used for this user's
    vehicle (`vehicle` = the one being edited, or None for a new one)."""
    others = user.vehicles.all() if vehicle is None else user.vehicles.exclude(pk=vehicle.pk)
    if any(p.upper() == plate.upper() for p in others.values_list('plate', flat=True)):
        return DUPLICATE_PLATE_MSG
    if plate_registered_elsewhere(user, plate):
        return plate_taken_msg(plate)
    return None


def details_changed(vehicle, make, model, year, color, plate):
    """DL-Verification checked make, model, year, colour and plate against
    the photo; any real change voids that."""
    return (
        make.casefold() != (vehicle.make or "").casefold()
        or model.strip().casefold() != (vehicle.model or "").strip().casefold()
        or year != vehicle.year
        or color.strip().casefold() != (vehicle.color or "").strip().casefold()
        or norm_code(plate) != norm_code(vehicle.plate)
    )


def ordered(user):
    """The user's vehicles, primary first, then oldest first."""
    return user.vehicles.order_by('-is_primary', 'created_at', 'id')


@transaction.atomic
def create_vehicle(user, *, make, model, year, color, plate):
    """A new, unverified vehicle. The first one is the primary."""
    return Vehicle.objects.create(
        owner=user, make=make, model=model, year=year, color=color, plate=plate,
        # Verified only by a DL-Verification pass (C3).
        vehicle_verified=False,
        is_primary=not user.vehicles.exists(),
    )


def update_vehicle(vehicle, *, make, model, year, color, plate):
    """Saves new details. Returns True if that ended its verification."""
    changed = details_changed(vehicle, make, model, year, color, plate)
    was_verified = vehicle.vehicle_verified
    vehicle.make, vehicle.model, vehicle.year, vehicle.color, vehicle.plate = make, model, year, color, plate
    if was_verified and changed:
        vehicle.vehicle_verified = False
    vehicle.save()
    return was_verified and not vehicle.vehicle_verified


def set_vehicle_photo(vehicle, photo_file):
    """Resizes and stores a new photo. Raises ValidationError for a file that
    isn't a usable image, before changing anything. Returns True if that
    ended the vehicle's verification."""
    resized = resize_image(photo_file, label=f"user={vehicle.owner_id} vehicle={vehicle.plate!r}")
    was_verified = vehicle.vehicle_verified
    old_url = vehicle.photo_url
    vehicle.photo_url = save_private_image(private_media.KIND_VEHICLE, vehicle.owner_id, resized)
    vehicle.vehicle_verified = False
    vehicle.save(update_fields=["photo_url", "vehicle_verified"])
    delete_stored_file(old_url)
    return was_verified


@transaction.atomic
def set_primary(vehicle):
    """The vehicle verification checks, and the first one offered."""
    Vehicle.objects.filter(owner_id=vehicle.owner_id).exclude(pk=vehicle.pk).update(is_primary=False)
    if not vehicle.is_primary:
        vehicle.is_primary = True
        vehicle.save(update_fields=["is_primary"])


def delete_vehicle(vehicle):
    """Removes the vehicle and its photo. If it was the primary, the oldest
    remaining vehicle takes over."""
    owner_id, was_primary, photo_url = vehicle.owner_id, vehicle.is_primary, vehicle.photo_url
    with transaction.atomic():
        vehicle.delete()
        if was_primary:
            successor = Vehicle.objects.filter(owner_id=owner_id).order_by('created_at', 'id').first()
            if successor is not None:
                successor.is_primary = True
                successor.save(update_fields=["is_primary"])
    delete_stored_file(photo_url)
