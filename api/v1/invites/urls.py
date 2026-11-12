"""/api/v1/invites/ (module 3)."""
from django.urls import path

from api.v1.invites import views

urlpatterns = [
    path('', views.invite_list, name='list'),
    path('by-token/<str:token>/', views.invite_by_token, name='by-token'),
    path('<str:invite_id>/', views.invite_detail, name='detail'),
    path('<str:invite_id>/accept/', views.accept, name='accept'),
    path('<str:invite_id>/decline/', views.decline, name='decline'),
]
