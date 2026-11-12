"""One error format for every API response that isn't a success:

    {"error": {"code": "invalid", "message": "...", "fields": {"email": ["..."]}}}

`code` is stable and meant for the app's logic; `message` is for showing to
the user; `fields` maps field names to their messages (empty unless the
request failed validation; errors not tied to one field are under
"non_field_errors")."""
import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler, set_rollback

logger = logging.getLogger('komute.api')

# Same wording as the website (komute.views._TOO_MANY_ATTEMPTS_MSG).
TOO_MANY_ATTEMPTS_MSG = "Too many attempts. Please try again later."
SERVER_ERROR_MSG = "Something went wrong on our side. Please try again."


class FieldError(exceptions.ValidationError):
    """A 400 on one field (or "non_field_errors") with its own error code,
    e.g. "incorrect_password", instead of the generic "invalid"."""

    def __init__(self, field, message, code='invalid'):
        super().__init__({field or 'non_field_errors': [message]})
        self.error_code = code


def error_body(code, message, fields=None, **extra):
    return {'error': {'code': code, 'message': message, 'fields': fields or {}, **extra}}


def _first_message(detail):
    """The first message in a (possibly nested) validation error."""
    if isinstance(detail, dict):
        for value in detail.values():
            found = _first_message(value)
            if found:
                return found
    elif isinstance(detail, (list, tuple)):
        for value in detail:
            found = _first_message(value)
            if found:
                return found
    elif detail:
        return str(detail)
    return None


def _as_fields(detail):
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, (list, tuple)):
        return {settings.REST_FRAMEWORK.get('NON_FIELD_ERRORS_KEY', 'non_field_errors'): list(detail)}
    return {'non_field_errors': [detail]}


def api_exception_handler(exc, context):
    # DRF converts these too, but keeps Django's message ("No Ride matches
    # the given query."), which names internals. Use the generic one.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)

    if response is None:
        # Not an API error (a bug or an outage). Logged in full; the app
        # gets JSON, not the website's HTML 500 page.
        logger.exception('Unhandled API error in %s', context.get('view').__class__.__name__,
                         exc_info=exc)
        set_rollback()
        return Response(error_body('server_error', SERVER_ERROR_MSG),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if isinstance(exc, exceptions.ValidationError):
        fields = _as_fields(exc.detail)
        body = error_body(getattr(exc, 'error_code', 'invalid'),
                          _first_message(exc.detail) or 'Please check the highlighted fields.', fields)
    elif isinstance(exc, exceptions.Throttled):
        body = error_body('throttled', TOO_MANY_ATTEMPTS_MSG, retry_after=exc.wait and int(exc.wait))
    else:
        codes = exc.get_codes()
        code = codes if isinstance(codes, str) else exc.default_code
        message = exc.detail if isinstance(exc.detail, str) else _first_message(exc.detail)
        body = error_body(code, str(message or exc.default_detail))

    response.data = body
    return response
