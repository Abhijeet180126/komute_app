from hashids import Hashids
from django.conf import settings

_hashids = Hashids(salt=settings.HASHID_SALT, min_length=8)


def encode_id(pk):
    """Encode an integer primary key to a short opaque string safe for URLs."""
    return _hashids.encode(pk)


def decode_id(hashed):
    """Decode a hashid string back to its integer primary key, or None if invalid."""
    result = _hashids.decode(hashed)
    return result[0] if result else None


def decode_id_strict(value):
    """Like decode_id, but only for the exact string encode_id() makes (not
    a hashid of several numbers) and None for anything that isn't a string."""
    if not isinstance(value, str) or not value:
        return None
    pk = decode_id(value)
    if pk is None or encode_id(pk) != value:
        return None
    return pk
