from allauth.account.signals import email_confirmed
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.dispatch import receiver

User = get_user_model()

@receiver(email_confirmed)
def email_verified_update(sender, request, email_address, **kwargs):
    user = email_address.user
    if not user:
        return

    update_fields = ['email_verified']
    user.email_verified = True

    if user.pending_email and user.pending_email == email_address.email:
        user.email         = user.pending_email
        user.username      = user.pending_email
        user.pending_email = None
        update_fields += ['email', 'username', 'pending_email']

    try:
        with transaction.atomic():
            user.save(update_fields=update_fields)
    except IntegrityError:
        # Someone else claimed this email address between the confirmation
        # link being sent and being clicked. Don't promote it -- clear the
        # now-stale pending_email instead of leaving the user stuck retrying
        # an address they can never win.
        user.pending_email = None
        user.save(update_fields=['email_verified', 'pending_email'])
        if request is not None:
            messages.error(
                request,
                "That email address was just claimed by another account. "
                "Please try changing your email again with a different address.",
            )
