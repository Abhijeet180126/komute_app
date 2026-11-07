"""/api/v1/. Each module adds one include() line here (docs/api-modular-breakup.md)."""
from django.urls import path

from api.common.views import ApiDocsView, ApiSchemaView, health

urlpatterns = [
    # Module 0: foundation
    path('health/', health, name='health'),
    path('schema/', ApiSchemaView.as_view(), name='schema'),
    path('docs/', ApiDocsView.as_view(url_name='api:v1:schema'), name='docs'),
    # Module 1: path('auth/', include('api.v1.auth.urls')),
]
