"""Communication email for Apple "Hide My Email" users.

Someone who signs in with Apple and hides their address gets an account
whose `email` is a relay address (...@privaterelay.appleid.com). That stays
the sign-in address. They can also give a real address, on the finish-signup
page or later on the profile page, which we email a confirmation link to.
Once confirmed, user.mail_to (models.CustomUser) sends our emails there.

The address is only for sending mail: it never logs anyone in, resets a
password or links accounts, so two accounts may share one.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.http import JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import is_private_relay

logger = logging.getLogger(__name__)

_SALT = 'komute-contact-email-v1'
LINK_MAX_AGE_DAYS = 3


def clean_contact_email(value):
    """The normalized address, or raises ValidationError with the message to show."""
    value = (value or '').strip().lower()
    try:
        validate_email(value)
    except ValidationError:
        raise ValidationError('Enter a valid email address.')
    if is_private_relay(value):
        raise ValidationError("Enter an email address you read, not an Apple relay address.")
    return value


def make_token(user):
    return signing.dumps({'uid': user.pk, 'e': user.contact_email.lower()}, salt=_SALT)


def _user_for_token(token):
    """The user the link is for, or None if it's expired, tampered with, or
    for an address the user has since changed."""
    try:
        data = signing.loads(token, salt=_SALT, max_age=LINK_MAX_AGE_DAYS * 24 * 3600)
    except signing.BadSignature:  # includes SignatureExpired
        return None
    user = get_user_model().objects.filter(pk=data.get('uid'), is_active=True).first()
    if user is None or (user.contact_email or '').lower() != data.get('e'):
        return None
    return user


def send_confirmation(request, user):
    """Email user.contact_email the link that confirms it. Raises if the
    email can't be sent."""
    confirm_url = request.build_absolute_uri(reverse('confirm-contact-email', args=[make_token(user)]))
    ctx = {'user': user, 'contact_email': user.contact_email, 'confirm_url': confirm_url,
           'max_age_days': LINK_MAX_AGE_DAYS}
    msg = EmailMultiAlternatives(
        subject='[Komüte] Confirm your communication email',
        body=render_to_string('account/email/contact_email_confirmation.txt', ctx),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.contact_email],
        reply_to=[settings.REPLY_TO_EMAIL_ACCOUNT_EMAIL_CHANGE],
    )
    msg.attach_alternative(render_to_string('account/email/contact_email_confirmation.html', ctx),
                           'text/html')
    msg.send()


# Shared with the mobile app API (api/v1/users). Callers check, in this
# order: relay_only_error(), blank -> remove(), check_new_address(), their
# rate limits, then start(). The messages are the profile page's.

NOT_RELAY_MSG = 'Only accounts that hide their email from Apple have a separate communication email.'
REMOVED_MSG = 'Communication email removed. We will email you through Apple instead.'
NOT_SENT_MSG = 'Saved, but the confirmation email could not be sent. Please try again later.'


def sent_msg(address):
    return f'We sent a confirmation link to {address}. We will start using it once you click the link.'


def remove(user):
    user.contact_email = None
    user.contact_email_confirmed = False
    user.save(update_fields=['contact_email', 'contact_email_confirmed'])


def check_new_address(user, raw):
    """The normalized address, or raises ValidationError with the message to show."""
    address = clean_contact_email(raw)
    if address == (user.contact_email or '').lower() and user.contact_email_confirmed:
        raise ValidationError('This is already your communication email.')
    return address


def start(request, user, address):
    """Saves `address` as unconfirmed and emails its link. Raises if the
    email can't be sent (the address stays saved)."""
    user.contact_email = address
    user.contact_email_confirmed = False
    user.save(update_fields=['contact_email', 'contact_email_confirmed'])
    send_confirmation(request, user)


@login_required
@require_POST
def set_contact_email_view(request):
    """Profile page: add, change or resend the communication email. Blank
    removes it."""
    from .views import _TOO_MANY_ATTEMPTS_MSG, _rate_limited

    user = request.user
    if not user.uses_private_relay:
        return JsonResponse({'error': NOT_RELAY_MSG}, status=400)

    raw = request.POST.get('contact_email', '').strip()
    if not raw:
        remove(user)
        return JsonResponse({'message': REMOVED_MSG})

    try:
        address = check_new_address(user, raw)
    except ValidationError as e:
        return JsonResponse({'error': e.messages[0]}, status=400)

    if (_rate_limited(request, 'change-email', extra_key=str(user.pk), per_ip=False)
            or _rate_limited(request, 'email-confirm-target', extra_key=address, per_ip=False)):
        return JsonResponse({'error': _TOO_MANY_ATTEMPTS_MSG}, status=429)

    try:
        start(request, user, address)
    except Exception:
        logger.exception('set_contact_email_view could not send for user %s', user.pk)
        return JsonResponse({'error': NOT_SENT_MSG}, status=400)
    return JsonResponse({'message': sent_msg(address)})


def confirm_contact_email_view(request, token):
    """The emailed link. GET shows a Confirm button and POST confirms, so a
    mail scanner opening the link doesn't confirm it on its own."""
    user = _user_for_token(token)
    state = 'invalid'
    if user is not None:
        state = 'ask'
        if user.contact_email_confirmed:
            state = 'done'
        elif request.method == 'POST':
            user.contact_email_confirmed = True
            user.save(update_fields=['contact_email_confirmed'])
            state = 'done'
    return render(request, 'contact-email-confirm.html', {
        'state': state,
        'token': token,
        'contact_email': user.contact_email if user else '',
        'max_age_days': LINK_MAX_AGE_DAYS,
    })
