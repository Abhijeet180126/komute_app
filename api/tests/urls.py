"""URLconf for the API tests: the website, the API (on whatever API_ENABLED
says), allauth.headless's app endpoints, and the test-only views."""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from api import urls as api_urls
from api.tests import views

test_patterns = [
    path('whoami/', views.whoami, name='whoami'),
    path('terms-gate/', views.terms_gate, name='terms-gate'),
    path('email-gate/', views.email_gate, name='email-gate'),
    path('driver-gate/', views.driver_gate, name='driver-gate'),
    path('acceptance/<str:hashid>/', views.AcceptanceDetail.as_view(), name='acceptance'),
    path('echo/', views.echo, name='echo'),
    path('numbers/', views.numbers, name='numbers'),
    path('boom/', views.boom, name='boom'),
    path('django-404/', views.django_404, name='django-404'),
    path('action-limited/', views.action_limited, name='action-limited'),
]

api_patterns = [
    path('v1/_test/', include((test_patterns, 'test'), namespace='test')),
    *api_urls.urlpatterns,
]

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path('_allauth/', include('allauth.headless.urls')),
    path('api/', include((api_patterns, 'api'), namespace='api')),
    path('', include('komute.urls')),
]
