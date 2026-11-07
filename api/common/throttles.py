"""Rate limits for the API.

Two layers:

1. AnonBurstThrottle / UserBurstThrottle (on every view, REST_FRAMEWORK
   settings): a generous per-client cap against scripted abuse.
2. rate_limited() / check_rate_limit(): the website's per-action buckets
   (settings.RATE_LIMITS, audit S-15). Same cache keys as
   komute.views._rate_limited, so the website and the app share one budget:
   five logins from the site plus five from the app count as ten.

Both count by the address Nginx saw (komute.client_ip), and both fail open
when the cache (Redis) is down, as the website does."""
import logging

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import Throttled
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from komute.client_ip import client_ip

logger = logging.getLogger('komute.api')


class _KomuteThrottleMixin:
    def get_ident(self, request):
        # DRF's own get_ident() trusts the first X-Forwarded-For entry, which
        # the client chooses. Nginx appends the real one last.
        return client_ip(request)

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.warning('API throttle cache unavailable; request allowed', exc_info=True)
            return True


class AnonBurstThrottle(_KomuteThrottleMixin, AnonRateThrottle):
    pass


class UserBurstThrottle(_KomuteThrottleMixin, UserRateThrottle):
    pass


def rate_limited(request, name, extra_key='', per_ip=True):
    """komute.views._rate_limited for API views (keep the two identical;
    api.tests.test_foundation checks they share buckets). `request` may be a
    DRF Request or a Django HttpRequest. True means block this call."""
    limit, window_seconds = settings.RATE_LIMITS[name]
    ip = client_ip(request) if per_ip else '*'
    bucket = f'ratelimit:{name}:{ip}:{extra_key}'
    try:
        cache.add(bucket, 0, window_seconds)
        count = cache.incr(bucket)
    except Exception:
        return False
    return count > limit


def check_rate_limit(request, name, extra_key='', per_ip=True):
    """rate_limited(), raising a 429 (error code "throttled") when blocked."""
    if rate_limited(request, name, extra_key=extra_key, per_ip=per_ip):
        raise Throttled(wait=settings.RATE_LIMITS[name][1])
