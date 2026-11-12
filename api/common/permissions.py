"""The website's account rules, as DRF permissions (docs/api-modular-breakup.md,
section 4). Each has a stable `code`, which ends up in the error body, so the
app can send the user to the right screen.

They assume an authenticated user; DRF answers 401 before any of them runs
when there is no valid token."""
from django.conf import settings
from rest_framework.permissions import BasePermission

from komute.models import LegalAcceptance


def has_accepted_current_terms(user):
    """Same test as komute.middleware.LegalAcceptanceMiddleware."""
    return LegalAcceptance.objects.filter(
        user=user, terms_version=settings.TERMS_VERSION,
        privacy_version=settings.PRIVACY_VERSION).exists()


class HasAcceptedTerms(BasePermission):
    """The current Terms and Privacy Policy versions are accepted (audit P-4)."""

    code = 'terms_not_accepted'
    message = 'Please review and accept the updated Terms and Privacy Policy.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and has_accepted_current_terms(user))


class HasVerifiedEmail(BasePermission):
    """Email verified, needed before posting or booking (audit S-10)."""

    code = 'email_unverified'
    message = 'Please verify your email address first.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.email_verified)


class IsVerifiedDriver(BasePermission):
    """A driver whose identity verification is current (as for posting a ride)."""

    code = 'driver_not_verified'
    message = 'You must be a verified driver to do this.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.role == 'driver'
                    and user.driver_verification_current)


class IsOwner(BasePermission):
    """The object belongs to the user. The owner field is `view.owner_field`
    (default "user"); compared by id, so the owner row isn't loaded."""

    code = 'not_owner'
    message = 'You do not have permission to do this.'

    def has_object_permission(self, request, view, obj):
        field = getattr(view, 'owner_field', 'user')
        user = request.user
        return bool(user and user.is_authenticated
                    and getattr(obj, f'{field}_id', None) == user.pk)
