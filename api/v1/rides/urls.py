"""/api/v1/rides/ (module 3). Fixed paths come before <ride_id>."""
from django.urls import path

from api.v1.rides import views

urlpatterns = [
    path('', views.RideListView.as_view(), name='list'),
    path('mine/', views.my_rides, name='mine'),
    path('search/', views.search, name='search'),
    path('demand/', views.demand, name='demand'),
    path('<str:ride_id>/', views.RideDetailView.as_view(), name='detail'),
    path('<str:ride_id>/cancel/', views.cancel_ride, name='cancel'),
    path('<str:ride_id>/summary/', views.summary, name='summary'),
    path('<str:ride_id>/receipt/', views.receipt, name='receipt'),
]
