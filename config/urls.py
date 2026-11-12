"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import path, include
from django.views.static import serve

def api_urlpatterns():
    """The mobile app API, only when API_ENABLED=True (docs/api-modular-breakup.md).
    Off, nothing is registered and /api/... is a 404 as before."""
    if not settings.API_ENABLED:
        return []
    return [path('api/', include(('api.urls', 'api'), namespace='api'))]


urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    *api_urlpatterns(),
    path('', include('komute.urls')),
]

if settings.DEBUG:
    # In production, Nginx serves /static/ and /media/ directly (see docs/security-audit-report-2026-07-28.md, Finding 16).
    urlpatterns += [
        path('static/<path:path>', serve, {'document_root': settings.STATIC_ROOT}),
        path('media/<path:path>', serve, {'document_root': settings.MEDIA_ROOT}),
    ]

handler400 = 'komute.views.custom_400_view'
handler403 = 'komute.views.custom_403_view'
handler404 = 'komute.views.custom_404_view'
handler500 = 'komute.views.custom_500_view'
