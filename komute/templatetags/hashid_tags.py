from django import template
from komute.hashid_utils import encode_id

register = template.Library()


@register.filter
def hashid(value):
    """Encode an integer ID to a hashid string. Usage: {{ obj.id|hashid }}"""
    try:
        return encode_id(int(value))
    except (ValueError, TypeError):
        return value
