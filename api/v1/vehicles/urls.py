"""/api/v1/vehicles/ (module 2)."""
from django.urls import path

from api.v1.vehicles import views

urlpatterns = [
    path('', views.VehicleListView.as_view(), name='list'),
    path('<str:vehicle_id>/', views.VehicleDetailView.as_view(), name='detail'),
    path('<str:vehicle_id>/photo/', views.VehiclePhotoView.as_view(), name='photo'),
    path('<str:vehicle_id>/primary/', views.VehiclePrimaryView.as_view(), name='primary'),
]
