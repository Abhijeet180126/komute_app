"""Module 9: app start-up config and the contact form (/api/v1/app/).

Both are public: the app reads the config before anyone signs in, and the
website's Contact Us form is open to anyone too. The contact rules are the
website's (komute.services.contact); the app also gets only the topics and
roles the page offers and the page's 1000-character message limit."""
import logging
import re

from django.conf import settings
from django.urls import reverse
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import exceptions, serializers, status
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api.common.exceptions import FieldError
from api.common.throttles import rate_limited
from komute.services import contact

TAG = ['app']
VERSION_RE = re.compile(r'^\d{1,4}\.\d{1,4}\.\d{1,4}$')
logger = logging.getLogger('komute.api')


def _version(setting_name, fallback):
    """A version setting, or `fallback` if .env holds something that is not
    "1.2.3": a typo must never lock every app out."""
    value = str(getattr(settings, setting_name)).strip()
    if value and not VERSION_RE.match(value):
        logger.error('%s=%r is not a "1.2.3" version; sending %r', setting_name, value, fallback)
        return fallback
    return value or fallback


class ContactRefused(exceptions.APIException):
    def __init__(self, error):
        self.status_code = error.status
        super().__init__(error.message, code=error.code)


class TooManyMessages(exceptions.APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = 'throttled'
    default_detail = 'Too many messages sent. Please try again later.'


def _platform(name):
    return inline_serializer(f'AppPlatform{name}', {
        'min_version': serializers.CharField(help_text='Older apps must update before going on.'),
        'latest_version': serializers.CharField(help_text='"" when not set. Newer than yours: offer an update.'),
        'store_url': serializers.CharField(help_text='"" when not set.'),
    })


def _legal_page(name):
    return inline_serializer(f'LegalPage{name}', {
        'version': serializers.CharField(), 'url': serializers.CharField()})


ConfigSerializer = inline_serializer('AppConfig', {
    'ios': _platform('Ios'),
    'android': _platform('Android'),
    'maintenance': inline_serializer('AppMaintenance', {
        'active': serializers.BooleanField(help_text='Show the message instead of the app.'),
        'message': serializers.CharField(),
    }),
    'legal': inline_serializer('AppLegal', {
        'terms': _legal_page('Terms'),
        'privacy': _legal_page('Privacy'),
        'biometric_notice_version': serializers.CharField(),
        'community_guidelines_url': serializers.CharField(),
        'accessibility_url': serializers.CharField(),
    }),
    'support': inline_serializer('AppSupport', {
        'channels': serializers.ListField(child=inline_serializer('SupportChannel', {
            'name': serializers.CharField(), 'email': serializers.EmailField()})),
        'hours': serializers.ListField(child=inline_serializer('SupportHours', {
            'days': serializers.CharField(), 'time': serializers.CharField()})),
        'hours_time_zone': serializers.CharField(),
        'contact_form': inline_serializer('ContactFormOptions', {
            'roles': serializers.ListField(child=serializers.CharField(), help_text='Optional; send one of these.'),
            'topics': serializers.ListField(child=serializers.CharField()),
            'message_max_chars': serializers.IntegerField(),
        }),
    }),
})


@extend_schema(tags=TAG, summary='App start-up config', auth=[], responses={200: ConfigSerializer},
               description='Read at start-up, before sign-in: minimum and latest app versions, the maintenance '
                           'switch, legal page versions and links, support contacts and the contact form options. '
                           'Versions are "major.minor.patch".')
@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def app_config(request):
    def page(name):
        return request.build_absolute_uri(reverse(name))

    return Response({
        'ios': {'min_version': _version('APP_MIN_VERSION_IOS', '0.0.0'),
                'latest_version': _version('APP_LATEST_VERSION_IOS', ''),
                'store_url': settings.APP_STORE_URL_IOS},
        'android': {'min_version': _version('APP_MIN_VERSION_ANDROID', '0.0.0'),
                    'latest_version': _version('APP_LATEST_VERSION_ANDROID', ''),
                    'store_url': settings.APP_STORE_URL_ANDROID},
        'maintenance': {'active': settings.APP_MAINTENANCE, 'message': settings.APP_MAINTENANCE_MESSAGE},
        'legal': {
            'terms': {'version': settings.TERMS_VERSION, 'url': page('terms-condition')},
            'privacy': {'version': settings.PRIVACY_VERSION, 'url': page('privacy-policy')},
            'biometric_notice_version': settings.BIOMETRIC_NOTICE_VERSION,
            'community_guidelines_url': page('community-guidelines'),
            'accessibility_url': page('accessibility'),
        },
        'support': {
            'channels': [{'name': n, 'email': e} for n, e in contact.CHANNELS],
            'hours': [{'days': d, 'time': t} for d, t in contact.HOURS],
            'hours_time_zone': contact.HOURS_TIME_ZONE,
            'contact_form': {'roles': list(contact.ROLES), 'topics': list(contact.TOPICS),
                             'message_max_chars': contact.MESSAGE_MAX_CHARS},
        },
    })


@extend_schema(tags=TAG, summary='Send a message to Komüte', auth=[],
               request=inline_serializer('ContactMessage', {
                   'name': serializers.CharField(), 'email': serializers.EmailField(),
                   'role': serializers.CharField(required=False, help_text='One of the config\'s roles, or "".'),
                   'topic': serializers.CharField(help_text='One of the config\'s topics.'),
                   'subject': serializers.CharField(),
                   'message': serializers.CharField(help_text='Up to message_max_chars.'),
               }),
               responses={200: None},
               description='The website\'s Contact Us form: emailed to the Komüte inbox, replies go to the sender. '
                           'Error codes: required, invalid_email, name_required, invalid_role, invalid_topic, '
                           'message_too_long (400), throttled (429, shared with the website), send_failed (500).')
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([JSONParser])
def contact_us(request):
    if rate_limited(request, 'contact'):
        raise TooManyMessages()

    data = request.data if isinstance(request.data, dict) else {}

    def text(field):
        value = data.get(field, '')
        return value.strip() if isinstance(value, str) else ''

    name, email, role, topic, subject, message = (
        text(f) for f in ('name', 'email', 'role', 'topic', 'subject', 'message'))
    if role and role not in contact.ROLES:
        raise FieldError('role', 'Please choose one of the listed roles.', 'invalid_role')
    if topic and topic not in contact.TOPICS:
        raise FieldError('topic', 'Please choose one of the listed topics.', 'invalid_topic')
    if len(message) > contact.MESSAGE_MAX_CHARS:
        raise FieldError('message', f'Please keep your message to {contact.MESSAGE_MAX_CHARS} characters.',
                         'message_too_long')
    try:
        contact.send_message(name, email, role, topic, subject, message)
    except contact.ContactError as e:
        raise ContactRefused(e)
    return Response({'sent': True})
