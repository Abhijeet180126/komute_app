"""Sign in with Google / Apple, through allauth.socialaccount.

A provider is on when its keys are in .env (config/settings.py builds
SOCIALACCOUNT_PROVIDERS from them); its button shows only then.

First sign-in: the user lands on the signup page in "social" mode
(SocialSignupView), which asks for what Google/Apple don't give us: phone,
date of birth and the Terms. No password is set. Same rules as email signup
(views._check_signup_details).

Later sign-ins, or an existing email/password account with the same address:
allauth logs the user in when the provider says the address is verified
(SOCIALACCOUNT_EMAIL_AUTHENTICATION). If our copy of that address was never
verified, allauth wipes the old password first, so whoever typed it at
signup can't keep a way in.
"""
import logging

from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.forms import SignupForm
from allauth.socialaccount.providers.base import AuthError
from allauth.socialaccount.views import SignupView
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect

from .models import LegalAcceptance
from . import views

logger = logging.getLogger(__name__)

PROVIDERS = ('google', 'apple')

# Stored on each SocialAccount row: the account id, the address and whether
# the provider verified it, and Apple's "Hide My Email" flag.
KEPT_PROFILE_FIELDS = ('sub', 'email', 'email_verified', 'is_private_email')


def enabled_providers():
    """The providers whose keys are set, in button order."""
    return [p for p in PROVIDERS if p in settings.SOCIALACCOUNT_PROVIDERS]


def social_providers(request):
    """Context processor: which sign-in buttons login.html and signup.html show."""
    return {'social_providers': enabled_providers()}


def mark_email_verified(user, email):
    """The provider vouched for `email`: mark it verified on our side, as
    the "Verify Now" email link would (signals.email_verified_update)."""
    if user.email.lower() != email.lower():
        return
    if not user.email_verified:
        user.email_verified = True
        user.save(update_fields=['email_verified'])
    if not EmailAddress.objects.filter(user=user, email__iexact=email, verified=True).exists():
        EmailAddress.objects.filter(user=user, email__iexact=email).delete()
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    error_messages = {
        **DefaultSocialAccountAdapter.error_messages,
        'email_taken': ('An account already uses this email address. Log in with your '
                        'email and password instead.'),
    }

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        # Apple sends the name only on the very first sign-in.
        user.full_name = ' '.join(filter(None, [data.get('first_name'), data.get('last_name')]))
        return user

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        data = form.cleaned_data
        # username == email everywhere (CustomUserManager, signup_view).
        user.username = user.email
        user.full_name = data['full_name']
        user.phone = data['phone']
        user.dob = data['dob']
        user.save(update_fields=['username', 'full_name', 'phone', 'dob'])
        return user

    def authenticate_by_email(self, sociallogin):
        found = super().authenticate_by_email(sociallogin)
        if found and found[0].email_verified:
            # Some verified accounts have no verified EmailAddress row;
            # without one allauth would treat the address as unverified and
            # wipe the user's password.
            mark_email_verified(*found)
        return found

    def pre_social_login(self, request, sociallogin):
        # allauth keeps the provider's whole profile (photo link, locale...);
        # keep only what the privacy policy (section 1) lists.
        account = sociallogin.account
        account.extra_data = {k: v for k, v in account.extra_data.items() if k in KEPT_PROFILE_FIELDS}
        if account.pk:
            account.save(update_fields=['extra_data'])

        user = sociallogin.user
        if user and user.pk and not user.is_active:
            messages.error(request, 'This account is closed or deactivated. Contact support if '
                                    'you think this is a mistake.')
            raise ImmediateHttpResponse(redirect('login'))

    def on_authentication_error(self, request, provider, error=None, exception=None,
                                extra_context=None):
        if error == AuthError.CANCELLED:
            messages.info(request, f'Sign-in with {provider.name} was cancelled.')
        else:
            logger.warning('%s sign-in failed: %s', provider.id, error, exc_info=exception)
            messages.error(request, f"Sign-in with {provider.name} didn't work. Please try again.")
        raise ImmediateHttpResponse(redirect('login'))


class SocialSignupForm(SignupForm):
    """The fields signup.html posts in social mode. Email comes from the
    provider and can't be changed here."""
    # Checked in clean() with signup_view's rules and messages.
    full_name = forms.CharField(required=False)
    country_code = forms.CharField(required=False)
    phone_number = forms.CharField(required=False)
    dob = forms.CharField(required=False)
    agreed_to_terms = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].disabled = True
        self.initial.setdefault('full_name', self.sociallogin.user.full_name)

    def validate_unique_email(self, value):
        # Said plainly, as signup_view does (audit S-14, accepted). allauth's
        # default instead emails the address and shows its own page.
        if get_user_model().objects.filter(email__iexact=value).exists():
            raise forms.ValidationError(SocialAccountAdapter.error_messages['email_taken'])
        return value

    def clean(self):
        cleaned = super().clean()
        errors, phone, dob = views._check_signup_details(
            cleaned.get('full_name', ''), cleaned.get('country_code', ''),
            cleaned.get('phone_number', ''), cleaned.get('dob', ''),
            cleaned.get('agreed_to_terms', ''))
        for field, message in errors.items():
            self.add_error(field, message)
        cleaned.update(phone=phone, dob=dob)
        return cleaned

    def custom_signup(self, request, user):
        views._record_legal_acceptance(request, user, LegalAcceptance.SOURCE_SIGNUP)
        views._send_welcome_email(request, user)
        # New accounts pick rider/driver first, as after email signup's first
        # login; allauth would otherwise go straight to `next`.
        self.sociallogin.state['next'] = views._with_next('role-select', self.sociallogin.state.get('next'))


class SocialSignupView(SignupView):
    """allauth's social signup, drawn with our signup.html."""
    template_name = 'signup.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx['form']
        errors = {name: errs[0] for name, errs in form.errors.items() if name != '__all__'}
        if form.non_field_errors():
            errors['form'] = form.non_field_errors()[0]
        if not form['email'].value():
            errors['email'] = (f"Your {self.sociallogin.provider.name} account didn't share an "
                               "email address, so it can't be used to sign up.")
        priority_countries, countries = views._dropdown_countries()
        provider = self.sociallogin.provider.name
        ctx.update(
            form={name: form[name].value() or '' for name in (
                'full_name', 'email', 'country_code', 'phone_number', 'dob', 'agreed_to_terms')},
            errors=errors,
            social=provider,
            heading='Almost done',
            subtitle=f'Signed in with {provider}. A few details to finish your account.',
            dob_max=views._latest_adult_dob().isoformat(),
            priority_countries=priority_countries,
            countries=countries,
        )
        return ctx
