"""/api/v1/auth/ (module 1)."""
from django.urls import path

from api.v1.auth import views

urlpatterns = [
    path('signup/', views.signup, name='signup'),
    path('login/', views.login, name='login'),
    path('logout/', views.logout, name='logout'),
    path('session/', views.session, name='session'),
    path('social/token/', views.social_token, name='social-token'),
    path('social/signup/', views.social_signup, name='social-signup'),
    path('role/', views.role, name='role'),
    path('email/verify/', views.send_verification, name='email-verify'),
    path('email/change/', views.change_email, name='email-change'),
    path('password/forgot/', views.forgot_password, name='password-forgot'),
    path('password/reset/', views.reset_password, name='password-reset'),
    path('password/change/', views.change_password, name='password-change'),
    path('terms/', views.terms, name='terms'),
    path('account/delete/', views.delete_account, name='account-delete'),
    path('account/data/', views.my_data, name='account-data'),
]
