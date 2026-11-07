"""Small formatting helpers used across services and views."""
import ipaddress

from komute.hashid_utils import encode_id

UNNAMED_USER = 'Komüte user'


def display_name(user):
    """How another person is named on pages and in emails: their name, never
    their email address (some older accounts have no name)."""
    return (user.full_name or '').strip().title() or UNNAMED_USER


def iso(value):
    return value.isoformat() if value else None


def ride_summary(ride):
    return {'ride_id': encode_id(ride.id), 'from': ride.origin, 'to': ride.destination,
            'departs_at': iso(ride.depart_at), 'status': ride.status}


def consent_details(ip, user_agent):
    """IP address and user agent stored with a consent or acceptance record.
    An address that doesn't parse is stored as null."""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        ip = None
    return {"ip_address": ip, "user_agent": (user_agent or "")[:300]}
