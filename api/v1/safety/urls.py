"""/api/v1/safety/ (module 7)."""
from django.urls import path

from api.v1.safety import views

urlpatterns = [
    path('reviews/', views.review, name='review'),
    path('reviews/pending/', views.pending_reviews, name='pending-reviews'),
    path('reports/', views.report, name='report'),
    path('blocks/', views.blocks, name='blocks'),
    path('blocks/<str:user_id>/', views.unblock, name='unblock'),
]
