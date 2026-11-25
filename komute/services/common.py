"""Small formatting helpers used across services and views."""
import ipaddress
import re

from komute.hashid_utils import encode_id


def norm_name(value):
    """A name as the verification node compares it: case, hyphens and extra
    spaces ignored."""
    return " ".join((value or "").replace("-", " ").split()).casefold()


def norm_code(value):
    """A license or plate number with case, spaces and hyphens ignored."""
    return re.sub(r"[\s\-]", "", value or "").upper()

UNNAMED_USER = 'Komüte user'

# Shown when booking, posting or accepting an invite needs these first.
EMAIL_UNVERIFIED_MSG = (
    "Please verify your email address first: click \"Verify Now\" next to "
    "your email on your profile page."
)
DOB_MISSING_MSG = (
    "Please add your date of birth on your profile page first. "
    "KOMUTE is for adults aged 18 and over."
)


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
