"""Root of the API. config/urls.py includes this under /api/ (namespace "api")
only when settings.API_ENABLED is True."""
from django.urls import include, path, re_path

from api.common.views import not_found

urlpatterns = [
    path('v1/', include(('api.v1.urls', 'v1'), namespace='v1')),
    # Anything else under /api/ answers a JSON 404, not the website's HTML
    # page, and isn't redirected to a trailing-slash URL (APPEND_SLASH).
    re_path(r'^.*$', not_found, name='not-found'),
]
