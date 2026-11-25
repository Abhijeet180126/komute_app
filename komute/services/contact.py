"""Contact Us: the message sent to the Komüte inbox, and the support details
shown next to the form. Shared by the website (contact_view) and the app
API (api/v1/appconfig/)."""
import logging
from email.utils import formataddr

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email

logger = logging.getLogger('komute.views')

# What the contact page offers (templates/contact-us.html); a test keeps
# these in step with the page.
ROLES = ('Passenger', 'Driver', 'Both', 'Not a user yet')
TOPICS = ('General question', 'Help with a booking', 'Account & verification', 'Cost-sharing question',
          'Report a safety concern', 'Partnership / press', 'Feedback & suggestions')
MESSAGE_MAX_CHARS = 1000  # the page's box
CHANNELS = (
    ('General Support', 'support@komute.net'),
    ('Trust & Safety', 'safety@komute.net'),
    ('Legal & Privacy', 'legal@komute.net'),
    ('Partnerships', 'hello@komute.net'),
    ('Security', 'security@komute.org'),
    ('Copyright', 'copyright@komute.org'),
)
HOURS = (
    ('Monday – Saturday', '9:00 AM – 6:00 PM'),
    ('Sunday', 'Closed'),
)
HOURS_TIME_ZONE = 'Eastern Time'

REQUIRED_MSG = 'Please fill in all required fields.'
EMAIL_MSG = 'Please enter a valid email address.'
NAME_MSG = 'Please enter your name.'
SEND_FAILED_MSG = 'Could not send message. Please try again later.'


class ContactError(Exception):
    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def one_line(text):
    return ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in text).split())


def send_message(name, email, role, topic, subject, message):
    """Checks the form as the website does and emails CONTACT_EMAIL, replying
    to the sender. Values arrive stripped; name is cut to 100 characters."""
    name = name[:100]
    if not all([name, email, topic, subject, message]):
        raise ContactError(REQUIRED_MSG, code='required')

    try:
        validate_email(email)
    except ValidationError:
        raise ContactError(EMAIL_MSG, code='invalid_email')

    # Strip control/non-printable characters before this goes into the
    # Reply-To header value below.
    name = ''.join(ch for ch in name if ch.isprintable()).strip()
    if not name:
        raise ContactError(NAME_MSG, code='name_required')
    # Topic and subject go into the Subject header, which refuses line
    # breaks: turn them (and other control characters) into one space.
    topic, subject = one_line(topic), one_line(subject)
    if not (topic and subject):
        raise ContactError(REQUIRED_MSG, code='required')

    body = (
        f"New contact form submission from Komüte\n"
        f"{'─' * 45}\n\n"
        f"Name:     {name}\n"
        f"Email:    {email}\n"
        f"Role:     {role or '—'}\n"
        f"Topic:    {topic}\n"
        f"Subject:  {subject}\n\n"
        f"Message:\n{message}\n"
    )

    try:
        msg = EmailMultiAlternatives(
            subject=f"[Komüte Contact] {topic}: {subject}",
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[settings.CONTACT_EMAIL],
            reply_to=[formataddr((name, email))],
        )
        msg.send(fail_silently=False)
    except Exception:
        logger.exception('contact_view: could not send the contact email')
        raise ContactError(SEND_FAILED_MSG, status=500, code='send_failed')
