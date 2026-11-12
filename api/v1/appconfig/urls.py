"""/api/v1/app/ (module 9)."""
from django.urls import path

from api.v1.appconfig import views

urlpatterns = [
    path('config/', views.app_config, name='config'),
    path('contact/', views.contact_us, name='contact'),
]
