"""drf-spectacular extensions (loaded by api.apps.ApiConfig.ready)."""
from drf_spectacular.extensions import OpenApiAuthenticationExtension

from api.common.authentication import TOKEN_HEADER


class SessionTokenScheme(OpenApiAuthenticationExtension):
    target_class = 'api.common.authentication.SessionTokenAuthentication'
    name = 'sessionToken'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'apiKey',
            'in': 'header',
            'name': TOKEN_HEADER,
            'description': 'The session token returned by login.',
        }
