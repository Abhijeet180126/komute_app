"""/api/v1/. Each module adds one include() line here (docs/api-modular-breakup.md)."""
from django.urls import include, path

from api.common.views import ApiDocsView, ApiSchemaView, health
from api.v1.users.views import media_file

urlpatterns = [
    # Module 0: foundation
    path('health/', health, name='health'),
    path('schema/', ApiSchemaView.as_view(), name='schema'),
    path('docs/', ApiDocsView.as_view(url_name='api:v1:schema'), name='docs'),
    # Module 1: auth and account
    path('auth/', include(('api.v1.auth.urls', 'auth'), namespace='auth')),
    # Module 2: profile, vehicles, photos
    path('users/', include(('api.v1.users.urls', 'users'), namespace='users')),
    path('vehicles/', include(('api.v1.vehicles.urls', 'vehicles'), namespace='vehicles')),
    path('media/<path:path>', media_file, name='media'),
]
