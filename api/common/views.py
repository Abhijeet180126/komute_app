"""Module 0 endpoints: health check, OpenAPI schema and docs, JSON 404."""
from django.conf import settings
from drf_spectacular.utils import extend_schema, inline_serializer
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response

from api.common.authentication import SessionTokenAuthentication


@extend_schema(
    summary='Health check',
    description='Answers 200 when the API is up. No token needed.',
    auth=[],
    responses=inline_serializer('Health', {
        'status': serializers.CharField(),
        'api_version': serializers.CharField(),
    }),
)
@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    return Response({'status': 'ok', 'api_version': 'v1'})


@extend_schema(exclude=True)
@api_view(['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@authentication_classes([])
@permission_classes([AllowAny])
def not_found(request, *args, **kwargs):
    raise NotFound()


class _DocsAccess:
    """Staff only (signed in on the website, or by token), unless
    API_DOCS_PUBLIC. Only GET, so the website cookie needs no CSRF check."""

    authentication_classes = [SessionTokenAuthentication, SessionAuthentication]

    def get_permissions(self):
        if settings.API_DOCS_PUBLIC:
            return [AllowAny()]
        return [IsAdminUser()]


class ApiSchemaView(_DocsAccess, SpectacularAPIView):
    pass


class ApiDocsView(_DocsAccess, SpectacularSwaggerView):
    pass
