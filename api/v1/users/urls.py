"""/api/v1/users/ (module 2)."""
from django.urls import path

from api.v1.users import views

urlpatterns = [
    path('me/', views.ProfileView.as_view(), name='me'),
    path('me/photo/', views.profile_photo, name='me-photo'),
    path('me/contact-email/', views.set_contact_email, name='me-contact-email'),
]
