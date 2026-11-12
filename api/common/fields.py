"""Ids in the API are the same hashids the website puts in its URLs
(/ride/aB3xK9pQ/), never raw database ids (komute.hashid_utils)."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from komute.hashid_utils import decode_id_strict, encode_id


def decode_hashid(value):
    """The primary key for a hashid, or None. Only the exact string
    encode_id() makes is accepted: decode_id() alone would also take a hashid
    of several numbers and return the first one."""
    return decode_id_strict(value)


@extend_schema_field(OpenApiTypes.STR)
class HashidField(serializers.Field):
    """An object's own id: shown as a hashid, read back to the primary key.
    Use with source='pk' (or 'id'), or on a ForeignKey's *_id attribute."""

    default_error_messages = {'invalid': 'Not a valid id.'}

    def to_representation(self, value):
        return encode_id(getattr(value, 'pk', value))

    def to_internal_value(self, data):
        pk = decode_hashid(data)
        if pk is None:
            self.fail('invalid')
        return pk


@extend_schema_field(OpenApiTypes.STR)
class HashidRelatedField(serializers.PrimaryKeyRelatedField):
    """A ForeignKey given and shown as a hashid. The error messages never
    repeat the database id."""

    default_error_messages = {
        'required': 'This field is required.',
        'does_not_exist': 'Not found.',
        'incorrect_type': 'Not a valid id.',
    }

    def to_internal_value(self, data):
        pk = decode_hashid(data)
        if pk is None:
            self.fail('incorrect_type')
        return super().to_internal_value(pk)

    def to_representation(self, value):
        return encode_id(value.pk)
