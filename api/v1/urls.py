"""/api/v1/. Each module adds one include() line here (docs/api-modular-breakup.md)."""
from django.urls import include, path

from api.common.views import ApiDocsView, ApiSchemaView, health
from api.v1.bookings import urls as booking_urls
from api.v1.users.views import media_file

urlpatterns = [
    # Module 0: foundation
    path('health/', health, name='health'),
    path('schema/', ApiSchemaView.as_view(), name='schema'),
    path('docs/', ApiDocsView.as_view(url_name='api:v1:schema'), name='docs'),
    # Module 1: auth and account
    path('auth/', include(('api.v1.auth.urls', 'auth'), namespace='auth')),
    # Module 2: profile, vehicles, photos
    path('users/', include(('api.v1.users.urls', 'users'), namespace='users')),
    path('vehicles/', include(('api.v1.vehicles.urls', 'vehicles'), namespace='vehicles')),
    path('media/<path:path>', media_file, name='media'),
    # Module 3: rides, search, route demand, invites
    path('rides/', include(('api.v1.rides.urls', 'rides'), namespace='rides')),
    path('invites/', include(('api.v1.invites.urls', 'invites'), namespace='invites')),
    # Module 4: bookings, price offers, inbox
    path('bookings/', include((booking_urls.booking_patterns, 'bookings'), namespace='bookings')),
    path('offers/', include((booking_urls.offer_patterns, 'offers'), namespace='offers')),
    # Module 7: reviews, reports, blocks
    path('safety/', include(('api.v1.safety.urls', 'safety'), namespace='safety')),
    # Module 8: driver identity verification
    path('verification/', include(('api.v1.verification.urls', 'verification'), namespace='verification')),
]
