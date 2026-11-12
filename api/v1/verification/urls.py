"""/api/v1/verification/ (module 8)."""
from django.urls import path

from api.v1.verification import views

urlpatterns = [
    path('', views.verification_status, name='status'),
    path('notice/', views.notice, name='notice'),
    path('attempt/', views.verify, name='attempt'),
    path('review/', views.request_review, name='review'),
]
