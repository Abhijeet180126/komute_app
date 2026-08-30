from django.core.signing import dumps, loads, BadSignature, SignatureExpired

_SALT    = 'komute.chat'
_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def make_chat_token(booking_id: int) -> str:
    return dumps({'bid': booking_id}, salt=_SALT)


def resolve_chat_token(token: str):
    try:
        data = loads(token, salt=_SALT, max_age=_MAX_AGE)
        return int(data['bid'])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None
