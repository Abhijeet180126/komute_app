from hashids import Hashids
from django.conf import settings

_hashids = Hashids(salt=settings.SECRET_KEY, min_length=8)


def encode_id(pk):
    """Encode an integer primary key to a short opaque string safe for URLs."""
    return _hashids.encode(pk)


def decode_id(hashed):
    """Decode a hashid string back to its integer primary key, or None if invalid."""
    result = _hashids.decode(hashed)
    return result[0] if result else None
