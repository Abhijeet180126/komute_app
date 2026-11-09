"""/api/v1/bookings/ and /api/v1/offers/ (module 4). Fixed paths before <booking_id>."""
from django.urls import path

from api.v1.bookings import views

booking_patterns = [
    path('', views.booking_list, name='list'),
    path('on-my-rides/', views.bookings_on_my_rides, name='on-my-rides'),
    path('inbox/', views.inbox, name='inbox'),
    path('<str:booking_id>/', views.booking_detail, name='detail'),
    path('<str:booking_id>/cancel/', views.cancel, name='cancel'),
    path('<str:booking_id>/accept/', views.accept, name='accept'),
    path('<str:booking_id>/reject/', views.reject, name='reject'),
    path('<str:booking_id>/offers/', views.offers, name='offers'),
]

offer_patterns = [
    path('<str:offer_id>/respond/', views.respond, name='respond'),
]
