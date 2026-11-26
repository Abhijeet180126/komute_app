"""Google / Apple sign-in for the app, through the website.

The app opens these pages in the phone's browser (Chrome Custom Tab /
ASWebAuthenticationSession), so the website's own Google and Apple sign-in
runs unchanged, with its keys. When it's done, a one-time code goes back to
the app on its komute:// link, and the app swaps the code for an app session.

    1. GET  /api/v1/auth/app/start/?provider=google&challenge=C
           -> the provider's sign-in (allauth), coming back to finish/.
    2. GET  /api/v1/auth/app/finish/?challenge=C   (signed in on the website)
           -> komute://auth?code=K   (K lives 2 minutes, works once)
    3. POST /api/v1/auth/app/exchange/  {code: K, verifier: V}
           -> the normal session payload {token, first_login, user}.

C is base64url(sha256(V)) for a random V only the app knows (as in PKCE),
so a code caught on its way back to the app is useless to anyone else.
These pages are under /api/, which the Terms check leaves to the app."""
import base64
import hashlib
import re
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api.v1.auth import serializers as s
from api.v1.auth.sessions import start_app_session

APP_CALLBACK = 'komute://auth'
CODE_TTL = 120
_CHALLENGE = re.compile(r'^[A-Za-z0-9_-]{43}$')
PROVIDERS = ('google', 'apple')


def _key(code):
    return f'app-handoff:{code}'


def _challenge(request):
    c = request.GET.get('challenge', '')
    return c if _CHALLENGE.match(c) else None


def _finish_url(challenge):
    return f"{reverse('api:v1:auth:app-finish')}?{urlencode({'challenge': challenge})}"


@never_cache
def start(request):
    challenge = _challenge(request)
    provider_id = request.GET.get('provider', '')
    if not challenge or provider_id not in PROVIDERS:
        return HttpResponseBadRequest('Please start sign-in from the Komüte app.')
    from allauth.socialaccount.adapter import get_adapter
    try:
        provider = get_adapter().get_provider(request, provider_id)
    except Exception:  # noqa: BLE001 - the provider has no keys on this server
        if getattr(settings, 'APP_HANDOFF_TEST_FALLBACK', False):
            # Local testing only (settings_local): the website's email login
            # stands in for the provider, so the rest of the flow can be tried.
            return redirect(f"{reverse('login')}?{urlencode({'next': _finish_url(challenge)})}")
        return HttpResponseBadRequest(f'{provider_id.title()} sign-in is not available right now.')
    # Straight to the provider. The website's own flow takes over from there
    # (linking accounts, the "Almost done" signup and role pages), then comes
    # back to finish/.
    return provider.redirect(request, process='login', next_url=_finish_url(challenge))


@never_cache
def finish(request):
    challenge = _challenge(request)
    if not challenge:
        return HttpResponseBadRequest('Please start sign-in from the Komüte app.')
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
    code = secrets.token_urlsafe(32)
    cache.set(_key(code), {'user': request.user.pk, 'challenge': challenge}, CODE_TTL)
    target = f'{APP_CALLBACK}?{urlencode({"code": code})}'
    # HttpResponseRedirect refuses custom schemes, so the header is set by hand.
    # The page body is a fallback in case the browser doesn't follow it.
    response = HttpResponse(
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Komüte</title><body style="font-family:sans-serif;text-align:center;padding:48px 20px">'
        f'<p>You are signed in.</p><p><a href="{target}">Return to the Komüte app</a></p></body>',
        status=302)
    response['Location'] = target
    return response


class ExchangeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    verifier = serializers.CharField(min_length=43, max_length=128)


@extend_schema(tags=['Auth'], auth=[], request=ExchangeSerializer, responses={200: s.SessionSerializer},
               summary='Finish Google / Apple sign-in done on the website',
               description='Swaps the one-time code from komute://auth for an app session. Error code '
                           'invalid_code (400): start again.')
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def exchange(request):
    body = ExchangeSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    key = _key(body.validated_data['code'])
    entry = cache.get(key)
    cache.delete(key)  # one use only, right or wrong
    digest = hashlib.sha256(body.validated_data['verifier'].encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    if not entry or not secrets.compare_digest(entry['challenge'], expected):
        raise exceptions.ValidationError({'code': 'This sign-in has expired. Please try again.'},
                                         code='invalid_code')
    user = get_user_model().objects.filter(pk=entry['user'], is_active=True).first()
    if user is None:
        raise exceptions.ValidationError({'code': 'This account is not available.'}, code='invalid_code')
    token = start_app_session(request, user)
    return Response(s.SessionSerializer({'token': token, 'first_login': False, 'user': user}).data,
                    status=status.HTTP_200_OK)
