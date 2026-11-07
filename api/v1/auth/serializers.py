"""Module 1 request and response shapes.

Request fields are deliberately loose (optional, blank allowed): the checks
are the website's own (komute.services.accounts), so the app gets the same
rules and the same messages, e.g. "Full name is required."."""
from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from api.common.exceptions import FieldError
from api.common.fields import HashidField
from komute.services import accounts


def _text(**kwargs):
    return serializers.CharField(required=False, allow_blank=True, default='', **kwargs)


def _password(**kwargs):
    # Never trimmed: a space can be part of a password, as on the website.
    return serializers.CharField(required=False, allow_blank=True, default='', trim_whitespace=False,
                                 style={'input_type': 'password'}, **kwargs)


def errors_to_validation(errors):
    """The service's {field: message} as a DRF 400."""
    return serializers.ValidationError({field: [message] for field, message in errors.items()})


def account_error_to_validation(error):
    """An AccountError as a 400 on its field (or the whole request), with its code."""
    return FieldError(error.field, error.message, code=error.code)


# ── Responses ─────────────────────────────────────────────────────

class AuthUserSerializer(serializers.Serializer):
    """The signed-in user, as the app needs it to decide the next screen.
    The full profile is module 2."""

    id = HashidField(source='pk', read_only=True)
    email = serializers.EmailField(read_only=True,
                                   help_text='The sign-in address (an Apple relay address for "Hide My Email").')
    full_name = serializers.CharField(read_only=True)
    role = serializers.CharField(read_only=True, help_text='rider, driver or admin.')
    email_verified = serializers.BooleanField(read_only=True,
                                              help_text='Needed before posting or booking a ride.')
    pending_email = serializers.EmailField(read_only=True, allow_null=True,
                                           help_text='A new address waiting for its confirmation link.')
    uses_private_relay = serializers.BooleanField(read_only=True)
    contact_email = serializers.EmailField(read_only=True, allow_null=True)
    contact_email_confirmed = serializers.BooleanField(read_only=True)
    terms_accepted = serializers.SerializerMethodField(
        help_text='False: show the Terms screen (POST /auth/terms/) before anything else.')

    def get_terms_accepted(self, user) -> bool:
        return accounts.has_accepted_current_terms(user)


class SessionSerializer(serializers.Serializer):
    token = serializers.CharField(help_text='Send as the X-Session-Token header on every call.')
    first_login = serializers.BooleanField(
        help_text='True the first time this account signs in: show the rider/driver choice (POST /auth/role/).')
    user = AuthUserSerializer()


class MessageSerializer(serializers.Serializer):
    message = serializers.CharField()


def message(text):
    return {'message': text}


# ── Requests ──────────────────────────────────────────────────────

class SignupSerializer(serializers.Serializer):
    full_name = _text()
    email = _text()
    country_code = _text(help_text='Dial code from the country list, e.g. "+1".')
    phone_number = _text(help_text='Digits only, without the country code.')
    dob = _text(help_text='Date of birth, YYYY-MM-DD. 18 to 100 years old.')
    password = _password()
    confirm_password = _password()
    agreed_to_terms = serializers.BooleanField(default=False,
                                               help_text='The user ticked "I accept the Terms and Privacy Policy".')

    def validate(self, attrs):
        email = attrs['email'].lower()
        errors, phone, dob = accounts.check_signup_details(
            attrs['full_name'], attrs['country_code'], attrs['phone_number'], attrs['dob'],
            '1' if attrs['agreed_to_terms'] else '')
        errors.update(accounts.check_signup_credentials(
            email, attrs['password'], attrs['confirm_password'], attrs['full_name']))
        if errors:
            raise errors_to_validation(errors)
        return {**attrs, 'email': email, 'phone': phone, 'dob': dob}


class LoginSerializer(serializers.Serializer):
    email = _text()
    password = _password()


class SocialTokenSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=['google', 'apple'])
    id_token = serializers.CharField(help_text="The ID token from the Google or Apple SDK.")
    access_token = serializers.CharField(required=False, help_text='Optional, Google only.')
    client_id = serializers.CharField(
        required=False,
        help_text="The client the token was issued to. Google: leave out (the server's web client ID, "
                  "used as serverClientId in the app). Apple: the app's bundle ID.")


class SocialSignupSerializer(serializers.Serializer):
    signup_token = serializers.CharField(help_text='From /auth/social/token/ when status is "signup_required".')
    full_name = _text()
    country_code = _text()
    phone_number = _text()
    dob = _text(help_text='YYYY-MM-DD.')
    agreed_to_terms = serializers.BooleanField(default=False)
    contact_email = _text(help_text='Apple "Hide My Email" only: an address the user reads (optional).')


class SocialTokenResultSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['signed_in', 'signup_required'])
    token = serializers.CharField(required=False, help_text='When signed_in.')
    first_login = serializers.BooleanField(required=False)
    user = AuthUserSerializer(required=False)
    signup_token = serializers.CharField(required=False, help_text='When signup_required.')
    email = serializers.EmailField(required=False)
    full_name = serializers.CharField(required=False)
    asks_contact_email = serializers.BooleanField(
        required=False, help_text='Apple relay address: also offer the optional contact_email field.')


class RoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=list(accounts.SELECTABLE_ROLES),
                                   error_messages={'invalid_choice': 'Choose rider or driver.'})


class ForgotPasswordSerializer(serializers.Serializer):
    email = _text()


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.CharField(help_text='The token from the reset link (/reset-password/<token>/).')
    new_password = _password()
    confirm_password = _password()


class ChangePasswordSerializer(serializers.Serializer):
    current_password = _password()
    new_password = _password()
    confirm_password = _password()


class ChangeEmailSerializer(serializers.Serializer):
    new_email = _text()
    password = _password()


class TermsStatusSerializer(serializers.Serializer):
    accepted = serializers.BooleanField()
    is_update = serializers.BooleanField(help_text='True: the user accepted an earlier version ("we updated our Terms").')
    terms_version = serializers.CharField()
    privacy_version = serializers.CharField()
    terms_url = serializers.URLField()
    privacy_url = serializers.URLField()


def terms_status(request, user):
    return {
        'accepted': accounts.has_accepted_current_terms(user),
        'is_update': user.legal_acceptances.exists(),
        'terms_version': settings.TERMS_VERSION,
        'privacy_version': settings.PRIVACY_VERSION,
        'terms_url': request.build_absolute_uri(reverse('terms-condition')),
        'privacy_url': request.build_absolute_uri(reverse('privacy-policy')),
    }


class AcceptTermsSerializer(serializers.Serializer):
    agreed_to_terms = serializers.BooleanField(default=False)
